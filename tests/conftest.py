import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from wt import compose, health, host, providers, router, tls
from wt.config import load_config
from wt.errors import WtError
from wt.orchestrator import Orchestrator
from wt.providers.base import BranchContext, Check, Resources
from wt.state import ProcessState

GIT_ENV = {
    "GIT_AUTHOR_NAME": "wt-test",
    "GIT_AUTHOR_EMAIL": "wt-test@example.invalid",
    "GIT_COMMITTER_NAME": "wt-test",
    "GIT_COMMITTER_EMAIL": "wt-test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real ~/.wt or the user's git config."""
    home = tmp_path / "wt-home"
    monkeypatch.setenv("WT_HOME", str(home))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    return home


_LEAKED: list[str] = []


@pytest.fixture(autouse=True)
def host_process_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Stop every host process this test started through host.start, even when it failed.

    Anything still alive after the test's own fixtures ran is killed and reported at the end
    of the session (see pytest_terminal_summary), so a leaking test shows up by name.
    """
    started: list[ProcessState] = []
    real_start, real_alive, real_stop = host.start, host.alive, host.stop

    def tracking_start(*args, **kwargs) -> ProcessState:
        ps = real_start(*args, **kwargs)
        started.append(ps)
        return ps

    monkeypatch.setattr(host, "start", tracking_start)
    yield
    for ps in started:
        if real_alive(ps):  # not host.alive: a test may have stubbed it
            _LEAKED.append(f"{request.node.nodeid}: pgid {ps.pgid} ({ps.command})")
            real_stop(ps, grace=0.2)


def pytest_terminal_summary(terminalreporter) -> None:
    if _LEAKED:
        terminalreporter.section("leaked host processes (killed)")
        for line in _LEAKED:
            terminalreporter.write_line(line)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def make_repo(tmp_path: Path):
    def make(name: str = "app", files: dict[str, str] | None = None) -> Path:
        repo = tmp_path / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        for rel, text in (files or {"README.md": "hello\n"}).items():
            (repo / rel).write_text(text)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        return repo

    return make


COMPOSE = "services:\n  api:\n    image: x\n  db:\n    image: y\n"


@dataclass
class FakeDocker:
    """Records what the orchestrator asks Docker to do."""

    healthy: bool = True
    fail_up: bool = False
    traefik_running: bool = False
    ups: list[str] = field(default_factory=list)
    downs: list[str] = field(default_factory=list)
    kept_downs: list[str] = field(default_factory=list)  # down without -v (stop)
    files: list[list[Path]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "ensure_running", lambda timeout=60, on_line=None: None)
        monkeypatch.setattr(router, "is_running", lambda: self.traefik_running)
        monkeypatch.setattr(router, "missing_in_container", lambda: [])
        monkeypatch.setattr(tls, "available", lambda: False)
        monkeypatch.setattr(compose, "list_services", lambda f: ["api", "db"])
        monkeypatch.setattr(compose, "up", self.up)
        monkeypatch.setattr(compose, "down", self.downs.append)
        monkeypatch.setattr(compose, "down_keep_volumes", self.kept_downs.append)
        monkeypatch.setattr(
            compose,
            "ps",
            lambda p: [compose.Container("db", "running", "")] if p in self.ups else [],
        )
        check = health.Check(self.healthy, "HTTP 200" if self.healthy else "HTTP 502")
        monkeypatch.setattr(health, "wait_healthy", lambda *a, **k: check)
        monkeypatch.setattr(health, "probe", lambda *a, **k: check)

    def up(self, project: str, files: list[Path], **kwargs: object) -> None:
        if self.fail_up:
            raise WtError("compose up failed")
        self.ups.append(project)
        self.files.append(files)


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def orch(make_repo, tmp_path: Path):
    def build(
        env: dict[str, str] | None = None,
        shared: dict[str, object] | None = None,
        extra_services: dict[str, object] | None = None,
        extra_needs: dict[str, list[str]] | None = None,
    ) -> Orchestrator:
        if not (tmp_path / "shop-api").exists():  # a test may build a second config
            make_repo("shop-api", {"docker-compose.yml": COMPOSE})
        config = {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./shop-api"},
            "services": {
                "api": {
                    "repo": "api",
                    "compose": "docker-compose.yml",
                    "port": 8000,
                    "subdomain": "api",
                    "env": env or {"URL": "{{ url }}"},
                },
                "db": {"repo": "api", "compose": "docker-compose.yml"},
                **(extra_services or {}),
            },
            **({"shared": shared} if shared else {}),
        }
        for name, needs in (extra_needs or {}).items():
            config["services"][name]["needs"] = needs  # type: ignore[index]
        path = tmp_path / "worktree.yaml"
        path.write_text(yaml.safe_dump(config))
        return Orchestrator(load_config(path), log=lambda m: None)

    return build


@dataclass
class FakeProvider:
    """A provider that records calls into a shared event list."""

    events: list[str]
    name: str = "postgres"
    fail_create: bool = False
    refuse: bool = False
    check: Check = field(default_factory=lambda: Check(True, "fine", []))
    kinds: list[str] = field(default_factory=list)

    def ensure_server(self) -> None:
        self.events.append("ensure")

    def preflight(self, ctx: BranchContext) -> None:
        if self.refuse:
            raise WtError("refused")

    def create(self, ctx: BranchContext) -> Resources:
        self.events.append("create")
        if self.fail_create:
            raise WtError("create failed")
        return Resources([f"db_{ctx.names.ident}"])

    def verify(self, ctx: BranchContext) -> Check:
        self.events.append("verify")
        return self.check

    def destroy(self, ctx: BranchContext) -> None:
        self.events.append("destroy")

    def template_vars(self, ctx: BranchContext, kind: str) -> dict[str, object]:
        self.kinds.append(kind)
        return {"pg": {"url": f"{kind}://{ctx.names.ident}@{ctx.base_branch}"}}


SHARED = {"postgres": {"databases": ["shop"]}}


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch, docker: FakeDocker) -> FakeProvider:
    """Registers a fake `postgres` provider; compose up/down are logged in the same event list."""
    events: list[str] = []
    fake = FakeProvider(events)
    monkeypatch.setitem(providers.REGISTRY, "postgres", lambda cfg, log: fake)
    real_up, real_down = docker.up, compose.down

    def up(project: str, files: list[Path], **kwargs: object) -> None:
        events.append("compose up")
        real_up(project, files, **kwargs)

    def down(project: str) -> None:
        events.append("compose down")
        real_down(project)

    monkeypatch.setattr(compose, "up", up)
    monkeypatch.setattr(compose, "down", down)
    return fake
