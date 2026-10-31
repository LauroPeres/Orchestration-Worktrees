import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from wt import compose, host, proc, router
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


PUBLISHED = "80/tcp -> 127.0.0.1:80\n443/tcp -> 127.0.0.1:443\n"


def test_ensure_running_streams_only_with_on_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WT_HOME", str(tmp_path))
    calls: list[dict[str, object]] = []

    def fake(args: list[str], **kwargs: object) -> proc.Result:
        if args[:2] == ["docker", "port"]:
            return proc.Result(0, PUBLISHED, "")
        calls.append(kwargs)
        return proc.Result(0, "", "")

    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr(proc, "run", fake)
    router.ensure_running()
    sink: Callable[[str], None] = print
    router.ensure_running(on_line=sink)
    assert calls[0]["capture"] is True and calls[0]["on_line"] is None
    assert calls[1]["capture"] is False and calls[1]["on_line"] is sink


def _fake_traefik(
    monkeypatch: pytest.MonkeyPatch, listing: dict[str, str], running: bool = True
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], **kw: object) -> proc.Result:
        calls.append(list(args))
        if args[:2] == ["docker", "inspect"]:
            return proc.Result(0, "true\n" if running else "false\n", "")
        if args[:2] == ["docker", "exec"]:
            return proc.Result(0, listing.get(args[-1], ""), "")
        if args[:2] == ["docker", "port"]:
            return proc.Result(0, PUBLISHED, "")
        return proc.Result(0, "", "")

    monkeypatch.setattr(proc, "run", run)
    return calls


def _host_files() -> None:
    router.dynamic_dir().mkdir(parents=True)
    router.certs_dir().mkdir(parents=True)
    (router.dynamic_dir() / "shop-main.yml").write_text("x")
    (router.certs_dir() / "wt.pem").write_text("x")


def test_missing_in_container_empty_when_all_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    _host_files()
    _fake_traefik(
        monkeypatch,
        {"/etc/traefik/dynamic": "shop-main.yml\n", "/etc/traefik/certs": "wt.pem\n"},
    )
    assert router.missing_in_container() == []


def test_missing_in_container_lists_unseen_files(monkeypatch: pytest.MonkeyPatch) -> None:
    _host_files()
    _fake_traefik(monkeypatch, {"/etc/traefik/certs": "wt.pem\n"})
    assert router.missing_in_container() == ["shop-main.yml"]


def test_missing_in_container_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    _host_files()
    calls = _fake_traefik(monkeypatch, {}, running=False)
    assert router.missing_in_container() == []
    assert not any(c[:2] == ["docker", "exec"] for c in calls)


def test_restart_recreates_container(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_traefik(monkeypatch, {})
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    router.restart()
    up = [c for c in calls if c[:2] == ["docker", "compose"]]
    assert len(up) == 1 and "--force-recreate" in up[0] and "up" in up[0]
    assert not any(c[:2] == ["docker", "restart"] for c in calls)


def test_ensure_running_does_not_force_recreate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_traefik(monkeypatch, {})
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    router.ensure_running()
    assert not any("--force-recreate" in c for c in calls)


def test_restart_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_traefik(monkeypatch, {}, running=False)
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(WtError, match="did not come back"):
        router.restart(timeout=0)


def _fake_traefik_ports(monkeypatch: pytest.MonkeyPatch, outputs: list[str]) -> list[list[str]]:
    """Fake docker where each `docker port` call returns the next of `outputs` (last repeats)."""
    calls: list[list[str]] = []
    pending = list(outputs)

    def run(args: list[str], **kw: object) -> proc.Result:
        calls.append(list(args))
        if args[:2] == ["docker", "port"]:
            return proc.Result(0, pending.pop(0) if len(pending) > 1 else pending[0], "")
        return proc.Result(0, "", "")

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    return calls


def _ups(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["docker", "compose"]]


def test_ensure_running_checks_the_configured_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_HTTP_PORT", "8080")
    monkeypatch.setenv("WT_HTTPS_PORT", "8443")
    calls = _fake_traefik_ports(
        monkeypatch, ["80/tcp -> 127.0.0.1:8080\n443/tcp -> 127.0.0.1:8443\n"]
    )
    router.ensure_running()
    assert len(_ups(calls)) == 1


def test_ensure_running_recreates_once_when_ports_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_traefik_ports(monkeypatch, ["", PUBLISHED])
    router.ensure_running()
    ups = _ups(calls)
    assert len(ups) == 2
    assert "--force-recreate" not in ups[0] and "--force-recreate" in ups[1]


def test_ensure_running_reports_a_port_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_traefik_ports(monkeypatch, ["443/tcp -> 127.0.0.1:443\n", ""])
    monkeypatch.setattr(host, "port_free", lambda port: False)
    with pytest.raises(WtError, match="port 80 is already in use by another program"):
        router.ensure_running()
    assert len(_ups(calls)) == 2


def test_ensure_running_reports_unpublished_port_when_it_is_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_traefik_ports(monkeypatch, [""])
    monkeypatch.setattr(host, "port_free", lambda port: True)
    with pytest.raises(WtError, match=f"{router.container()} does not publish port 80"):
        router.ensure_running()


def test_ensure_running_with_force_recreate_recreates_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_traefik_ports(monkeypatch, [PUBLISHED])
    router.ensure_running(force_recreate=True)
    assert [("--force-recreate" in up) for up in _ups(calls)] == [True]


def test_ensure_running_with_force_recreate_still_reports_missing_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_traefik_ports(monkeypatch, [""])
    monkeypatch.setattr(host, "port_free", lambda port: False)
    with pytest.raises(WtError, match="port 80 is already in use by another program"):
        router.ensure_running(force_recreate=True)
    assert len(_ups(calls)) == 1
