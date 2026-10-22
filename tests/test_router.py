from pathlib import Path

import pytest
import yaml

from wt import router
from wt.errors import WtError
from wt.naming import Names

NAMES = Names("shop", "feat/cart", "shop.localhost")
ROUTES = [router.Route("api", "api.feat-cart.shop.localhost", "http://wt-shop-feat-cart-api:8000")]


def test_route_doc() -> None:
    doc = router.route_doc(NAMES, ROUTES, None)
    r = doc["http"]["routers"]["shop--feat-cart--api"]
    assert r["rule"] == "Host(`api.feat-cart.shop.localhost`)"
    assert r["entryPoints"] == ["websecure"]
    assert r["tls"] == {}
    servers = doc["http"]["services"]["shop--feat-cart--api"]["loadBalancer"]["servers"]
    assert servers == [{"url": "http://wt-shop-feat-cart-api:8000"}]
    assert "tls" not in doc


def test_route_doc_with_cert() -> None:
    cert = (Path("/x/shop--feat-cart.pem"), Path("/x/shop--feat-cart-key.pem"))
    doc = router.route_doc(NAMES, ROUTES, cert)
    assert doc["tls"]["certificates"] == [
        {
            "certFile": "/etc/traefik/certs/shop--feat-cart.pem",
            "keyFile": "/etc/traefik/certs/shop--feat-cart-key.pem",
        }
    ]


def test_write_and_remove_routes(isolated_home: Path) -> None:
    path = router.write_routes(NAMES, ROUTES, None)
    assert path == isolated_home / "traefik" / "dynamic" / "shop--feat-cart.yml"
    assert yaml.safe_load(path.read_text())["http"]["routers"]
    assert [p.name for p in path.parent.iterdir()] == ["shop--feat-cart.yml"]  # no temp files
    router.remove_routes(path)
    router.remove_routes(path)  # idempotent
    assert not path.exists()


def test_traefik_compose_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_HTTPS_PORT", "8443")
    monkeypatch.setenv("WT_HTTP_PORT", "8080")
    svc = router.compose_doc()["services"]["traefik"]
    assert svc["ports"] == ["127.0.0.1:8080:80", "127.0.0.1:8443:443"]
    assert "--entrypoints.web.http.redirections.entrypoint.to=:8443" in svc["command"]


def test_traefik_default_redirect() -> None:
    cmd = router.compose_doc()["services"]["traefik"]["command"]
    assert "--entrypoints.web.http.redirections.entrypoint.to=websecure" in cmd


def test_traefik_reaches_host_processes() -> None:
    svc = router.compose_doc()["services"]["traefik"]
    assert svc["extra_hosts"] == ["host.docker.internal:host-gateway"]


def test_host_upstream() -> None:
    assert router.host_upstream(23456) == "http://host.docker.internal:23456"


@pytest.mark.parametrize(
    ("var", "fn"), [("WT_HTTP_PORT", router.http_port), ("WT_HTTPS_PORT", router.https_port)]
)
@pytest.mark.parametrize("bad", ["abc", "", "0", "70000", "-1"])
def test_invalid_port_variable(
    monkeypatch: pytest.MonkeyPatch, var: str, fn: object, bad: str
) -> None:
    monkeypatch.setenv(var, bad)
    with pytest.raises(WtError, match=var):
        fn()  # type: ignore[operator]
