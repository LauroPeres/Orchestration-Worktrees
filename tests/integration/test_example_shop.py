"""The examples/shop stack end to end against a real Docker daemon: `pytest -m docker`."""

import contextlib
import json
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wt import compose, health, router
from wt.config import load_config
from wt.errors import WtError
from wt.orchestrator import Orchestrator
from wt.providers import postgres

pytestmark = pytest.mark.docker

BOOTSTRAP = Path(__file__).parents[2] / "examples" / "shop" / "bootstrap.sh"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def request(
    host: str, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """HTTPS request through Traefik, reusing the loopback + SNI connection of wt.health."""
    conn = health._LoopbackHTTPS(host, router.https_port(), 10.0)
    try:
        headers = {"Host": host}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"null")
    finally:
        conn.close()


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Orchestrator]:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.fail("docker is not available")
    instance = f"wtshop{free_port()}"
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_HOME", str(tmp_path / "wt-home"))
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("WT_POSTGRES_PORT", str(free_port()))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")  # no mkcert: self-signed path

    stack = tmp_path / "stack"
    subprocess.run([str(BOOTSTRAP), str(stack)], check=True, capture_output=True)
    config = stack / "worktree.yaml"
    # A unique project name keeps compose projects and images away from any real `shop` stack.
    config.write_text(config.read_text().replace("project: shop\n", f"project: {instance}\n"))
    orch = Orchestrator(load_config(config), log=lambda m: None)
    try:
        yield orch
    finally:
        for branch in list(orch.store.load().branches):
            with contextlib.suppress(WtError):
                orch.down(branch, force=True)
        docker("rm", "-f", "-v", router.container(), postgres.container())
        docker("volume", "rm", "-f", postgres.volume())
        docker("network", "rm", compose.network_name())
        # Compose names built images `<compose project>-<service>`, and the project embeds the
        # unique instance name, so this only matches images this test built.
        images = docker("image", "ls", "-q", "--filter", f"reference=*{instance}*").stdout.split()
        if images:
            docker("rmi", "-f", *images)


def up(orch: Orchestrator, branch: str, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        reports = orch.up(branch, timeout=180)
    except WtError as exc:
        if "not available" not in str(exc) and "already allocated" not in str(exc):
            raise
        monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
        monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
        monkeypatch.setenv("WT_POSTGRES_PORT", str(free_port()))
        docker("rm", "-f", "-v", postgres.container())
        reports = orch.up(branch, timeout=180)
    assert {r.service: r.outcome for r in reports} == {"api": "ok", "web": "ok"}, reports


def test_shop_branches_are_isolated(shop: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    domain = shop.cfg.domain
    up(shop, "main", monkeypatch)
    up(shop, "feat/cart", monkeypatch)

    main_api, cart_api = f"api.main.{domain}", f"api.feat-cart.{domain}"
    assert request(main_api, "GET", "/") == (200, {"branch": "main"})
    assert request(cart_api, "GET", "/") == (200, {"branch": "feat/cart"})

    _, main_items = request(main_api, "GET", "/items")
    _, cart_items = request(cart_api, "GET", "/items")
    assert len(main_items) == 3
    assert all("quantity" not in row for row in main_items)
    assert all(row["quantity"] == 1 for row in cart_items)

    status, created = request(cart_api, "POST", "/items", {"name": "cable", "quantity": 4})
    assert (status, created["name"], created["quantity"]) == (201, "cable", 4)
    assert "cable" in [row["name"] for row in request(cart_api, "GET", "/items")[1]]
    assert "cable" not in [row["name"] for row in request(main_api, "GET", "/items")[1]]

    for slug in ("main", "feat-cart"):
        assert health.probe(f"{slug}.{domain}", "/", router.https_port()).detail == "HTTP 200"

    shop.down("feat/cart")
    assert not health.probe(cart_api, "/healthz", router.https_port()).ok
    assert health.probe(main_api, "/healthz", router.https_port()).ok
    assert health.probe(f"main.{domain}", "/", router.https_port()).ok
