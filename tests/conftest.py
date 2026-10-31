import os
import random
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from wt import compose, health, host, naming, providers, router, tls
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


PORT_WINDOW = 1000  # room for worktree indexes 0..9 of 100 services
PORT_WINDOW_FLOOR = 15000
# The OS hands out ephemeral ports (client sockets, bind(0)) from a range that starts at 32768 on
# Linux and at 49152 on macOS. A window below the lowest start never overlaps either one, so a
# port the formula picks cannot be taken by an unrelated connection.
LOWEST_EPHEMERAL_PORT = 32768


def _window_is_free(base: int, size: int = PORT_WINDOW) -> bool:
    """True when every port in [base, base + size) can be bound right now.

    A bind attempt (no SO_REUSEADDR, like a server that did not set it) also rejects ports that
    are bound but not listening or held as the local end of a connection, which a connect misses.
    """
    for port in range(base, base + size):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                return False
    return True


WINDOW_TRIES = 50
# Every window the run probed and every base a test saw: tests assert the probe happens once per
# run: a probe per test opens ~1000 sockets each and exhausts macOS socket buffers (ENOBUFS).
_PROBED_WINDOWS: list[int] = []
_BASES_SEEN: set[int] = set()


def _pick_free_window(is_free: Callable[[int], bool], probed: list[int]) -> int:
    """First random window that `is_free` accepts, noting every window tried in `probed`."""
    rng = random.Random()
    for _ in range(WINDOW_TRIES):
        base = rng.randrange(PORT_WINDOW_FLOOR, LOWEST_EPHEMERAL_PORT - PORT_WINDOW)
        probed.append(base)
        if is_free(base):
            return base
    raise RuntimeError(
        f"no free window of {PORT_WINDOW} ports in [{PORT_WINDOW_FLOOR}, {LOWEST_EPHEMERAL_PORT}) "
        f"after {WINDOW_TRIES} tries; free the local ports or lower PORT_WINDOW"
    )


@pytest.fixture(scope="session")
def session_port_base() -> int:
    """Pick the free private window once per run; a bind probe per test is too many sockets."""
    return _pick_free_window(_window_is_free, _PROBED_WINDOWS)


@pytest.fixture(autouse=True)
def private_port_base(session_port_base: int, monkeypatch: pytest.MonkeyPatch) -> int:
    """Move the host-port formula to the run's private window, so tests never meet a real stack."""
    _BASES_SEEN.add(session_port_base)
    monkeypatch.setattr(naming, "PORT_BASE", session_port_base)
    return session_port_base


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
    resource_names: list[str] | None = None  # what `create` returns; default `db_<ident>`
    recorded_at_destroy: list[list[str]] = field(default_factory=list)

    def ensure_server(self) -> None:
        self.events.append("ensure")

    def preflight(self, ctx: BranchContext) -> None:
        if self.refuse:
            raise WtError("refused")

    def create(self, ctx: BranchContext) -> Resources:
        self.events.append("create")
        if self.fail_create:
            raise WtError("create failed")
        return Resources(self.resource_names or [f"db_{ctx.names.ident}"])

    def verify(self, ctx: BranchContext) -> Check:
        self.events.append("verify")
        return self.check

    def destroy(self, ctx: BranchContext) -> None:
        self.events.append("destroy")
        # Real providers read the recorded names from the state, so the fake captures the same view.
        bs = ctx.state.load().branches.get(ctx.branch)
        self.recorded_at_destroy.append(list(bs.providers.get(self.name, [])) if bs else [])

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
