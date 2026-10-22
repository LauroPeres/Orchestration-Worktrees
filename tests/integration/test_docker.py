"""End to end against a real Docker daemon: `pytest -m docker`."""

import json
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import yaml

from wt import health, router
from wt.config import load_config
from wt.errors import WtError
from wt.orchestrator import Orchestrator, ServiceReport

pytestmark = pytest.mark.docker

SERVE = Path(__file__).parent.parent / "serve.py"
IMAGE = "traefik/whoami:v1.10"
COMPOSE = f"""\
services:
  api:
    image: {IMAGE}
    container_name: fixed-name   # removed by wt, or two branches would collide
    ports: ["8000:8000"]         # same
    command: ["--port", "8000"]
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def make_stack(
    make_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services: Mapping[str, object]
) -> Iterator[Orchestrator]:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.skip("docker is not available")
    instance = f"wtit{free_port()}"
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")  # no mkcert: self-signed path
    make_repo("api", {"docker-compose.yml": COMPOSE})
    config = {
        "project": instance,
        "domain": f"{instance}.localhost",
        "repos": {"api": "./api"},
        "services": services,
    }
    path = tmp_path / "worktree.yaml"
    path.write_text(yaml.safe_dump(config))
    orch = Orchestrator(load_config(path), log=lambda m: None)
    yield orch
    for branch in list(orch.store.load().branches):
        orch.down(branch, force=True)
    docker("rm", "-f", router.container())
    docker("network", "rm", instance)


@pytest.fixture
def stack(make_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Orchestrator]:
    services = {
        "api": {
            "repo": "api",
            "compose": "docker-compose.yml",
            "port": 8000,
            "subdomain": "api",
            "env": {"BRANCH_URL": "{{ url }}"},
        }
    }
    yield from make_stack(make_repo, tmp_path, monkeypatch, services)


@pytest.fixture
def host_stack(
    make_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Orchestrator]:
    services = {
        "web": {
            "repo": "api",
            "run": f"{sys.executable} {SERVE} {{{{ port }}}}",
            "port": "auto",
            "subdomain": "web",
        }
    }
    yield from make_stack(make_repo, tmp_path, monkeypatch, services)


def up_retrying(
    orch: Orchestrator, branch: str, monkeypatch: pytest.MonkeyPatch
) -> list[ServiceReport]:
    """Another test run may grab a port between free_port() and compose up: retry once."""
    try:
        return orch.up(branch, timeout=90)
    except WtError as exc:
        if "not available" not in str(exc) and "already allocated" not in str(exc):
            raise
        monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
        monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
        return orch.up(branch, timeout=90)


def env_of(container: str) -> list[str]:
    out = docker("inspect", container, "--format", "{{json .Config.Env}}").stdout
    return list(json.loads(out))


def test_two_branches_side_by_side(stack: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    for branch in ("main", "feat/cart"):
        reports = up_retrying(stack, branch, monkeypatch)
        assert [r.outcome for r in reports] == ["ok"], reports

    port = router.https_port()
    for slug in ("main", "feat-cart"):
        host = f"api.{slug}.{stack.cfg.domain}"
        assert health.probe(host, "/", port).ok

    project = stack.names("feat/cart").compose_project("api")
    assert f"BRANCH_URL=https://api.feat-cart.{stack.cfg.domain}:{port}" in env_of(
        f"{project}-api-1"
    )

    rows = {r.branch: r for r in stack.ls()}
    assert set(rows) == {"main", "feat/cart"}
    assert all(r.running == 1 for r in rows.values())

    stack.down("feat/cart")
    assert not health.probe(f"api.feat-cart.{stack.cfg.domain}", "/", port).ok
    assert health.probe(f"api.main.{stack.cfg.domain}", "/", port).ok  # main untouched
    assert (
        docker("ps", "-q", "--filter", f"label=com.docker.compose.project={project}").stdout.strip()
        == ""
    )


def test_traefik_routes_to_a_host_process(
    host_stack: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = up_retrying(host_stack, "main", monkeypatch)
    assert [(r.service, r.outcome) for r in reports] == [("web", "ok")], reports

    ps = host_stack.store.load().branches["main"].processes["web"]
    host = f"web.main.{host_stack.cfg.domain}"
    assert health.probe(host, "/", router.https_port()).detail == "HTTP 200"
    extra = docker("inspect", router.container(), "--format", "{{json .HostConfig.ExtraHosts}}")
    assert "host.docker.internal:host-gateway" in extra.stdout

    host_stack.down("main")
    assert not health.probe(host, "/", router.https_port()).ok
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", ps.port or 0), timeout=1)
