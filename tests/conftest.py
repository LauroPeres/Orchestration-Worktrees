import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from wt import compose, health, router, tls
from wt.config import load_config
from wt.errors import WtError
from wt.orchestrator import Orchestrator

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
    files: list[list[Path]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(router, "ensure_running", lambda timeout=60: None)
        monkeypatch.setattr(router, "is_running", lambda: self.traefik_running)
        monkeypatch.setattr(tls, "available", lambda: False)
        monkeypatch.setattr(compose, "list_services", lambda f: ["api", "db"])
        monkeypatch.setattr(compose, "up", self.up)
        monkeypatch.setattr(compose, "down", self.downs.append)
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
    def build(env: dict[str, str] | None = None) -> Orchestrator:
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
            },
        }
        path = tmp_path / "worktree.yaml"
        path.write_text(yaml.safe_dump(config))
        return Orchestrator(load_config(path), log=lambda m: None)

    return build
