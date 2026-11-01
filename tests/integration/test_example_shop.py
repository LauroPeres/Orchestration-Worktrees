"""The examples/shop stack end to end against a real Docker daemon: `pytest -m docker`."""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from wt import compose, health, router
from wt.catalog import load_project
from wt.errors import WtError
from wt.orchestrator import Orchestrator
from wt.paths import project_dir
from wt.providers import kafka, keycloak, postgres, redis

from .isolation import reserve_free_indexes

pytestmark = pytest.mark.docker

USES = ["web", "worker"]  # web consumes api, so the whole stack is selected
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


def clean_up(catalog: Path, instance: str) -> None:
    """Take down every branch the test created, then the instance's shared containers."""
    orch = Orchestrator(load_project(catalog), log=lambda m: None)
    for branch in list(orch.store.load().branches):
        with contextlib.suppress(WtError):
            orch.down(branch, force=True)
    docker(
        "rm",
        "-f",
        "-v",
        router.container(),
        postgres.container(),
        redis.container(),
        kafka.container(),
        keycloak.container(),
    )
    docker(
        "volume",
        "rm",
        "-f",
        postgres.volume(),
        redis.volume(),
        kafka.volume(),
        keycloak.volume(),
    )
    docker("network", "rm", compose.network_name())
    # Compose names built images `<compose project>-<service>`, and the project embeds the
    # unique instance name, so this only matches images this test built.
    images = docker("image", "ls", "-q", "--filter", f"reference=*{instance}*").stdout.split()
    if images:
        docker("rmi", "-f", *images)


@pytest.fixture
def shop_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The bootstrapped shop catalog, isolated: own WT_HOME, instance name, ports and project."""
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.fail("docker is not available")
    instance = f"wtshop{free_port()}"
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_HOME", str(tmp_path / "wt-home"))
    reserve_free_indexes(tmp_path / "wt-home")
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("WT_POSTGRES_PORT", str(free_port()))
    monkeypatch.setenv("WT_REDIS_PORT", str(free_port()))
    monkeypatch.setenv("WT_KAFKA_PORT", str(free_port()))
    monkeypatch.setenv("WT_KEYCLOAK_PORT", str(free_port()))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")  # no mkcert: self-signed path

    stack = tmp_path / "stack"
    subprocess.run([str(BOOTSTRAP), str(stack)], check=True, capture_output=True)
    catalog = stack / "catalog.yaml"
    # A unique project name keeps compose projects and images away from any real `shop` stack.
    catalog.write_text(catalog.read_text().replace("project: shop\n", f"project: {instance}\n"))
    try:
        yield catalog
    finally:
        clean_up(catalog, instance)


@pytest.fixture
def shop(shop_catalog: Path) -> Orchestrator:
    return Orchestrator(load_project(shop_catalog), log=lambda m: None)


def up(orch: Orchestrator, branch: str, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        reports = orch.up(branch, uses=USES, timeout=180)
    except WtError as exc:
        if "not available" not in str(exc) and "already allocated" not in str(exc):
            raise
        monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
        monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
        monkeypatch.setenv("WT_POSTGRES_PORT", str(free_port()))
        monkeypatch.setenv("WT_REDIS_PORT", str(free_port()))
        monkeypatch.setenv("WT_KAFKA_PORT", str(free_port()))
        monkeypatch.setenv("WT_KEYCLOAK_PORT", str(free_port()))
        docker(
            "rm", "-f", "-v",
            postgres.container(), redis.container(), kafka.container(), keycloak.container(),
        )  # fmt: skip
        reports = orch.up(branch, uses=USES, timeout=180)
    assert {r.service: r.outcome for r in reports} == {"api": "ok", "web": "ok", "worker": "ok"}, (
        reports
    )


def redis_cli(*args: str) -> str:
    return docker("exec", redis.container(), "redis-cli", *args).stdout.strip()


def kafka_list(script: str) -> list[str]:
    return kafka.tool(script, "--list").stdout.split()


def keycloak_clients() -> dict[str, list[str]]:
    out = keycloak.kcadm("get", "clients", "-r", "shop", "--fields", "clientId,redirectUris")
    return {r["clientId"]: r["redirectUris"] for r in json.loads(out.stdout)}


def http_get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return int(resp.status), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def page_body(host: str) -> str:
    conn = health._LoopbackHTTPS(host, router.https_port(), 10.0)
    try:
        conn.request("GET", "/", headers={"Host": host})
        return conn.getresponse().read().decode()
    finally:
        conn.close()


def wait_counter(key: str, expected: str, timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    value = redis_cli("GET", key)
    while value != expected and time.monotonic() < deadline:
        time.sleep(1)
        value = redis_cli("GET", key)
    return value


def test_shop_branches_are_isolated(shop: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    domain = shop.cfg.domain
    project = shop.cfg.project  # the fixture renames it to keep the stack apart from a real `shop`
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

    # Events: each POST reaches the branch's worker through its own topic and group.
    request(cart_api, "POST", "/items", {"name": "plug", "quantity": 1})
    request(main_api, "POST", "/items", {"name": "lamp"})
    assert wait_counter(f"{project}:main:events", "1") == "1"
    assert wait_counter(f"{project}:feat-cart:events", "2") == "2"
    assert f"worker-{project}.feat-cart" in kafka_list("kafka-consumer-groups.sh")
    assert f"{project}.feat-cart.items" in kafka_list("kafka-topics.sh")

    # Keycloak: one client per branch, each with its own redirect URI, and a working login page.
    port = router.https_port()
    cart_web = f"https://feat-cart.{domain}:{port}"
    clients = keycloak_clients()
    assert set(clients[f"web-{project}.main"]) == {
        f"https://main.{domain}:{port}/*",
        f"https://api.main.{domain}:{port}/*",
    }
    assert f"{cart_web}/*" in clients[f"web-{project}.feat-cart"]
    query = urllib.parse.urlencode(
        {
            "client_id": f"web-{project}.feat-cart",
            "redirect_uri": cart_web + "/",
            "response_type": "code",
            "scope": "openid",
        }
    )
    status, login = http_get(f"{keycloak.url()}/realms/shop/protocol/openid-connect/auth?{query}")
    assert status == 200
    assert "kc-form-login" in login or 'name="username"' in login
    assert f"client_id=web-{project}.feat-cart" in page_body(f"feat-cart.{domain}")

    shop.down("feat/cart")
    clients = keycloak_clients()
    assert f"web-{project}.feat-cart" not in clients
    assert f"web-{project}.main" in clients
    realms = json.loads(keycloak.kcadm("get", "realms", "--fields", "realm").stdout)
    assert "shop" in [r["realm"] for r in realms]
    assert not health.probe(cart_api, "/healthz", router.https_port()).ok
    assert health.probe(main_api, "/healthz", router.https_port()).ok
    assert health.probe(f"main.{domain}", "/", router.https_port()).ok

    assert redis_cli("KEYS", f"{project}:feat-cart:*") == ""
    assert redis_cli("GET", f"{project}:main:events") == "1"
    assert f"worker-{project}.feat-cart" not in kafka_list("kafka-consumer-groups.sh")
    assert f"worker-{project}.main" in kafka_list("kafka-consumer-groups.sh")
    topics = kafka_list("kafka-topics.sh")
    assert not [t for t in topics if t.startswith(f"{project}.feat-cart.")]
    assert f"{project}.main.items" in topics


def wt(args: list[str], cwd: Path, catalog: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the real CLI from `cwd` (a folder with no project in it), as a user would."""
    env = {**os.environ, "COLUMNS": "200", "NO_COLOR": "1"}
    env.pop("WT_CATALOG", None)
    if catalog is not None:
        env["WT_CATALOG"] = str(catalog)
    return subprocess.run(
        [sys.executable, "-m", "wt", *args], cwd=cwd, env=env, capture_output=True, text=True
    )


def test_cli_from_outside_any_project_brings_up_the_dependency_closure(
    shop_catalog: Path, tmp_path: Path
) -> None:
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    domain = load_project(shop_catalog).config.domain

    # `worker` is selected too: api and worker share one compose file, and `compose up` starts
    # every service in it, so an unselected worker would run without its Kafka/Redis variables.
    added = wt(["add", "feat/x", "--uses", "web,worker"], nowhere, shop_catalog)
    assert added.returncode == 0, added.stderr
    state = Orchestrator(load_project(shop_catalog), log=lambda m: None).store.load()
    assert state.branches["feat/x"].uses == ["web", "worker"]
    assert set(state.branches["feat/x"].ports) == {"web", "api"}  # api comes in: web consumes it

    up_run = wt(["up", "feat/x", "--timeout", "180"], nowhere)  # no -c: WT_CATALOG is not set
    assert up_run.returncode != 0
    assert "no catalog at" in up_run.stderr + up_run.stdout

    up_run = wt(["up", "feat/x", "--timeout", "180"], nowhere, shop_catalog)
    assert up_run.returncode == 0, up_run.stdout + up_run.stderr
    assert health.probe(f"api.feat-x.{domain}", "/healthz", router.https_port()).ok
    assert health.probe(f"feat-x.{domain}", "/", router.https_port()).ok

    listing = wt(["ls"], nowhere, shop_catalog)
    assert listing.returncode == 0, listing.stderr
    assert "feat/x" in listing.stdout
    assert "api" in listing.stdout
    assert "web" in listing.stdout


def test_up_without_a_selection_fails_and_creates_nothing(
    shop_catalog: Path, tmp_path: Path
) -> None:
    result = wt(["up", "other"], tmp_path, shop_catalog)

    assert result.returncode == 1
    assert "branch other has no services yet; run wt add other --uses api (services: api" in (
        result.stdout + result.stderr
    )
    orch = Orchestrator(load_project(shop_catalog), log=lambda m: None)
    assert orch.store.load().branches == {}
    assert not (project_dir(orch.cfg.project) / "worktrees").exists()
    assert docker("ps", "-aq", "--filter", f"name={orch.cfg.project}").stdout.strip() == ""


def service_summary(catalog: Path) -> dict[str, tuple[str, ...]]:
    """Service name -> sorted infra providers, read from the catalog file."""
    raw = yaml.safe_load(catalog.read_text())["services"]
    return {name: tuple(sorted(svc.get("infra", {}))) for name, svc in raw.items()}


def test_migrate_the_legacy_shop_file_matches_the_catalog_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = tmp_path / "stack"
    subprocess.run([str(BOOTSTRAP), str(stack)], check=True, capture_output=True)
    wt_home = tmp_path / "empty-home"
    monkeypatch.setenv("WT_HOME", str(wt_home))
    monkeypatch.delenv("WT_CATALOG", raising=False)
    legacy = stack / "legacy" / "worktree.yaml"
    migrated = wt_home / "catalog.yaml"

    first = wt(["migrate", str(legacy)], tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    for name in ("api", "worker", "web"):
        assert f"added service {name}" in first.stdout
    written = yaml.safe_load(migrated.read_text())
    reference = yaml.safe_load((stack / "catalog.yaml").read_text())
    assert (written["project"], written["domain"]) == (reference["project"], reference["domain"])
    assert set(written["services"]) == set(reference["services"])
    assert written["repos"] == {
        "api": str((stack / "shop-api").resolve()),
        "web": str((stack / "shop-web").resolve()),
    }
    # The loaded stack is the same one the hand-written catalog describes.
    # (A migrated web keeps its explicit API_URL, so it does not pull the api in by itself.)
    everything = ["api", "worker", "web"]
    ours = load_project(migrated, uses=everything).config
    expected = load_project(stack / "catalog.yaml", uses=everything).config
    assert set(ours.services) == set(expected.services)
    assert ours.shared.model_dump() == expected.shared.model_dump()
    assert ours.shared.keycloak is not None and ours.shared.keycloak.realm == "shop"
    assert service_summary(migrated)["web"] == ("keycloak",)

    before = migrated.read_bytes()
    second = wt(["migrate", str(legacy)], tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "added service" not in second.stdout
    assert second.stdout.count("skipped service") == 3
    assert migrated.read_bytes() == before
