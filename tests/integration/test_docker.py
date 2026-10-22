"""End to end against a real Docker daemon: `pytest -m docker`."""

import json
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from wt import health, router
from wt.config import load_config
from wt.orchestrator import Orchestrator

pytestmark = pytest.mark.docker

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


@pytest.fixture
def stack(make_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Orchestrator]:
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
        "services": {
            "api": {
                "repo": "api",
                "compose": "docker-compose.yml",
                "port": 8000,
                "subdomain": "api",
                "env": {"BRANCH_URL": "{{ url }}"},
            }
        },
    }
    path = tmp_path / "worktree.yaml"
    path.write_text(yaml.safe_dump(config))
    orch = Orchestrator(load_config(path), log=lambda m: None)
    yield orch
    for branch in list(orch.store.load().branches):
        orch.down(branch, force=True)
    docker("rm", "-f", router.container())
    docker("network", "rm", instance)


def env_of(container: str) -> list[str]:
    out = docker("inspect", container, "--format", "{{json .Config.Env}}").stdout
    return list(json.loads(out))


def test_two_branches_side_by_side(stack: Orchestrator) -> None:
    for branch in ("main", "feat/cart"):
        reports = stack.up(branch, timeout=90)
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
