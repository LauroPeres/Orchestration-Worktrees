"""End to end against a real Docker daemon: `pytest -m docker`."""

import json
import shutil
import socket
import subprocess
import sys
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import yaml

from wt import health, naming, router
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
    make_repo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    services: Mapping[str, object],
    compose_text: str = COMPOSE,
) -> Iterator[Orchestrator]:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.skip("docker is not available")
    instance = f"wtit{free_port()}"
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")  # no mkcert: self-signed path
    make_repo("api", {"docker-compose.yml": compose_text})
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


VOLUME_COMPOSE = """\
services:
  box:
    image: alpine:3
    command: ["sleep", "3600"]
    volumes: ["data:/data"]
volumes:
  data: {}
"""


@pytest.fixture
def volume_stack(
    make_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Orchestrator]:
    services = {"box": {"repo": "api", "compose": "docker-compose.yml"}}
    yield from make_stack(make_repo, tmp_path, monkeypatch, services, VOLUME_COMPOSE)


REMOTE = "https://api.remote.example"


@pytest.fixture
def catalog_stack(
    make_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Orchestrator]:
    """A compose `api` and a host-process `web` that consumes it (remote when api is dropped)."""
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.skip("docker is not available")
    instance = f"wtit{free_port()}"
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")
    make_repo("api", {"docker-compose.yml": COMPOSE})
    make_repo("web")
    catalog = {
        "repos": {"api": "./api", "web": "./web"},
        "services": {
            "api": {
                "repo": "api",
                "compose": "docker-compose.yml",
                "port": 8000,
                "subdomain": "api",
                "remote": REMOTE,
            },
            "web": {
                "repo": "web",
                "run": f"{sys.executable} {SERVE} {{{{ port }}}}",
                "port": "auto",
                "subdomain": "web",
                "consumes": {"api": "API_URL"},
            },
        },
    }
    (tmp_path / "catalog.yaml").write_text(yaml.safe_dump(catalog))
    config = {
        "project": instance,
        "domain": f"{instance}.localhost",
        "catalog": "./catalog.yaml",
        "uses": ["web", "api"],
    }
    path = tmp_path / "worktree.yaml"
    path.write_text(yaml.safe_dump(config))
    orch = Orchestrator(load_config(path), log=lambda m: None)
    yield orch
    for branch in list(orch.store.load().branches):
        orch.down(branch, force=True)
    docker("rm", "-f", router.container())
    docker("network", "rm", instance)


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


def get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return int(resp.status), resp.read().decode()


def test_worktrees_get_formula_ports_and_publish_on_loopback(
    stack: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    for branch in ("main", "feat/cart"):
        up_retrying(stack, branch, monkeypatch)

    branches = stack.store.load().branches
    assert [branches[b].index for b in ("main", "feat/cart")] == [0, 1]
    main_port = branches["main"].ports["api"]
    cart_port = branches["feat/cart"].ports["api"]
    assert (main_port, cart_port) == (naming.PORT_BASE, naming.PORT_BASE + 100)

    for port in (main_port, cart_port):
        status, body = get(f"http://127.0.0.1:{port}/")
        assert status == 200
        assert "Hostname:" in body
    container = f"{stack.names('main').compose_project('api')}-api-1"
    published = docker("port", container, "8000/tcp").stdout.split()
    assert published == [f"127.0.0.1:{main_port}"]  # loopback only, no 0.0.0.0

    for branch, port in (("main", main_port), ("feat/cart", cart_port)):
        rows = stack.services(branch)
        assert [(r.name, r.status, r.port) for r in rows] == [("api", "healthy", port)]
        assert rows[0].url is not None


def test_stop_keeps_a_named_volume_and_down_removes_it(
    volume_stack: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = volume_stack
    up_retrying(orch, "main", monkeypatch)
    project = orch.names("main").compose_project("api")
    container = f"{project}-box-1"

    def volumes() -> list[str]:
        label = f"label=com.docker.compose.project={project}"
        return docker("volume", "ls", "-q", "--filter", label).stdout.split()

    assert len(volumes()) == 1
    written = docker("exec", container, "sh", "-c", "echo kept > /data/note.txt")
    assert written.returncode == 0, written.stderr

    orch.stop("main")
    assert len(volumes()) == 1
    up_retrying(orch, "main", monkeypatch)
    read = docker("exec", container, "cat", "/data/note.txt")
    assert read.stdout.strip() == "kept"

    orch.down("main", force=True)
    assert volumes() == []


def process_env(orch: Orchestrator, branch: str, service: str) -> dict[str, str]:
    pid = orch.store.load().branches[branch].processes[service].pid
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    return dict(item.decode().split("=", 1) for item in raw if b"=" in item)


def test_configure_drops_a_service_and_adds_it_back(
    catalog_stack: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = catalog_stack
    up_retrying(orch, "main", monkeypatch)
    api_port = orch.store.load().branches["main"].ports["api"]
    assert get(f"http://127.0.0.1:{api_port}/")[0] == 200
    own_url = process_env(orch, "main", "web")["API_URL"]
    assert own_url.startswith("https://api.main.")

    orch.configure("main", ["web"])
    bs = orch.store.load().branches["main"]
    assert bs.uses == ["web"]
    assert "api" not in bs.ports
    assert process_env(orch, "main", "web")["API_URL"] == REMOTE
    project = orch.names("main").compose_project("api")
    label = f"label=com.docker.compose.project={project}"
    assert docker("ps", "-aq", "--filter", label).stdout.strip() == ""
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", api_port), timeout=1)
    assert [r.name for r in orch.services("main")] == ["web"]

    orch.configure("main", ["web", "api"])
    assert get(f"http://127.0.0.1:{orch.store.load().branches['main'].ports['api']}/")[0] == 200
    assert process_env(orch, "main", "web")["API_URL"].startswith("https://api.main.")
    assert {r.name: r.status for r in orch.services("main")} == {"web": "healthy", "api": "healthy"}
