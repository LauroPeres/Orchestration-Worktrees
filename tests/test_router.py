import http.client
import http.server
import ssl
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from wt import compose, health, host, proc, router
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


def _write_route_file(project: str, branch: str, *rules: str) -> Path:
    """A route file shaped like `write_routes` writes it, with the given router rules."""
    names = Names(project, branch, "shop.localhost")
    path = router.write_routes(names, [], None)
    routers = {
        f"r{i}": {"rule": rule, "entryPoints": ["websecure"], "service": f"r{i}", "tls": {}}
        for i, rule in enumerate(rules)
    }
    path.write_text(yaml.safe_dump({"http": {"routers": routers, "services": {}}}))
    return path


def test_hosts_in_use_empty_folder(isolated_home: Path) -> None:
    assert router.hosts_in_use() == {}  # no folder yet
    router.dynamic_dir().mkdir(parents=True)
    assert router.hosts_in_use() == {}


def test_hosts_in_use_reads_every_file(isolated_home: Path) -> None:
    a = _write_route_file("shop", "main", "Host(`main.shop.localhost`)")
    b = _write_route_file("lim", "dev", "Host(`dev.lim.localhost`)")
    assert router.hosts_in_use() == {"main.shop.localhost": a, "dev.lim.localhost": b}


def test_hosts_in_use_from_write_routes(isolated_home: Path) -> None:
    path = router.write_routes(NAMES, ROUTES, None)
    assert router.hosts_in_use() == {"api.feat-cart.shop.localhost": path}


def test_hosts_in_use_same_host_shows_the_other_file_when_own_is_excluded(
    isolated_home: Path,
) -> None:
    mine = _write_route_file("shop", "main", "Host(`main.shop.localhost`)")
    other = _write_route_file("lim", "main", "Host(`main.shop.localhost`)")
    assert router.hosts_in_use(exclude=mine) == {"main.shop.localhost": other}
    assert router.hosts_in_use(exclude=other) == {"main.shop.localhost": mine}


def test_hosts_in_use_excludes_own_file(isolated_home: Path) -> None:
    mine = _write_route_file("shop", "main", "Host(`main.shop.localhost`)")
    assert router.hosts_in_use(exclude=mine) == {}


def test_hosts_in_use_reads_every_host_of_a_rule(isolated_home: Path) -> None:
    path = _write_route_file(
        "shop",
        "main",
        "Host(`a.localhost`) || Host(`b.localhost`)",
        "Host(`c.localhost`) && PathPrefix(`/x`)",
    )
    assert router.hosts_in_use() == {
        "a.localhost": path,
        "b.localhost": path,
        "c.localhost": path,
    }


def test_hosts_in_use_ignores_unreadable_files(isolated_home: Path) -> None:
    good = _write_route_file("shop", "main", "Host(`ok.localhost`)")
    folder = router.dynamic_dir()
    (folder / "bad--yaml.yml").write_text("http: [unclosed\n  - : :")
    (folder / "binary--x.yml").write_bytes(b"\xff\xfe\x00")
    (folder / "list--x.yml").write_text("- Host(`list.localhost`)\n")
    (folder / "routers--x.yml").write_text("http:\n  routers: [1, 2]\n")
    (folder / "norule--x.yml").write_text("http:\n  routers:\n    r: {rule: 3}\n    s: 4\n")
    (folder / "notes.txt").write_text("Host(`txt.localhost`)")
    assert router.hosts_in_use() == {"ok.localhost": good}


def test_hosts_in_use_compares_case_insensitively(isolated_home: Path) -> None:
    path = _write_route_file("shop", "main", "Host(`Main.Shop.Localhost`)")
    assert router.hosts_in_use() == {"main.shop.localhost": path}


@pytest.mark.parametrize(
    ("file_name", "owner"),
    [
        ("shop--feat-cart.yml", "shop/feat-cart"),
        ("my--shop--main.yml", "my--shop/main"),
        ("handwritten.yml", "handwritten"),
        ("--main.yml", "--main"),
    ],
)
def test_route_file_owner(file_name: str, owner: str) -> None:
    assert router.route_file_owner(Path("/x") / file_name) == owner


class _StatusServer:
    """Local plain-HTTP server answering `statuses` in order, then `last` forever."""

    def __init__(self, statuses: list[int], last: int) -> None:
        self.hits: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.hits.append(self.headers.get("Host", ""))
                self.send_response(statuses.pop(0) if statuses else last)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                """Keep test output quiet."""

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def fetch(self, host: str, timeout: float) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request("GET", "/", headers={"Host": host})
            return conn.getresponse().status
        finally:
            conn.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def traefik_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "is_running", lambda: True)


def test_wait_unrouted_true_when_traefik_is_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router, "is_running", lambda: False)

    def must_not_probe(host: str, timeout: float) -> int:
        raise AssertionError("probed although Traefik is not running")

    assert router.wait_unrouted(["a.x.localhost"], fetch_status=must_not_probe)


@pytest.mark.usefixtures("traefik_running")
def test_wait_unrouted_waits_until_the_route_answers_404() -> None:
    server = _StatusServer([200, 502, 200], last=404)
    try:
        done = router.wait_unrouted(["a.x.localhost"], 3.0, 0.01, fetch_status=server.fetch)
    finally:
        server.close()
    assert done
    assert len(server.hits) == 4
    assert set(server.hits) == {"a.x.localhost"}


@pytest.mark.usefixtures("traefik_running")
def test_wait_unrouted_needs_every_host() -> None:
    statuses = {"a.x.localhost": [404], "b.x.localhost": [502, 502, 404]}

    def fetch(host: str, timeout: float) -> int:
        queue = statuses[host]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    assert router.wait_unrouted(list(statuses), 3.0, 0.01, fetch_status=fetch)
    assert statuses["b.x.localhost"] == [404]


@pytest.mark.usefixtures("traefik_running")
@pytest.mark.parametrize("still_routed", [200, 301, 502, 503])
def test_wait_unrouted_times_out_while_the_route_answers(still_routed: int) -> None:
    server = _StatusServer([], last=still_routed)
    start = time.monotonic()
    try:
        done = router.wait_unrouted(["a.x.localhost"], 0.3, 0.02, fetch_status=server.fetch)
    finally:
        server.close()
    assert not done
    assert time.monotonic() - start < 2.0


@pytest.mark.usefixtures("traefik_running")
@pytest.mark.parametrize("error", [ConnectionRefusedError, ConnectionResetError])
def test_wait_unrouted_counts_refused_and_reset_as_unrouted(error: type[OSError]) -> None:
    def fetch(host: str, timeout: float) -> int:
        raise error

    assert router.wait_unrouted(["a.x.localhost"], 0.3, 0.01, fetch_status=fetch)


@pytest.mark.usefixtures("traefik_running")
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        OSError(),
        ssl.SSLError(),
        http.client.BadStatusLine("x"),
    ],
)
def test_wait_unrouted_retries_other_errors_until_timeout(error: Exception) -> None:
    calls: list[str] = []

    def fetch(host: str, timeout: float) -> int:
        calls.append(host)
        raise error

    assert not router.wait_unrouted(["a.x.localhost"], 0.2, 0.01, fetch_status=fetch)
    assert len(calls) > 1


@pytest.mark.usefixtures("traefik_running")
def test_wait_unrouted_default_probe_hits_the_https_port(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int, float]] = []

    def fake_status(host: str, path: str, port: int, timeout: float) -> int:
        seen.append((host, port, timeout))
        return 404

    monkeypatch.setenv("WT_HTTPS_PORT", "8443")
    monkeypatch.setattr(health, "status", fake_status)
    assert router.wait_unrouted(["a.x.localhost"])
    assert seen == [("a.x.localhost", 8443, router.UNROUTED_REQUEST_TIMEOUT)]
