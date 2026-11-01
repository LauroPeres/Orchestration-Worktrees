"""The v0.6.1 behaviours (SPEC section 22) through the real CLI against a real Docker daemon.

Every test drives `python -m wt` in a subprocess with its own WT_HOME, WT_INSTANCE, catalog and
free ports, so nothing here touches a real stack. `pytest -m docker`.
"""

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from wt import health, router

from .isolation import reserve_free_indexes

pytestmark = pytest.mark.docker

SERVE = Path(__file__).parent.parent / "serve.py"
CLI_TIMEOUT = 240
UP_TIMEOUT = 90
SETUP_SLEEP = 30
SIGINT_EXIT_CODES = (130, -signal.SIGINT)
FIRST_ATTEMPTS = 20
ATTEMPT_PAUSE = 0.05
LS_KEYS = {"schema", "project", "branches"}
BRANCH_KEYS = {"branch", "status", "services"}
SERVICE_KEYS = {"name", "kind", "status", "health", "url", "port"}
ACTION_KEYS = {"schema", "branch", "action", "changed", "status", "urls", "warnings"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return out.stdout.strip()


class Wt:
    """One isolated wt installation: instance name, WT_HOME, catalog and a way to run `wt`."""

    def __init__(self, instance: str, wt_home: Path, catalog: Path) -> None:
        self.instance = instance
        self.wt_home = wt_home
        self.catalog = catalog

    @property
    def domain(self) -> str:
        return f"{self.instance}.localhost"

    def write_catalog(self, repos: Mapping[str, Path], services: Mapping[str, object]) -> None:
        document = {
            "project": self.instance,
            "domain": self.domain,
            "repos": {name: str(path) for name, path in repos.items()},
            "services": services,
        }
        self.catalog.write_text(yaml.safe_dump(document))

    def popen(self, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-m", "wt", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "COLUMNS": "400"},
            start_new_session=True,  # a SIGINT aimed at wt must not reach pytest
        )

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "wt", *args],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
            env={**os.environ, "COLUMNS": "400"},
        )

    def json(self, *args: str, code: int = 0) -> dict[str, Any]:
        result = self.run(*args, "--json")
        assert result.returncode == code, (args, result.stdout, result.stderr)
        document: dict[str, Any] = json.loads(result.stdout)
        return document

    def up(self, branch: str, *uses: str) -> dict[str, Any]:
        extra = ["--uses", ",".join(uses)] if uses else []
        return self.json("up", branch, *extra, "--timeout", str(UP_TIMEOUT))

    def ls(self) -> dict[str, Any]:
        return self.json("ls")

    def branch(self, name: str) -> dict[str, Any]:
        rows = [row for row in self.ls()["branches"] if row["branch"] == name]
        assert len(rows) == 1, rows
        row: dict[str, Any] = rows[0]
        return row

    def port_of(self, branch: str, service: str) -> int:
        services = {s["name"]: s for s in self.branch(branch)["services"]}
        port = services[service]["port"]
        assert isinstance(port, int), services
        return port

    def fetch(self, branch: str, service: str, path: str) -> str:
        url = f"http://127.0.0.1:{self.port_of(branch, service)}{path}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return str(resp.read().decode())

    def probe(self, host: str) -> str:
        return health.probe(host, "/", router.https_port(), timeout=2.0).detail

    def setup_log(self, service: str) -> Path | None:
        found = sorted(self.wt_home.glob(f"**/{service}.setup.log"))
        return found[0] if found else None

    def cleanup(self) -> None:
        with contextlib.suppress(Exception):
            for row in self.ls()["branches"]:
                self.run("down", row["branch"], "--force")
        docker("rm", "-f", "-v", router.container())
        docker("network", "rm", self.instance)


@pytest.fixture
def wt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Wt]:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.skip("docker is not available")
    instance = f"wtv061{free_port()}"
    wt_home = Path(os.environ["WT_HOME"])
    catalog = tmp_path / "catalog.yaml"
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_CATALOG", str(catalog))
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")  # no mkcert: self-signed path
    reserve_free_indexes(wt_home)
    stack = Wt(instance, wt_home, catalog)
    yield stack
    stack.cleanup()


def serve_service(repo: str, **extra: object) -> dict[str, object]:
    return {
        "repo": repo,
        "run": f"{sys.executable} {SERVE} {{{{ port }}}}",
        "port": "auto",
        "subdomain": repo,
        **extra,
    }


def setup_text(version: str) -> str:
    """Counts its runs in counter.txt and writes the version served as marker.txt."""
    return f"echo run >> counter.txt && printf '%s' {version} > marker.txt"


def counter(stack: Wt, branch: str) -> int:
    return len(stack.fetch(branch, "app", "/counter.txt").split())


def wait_for(condition: Callable[[], bool], timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def test_setup_runs_once_and_again_only_when_its_text_changes(wt: Wt, make_repo) -> None:
    repo = make_repo("app")
    services = {"app": serve_service("app", setup=setup_text("first"))}
    wt.write_catalog({"app": repo}, services)

    assert wt.up("feat/s", "app")["status"] == "up"
    assert wt.fetch("feat/s", "app", "/marker.txt") == "first"
    assert counter(wt, "feat/s") == 1

    wt.up("feat/s")  # the stored selection is used
    assert counter(wt, "feat/s") == 1  # same text, same hash: setup did not run again

    services["app"] = serve_service("app", setup=setup_text("second"))
    wt.write_catalog({"app": repo}, services)
    wt.up("feat/s")
    assert wt.fetch("feat/s", "app", "/marker.txt") == "second"
    assert counter(wt, "feat/s") == 2

    wt.up("feat/s")
    assert counter(wt, "feat/s") == 2  # re-ran once, not on every up


def test_a_failing_setup_fails_up_with_the_exit_code_and_the_log(wt: Wt, make_repo) -> None:
    repo = make_repo("app")
    wt.write_catalog(
        {"app": repo}, {"app": serve_service("app", setup="echo setup-output-here; exit 3")}
    )

    document = wt.json("up", "feat/bad", "--uses", "app", "--timeout", str(UP_TIMEOUT), code=1)

    log = wt.setup_log("app")
    assert log is not None, list(wt.wt_home.rglob("*.log"))
    assert document["error"] == f"setup of app failed (exit 3); see {log}"
    assert "setup-output-here" in log.read_text()
    assert wt.branch("feat/bad")["status"] == "failed"


def test_requests_right_after_down_get_404_never_502(wt: Wt, make_repo) -> None:
    wt.write_catalog({"app": make_repo("app")}, {"app": serve_service("app")})
    wt.up("feat/gone", "app")
    host = f"app.feat-gone.{wt.domain}"
    assert wt.probe(host) == "HTTP 200"

    seen: list[str] = []
    down = wt.popen("down", "feat/gone", "--force")
    while down.poll() is None:  # during down: the route goes first, so never a dead backend
        seen.append(wt.probe(host))
    down.communicate()
    assert down.returncode == 0
    for _ in range(FIRST_ATTEMPTS):
        seen.append(wt.probe(host))
        time.sleep(ATTEMPT_PAUSE)

    assert "HTTP 502" not in seen, seen
    assert seen[-1] in ("HTTP 404",) or seen[-1].startswith("no response"), seen


def test_up_outside_a_git_repository_leaves_no_traefik_and_no_network(wt: Wt, tmp_path: Path):
    not_git = tmp_path / "plain-folder"
    not_git.mkdir()
    wt.write_catalog({"app": not_git}, {"app": serve_service("app")})

    result = wt.run("up", "feat/x", "--uses", "app")

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "not a git repository" in result.stderr + result.stdout
    traefik = docker("ps", "-a", "-q", "--filter", f"name=^{router.container()}$")
    assert traefik.stdout.strip() == ""
    network = docker("network", "ls", "-q", "--filter", f"name=^{wt.instance}$")
    assert network.stdout.strip() == ""
    assert wt.ls()["branches"] == []  # no half-created branch in state either


def test_json_documents_carry_ports_changed_and_urls(wt: Wt, make_repo) -> None:
    wt.write_catalog({"app": make_repo("app")}, {"app": serve_service("app")})

    added = wt.json("add", "feat/j", "--uses", "app")
    assert set(added) == ACTION_KEYS
    assert (added["schema"], added["action"], added["changed"]) == (1, "add", True)
    assert set(added["urls"]) == {"app"}
    assert wt.json("add", "feat/j", "--uses", "app")["changed"] is False

    up = wt.up("feat/j")
    assert set(up) == ACTION_KEYS
    assert (up["action"], up["status"]) == ("up", "up")
    assert set(up["urls"]) == {"app"}

    listing = wt.ls()
    assert set(listing) == LS_KEYS and listing["schema"] == 1
    assert listing["project"] == wt.instance
    (row,) = listing["branches"]
    assert set(row) == BRANCH_KEYS and row["status"] == "up"
    (service,) = row["services"]
    assert set(service) == SERVICE_KEYS
    assert service["kind"] == "run" and service["name"] == "app"
    assert isinstance(service["port"], int) and not isinstance(service["port"], bool)
    assert service["url"] and service["health"] == "ok"

    stopped = wt.json("stop", "feat/j")
    assert set(stopped) == ACTION_KEYS
    assert (stopped["action"], stopped["changed"], stopped["status"]) == ("stop", True, "stopped")
    assert set(stopped["urls"]) == {"app"}

    down = wt.json("down", "feat/j", "--force")
    assert (down["changed"], down["status"], down["urls"]) == (True, None, {})
    assert wt.json("down", "feat/j")["changed"] is False


def test_remove_after_down_deletes_the_branch_in_every_repo(wt: Wt, make_repo) -> None:
    first, second = make_repo("first"), make_repo("second")
    wt.write_catalog(
        {"first": first, "second": second},
        {"first": serve_service("first"), "second": serve_service("second")},
    )
    wt.up("feat/rm", "first", "second")
    assert git(first, "branch", "--list", "feat/rm") and git(second, "branch", "--list", "feat/rm")

    wt.json("down", "feat/rm", "--force")
    assert git(first, "branch", "--list", "feat/rm") and git(second, "branch", "--list", "feat/rm")
    removed = wt.run("remove", "feat/rm", "--yes")

    assert removed.returncode == 0, (removed.stdout, removed.stderr)
    assert git(first, "branch", "--list", "feat/rm") == ""
    assert git(second, "branch", "--list", "feat/rm") == ""
    assert git(first, "branch", "--list", "main") and git(second, "branch", "--list", "main")


def test_ctrl_c_during_setup_leaves_the_branch_failed_and_down_cleans(wt: Wt, make_repo) -> None:
    repo = make_repo("app")
    wt.write_catalog({"app": repo}, {"app": serve_service("app", setup=f"sleep {SETUP_SLEEP}")})
    process = wt.popen("up", "feat/int", "--uses", "app", "--timeout", str(UP_TIMEOUT))
    try:
        assert wait_for(lambda: wt.setup_log("app") is not None), "setup never started"
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode in SIGINT_EXIT_CODES, (process.returncode, err)
    assert "interrupted" in out + err
    assert wt.branch("feat/int")["status"] == "failed"

    assert wt.json("down", "feat/int", "--force")["changed"] is True
    assert wt.ls()["branches"] == []
