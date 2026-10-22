import os
import subprocess
from pathlib import Path

import pytest

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
