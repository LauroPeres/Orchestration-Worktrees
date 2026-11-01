"""The v0.8.0 install cache (SPEC section 24) through the real CLI against a real Docker daemon.

The fixture project depends on a local package, so `npm install --package-lock-only` and
`npm ci --offline` need no network. Every test has its own WT_HOME, WT_INSTANCE, catalog and ports.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .isolation import reserve_free_indexes
from .test_v061 import UP_TIMEOUT, Wt, docker, free_port, git

pytestmark = pytest.mark.docker

NPM = shutil.which("npm")  # resolved before PATH is trimmed
NODE = shutil.which("node")
SAFE_PATH = "/usr/bin:/bin:/usr/local/bin"  # no mkcert: the self-signed path
RESTORED_MESSAGE = "restored node_modules from the install cache"
SERVER_JS = """\
const http = require('http');
const text = require('dep');
http.createServer((req, res) => res.end(text)).listen(parseInt(process.argv[2], 10), '0.0.0.0');
"""


def _package(name: str, version: str, extra: dict[str, object] | None = None) -> str:
    return json.dumps({"name": name, "version": version, **(extra or {})}, indent=2) + "\n"


def _npm(repo: Path, *args: str) -> None:
    assert NPM is not None
    result = subprocess.run(
        [NPM, *args], cwd=repo, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)


def _write_project(repo: Path, dep_version: str) -> None:
    (repo / "dep").mkdir(exist_ok=True)
    (repo / "dep" / "package.json").write_text(_package("dep", dep_version))
    (repo / "dep" / "index.js").write_text("module.exports = 'hello';\n")
    (repo / "package.json").write_text(
        _package("app", "1.0.0", {"dependencies": {"dep": "file:./dep"}})
    )
    (repo / "server.js").write_text(SERVER_JS)
    (repo / ".gitignore").write_text("node_modules/\n")
    _npm(repo, "install", "--package-lock-only", "--offline", "--no-audit", "--no-fund")


def _commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Wt]:
    if NPM is None or NODE is None:
        pytest.skip("npm and node are not on PATH")
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.skip("docker is not available")
    instance = f"wtv08{free_port()}"
    wt_home = Path(os.environ["WT_HOME"])
    catalog = tmp_path / "catalog.yaml"
    node_dirs = {str(Path(NPM).parent), str(Path(NODE).parent)}
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_CATALOG", str(catalog))
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("PATH", ":".join([*sorted(node_dirs), SAFE_PATH]))
    reserve_free_indexes(wt_home)
    wt = Wt(instance, wt_home, catalog)
    yield wt
    wt.cleanup()


def _npm_service(setups: Path, **extra: object) -> dict[str, object]:
    return {
        "repo": "app",
        "setup": f"echo x >> {setups} && npm ci --offline --no-audit --no-fund",
        "setup_cache": "npm",
        "run": "node server.js {{ port }}",
        "port": "auto",
        "subdomain": "app",
        **extra,
    }


def _lines(path: Path) -> int:
    return len(path.read_text().split()) if path.exists() else 0


def _worktree(wt: Wt, branch: str) -> Path:
    found = sorted(wt.wt_home.glob(f"**/{branch.replace('/', '-')}/**/node_modules"))
    assert len(found) == 1, found
    return found[0]


def _cache_ls(wt: Wt) -> dict[str, Any]:
    return wt.json("cache", "ls")


def test_npm_cache_restores_misses_on_a_new_lock_and_survives_prune(
    stack: Wt, make_repo, tmp_path: Path
) -> None:
    repo = make_repo("app")
    _write_project(repo, "1.0.0")
    _commit_all(repo, "project")
    git(repo, "branch", "feat/c")
    git(repo, "checkout", "-q", "feat/c")
    _write_project(repo, "2.0.0")  # another lock file content
    _commit_all(repo, "other lock")
    git(repo, "checkout", "-q", "main")
    setups = tmp_path / "setups.txt"
    stack.write_catalog({"app": repo}, {"app": _npm_service(setups)})

    # first branch: a miss, so setup runs and the install is stored
    assert stack.up("feat/a", "app")["status"] == "up"
    assert _lines(setups) == 1
    assert stack.fetch("feat/a", "app", "/") == "hello"
    listing = _cache_ls(stack)
    assert [e["preset"] for e in listing["entries"]] == ["npm"]

    # second branch, same lock files: restored, setup does not run
    second = stack.run("up", "feat/b", "--uses", "app", "--timeout", str(UP_TIMEOUT), "--json")
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert RESTORED_MESSAGE in second.stdout + second.stderr
    assert _lines(setups) == 1
    restored = _worktree(stack, "feat/b")
    assert (restored / "dep").exists()
    assert stack.fetch("feat/b", "app", "/") == "hello"

    # a different lock file: a new key, setup runs again
    assert stack.up("feat/c", "app")["status"] == "up"
    assert _lines(setups) == 2
    assert len(_cache_ls(stack)["entries"]) == 2

    # prune --all removes the store but not what worktrees already copied
    pruned = stack.run("cache", "prune", "--all", "--yes", "--json")
    assert pruned.returncode == 0, (pruned.stdout, pruned.stderr)
    assert len(json.loads(pruned.stdout)["removed"]) >= 2
    assert _cache_ls(stack)["entries"] == []
    assert (restored / "dep").exists()
    assert stack.fetch("feat/b", "app", "/") == "hello"

    # the first lock content is a miss again
    assert stack.up("feat/d", "app")["status"] == "up"
    assert _lines(setups) == 3


def test_npm_cache_without_package_lock_fails_up_and_creates_nothing(
    stack: Wt, make_repo, tmp_path: Path
) -> None:
    repo = make_repo("app", {"package.json": _package("app", "1.0.0"), "server.js": SERVER_JS})
    setups = tmp_path / "setups.txt"
    stack.write_catalog({"app": repo}, {"app": _npm_service(setups)})

    result = stack.run("up", "feat/x", "--uses", "app", "--timeout", str(UP_TIMEOUT))

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "needs package-lock.json" in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert not setups.exists()
    assert list(stack.wt_home.glob("**/node_modules")) == []
    assert _cache_ls(stack)["entries"] == []
    assert stack.ls()["branches"] == []
