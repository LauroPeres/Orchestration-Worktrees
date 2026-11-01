import copy
import socket
from collections.abc import Callable, Mapping

import pytest

from tests.conftest import SHOP_CATALOG, write_global_catalog
from wt import compose, doctor, host, proc, router
from wt.config import LoadedConfig
from wt.errors import WtError
from wt.providers import kafka, keycloak, postgres, redis
from wt.state import BranchState, ProcessState, StateStore

OK = proc.Result(0, "", "")
TRAEFIK_PORTS = "80/tcp -> 127.0.0.1:80\n443/tcp -> 127.0.0.1:443\n"


def _answer(running: bool, ports: set[int]) -> Callable[..., proc.Result]:
    """Fake `proc.run` for a container: `docker inspect` says running, `docker port` its ports."""

    def run(args: list[str], **kw: object) -> proc.Result:
        if args[:2] == ["docker", "port"]:
            return proc.Result(0, "".join(f"{p}/tcp -> 127.0.0.1:{p}\n" for p in sorted(ports)), "")
        return proc.Result(0, "true\n" if running else "false\n", "")

    return run


def _traefik_answers(args: list[str], **kw: object) -> proc.Result:
    """Fake `proc.run` for a Traefik that publishes its HTTP and HTTPS ports."""
    return proc.Result(0, TRAEFIK_PORTS if args[:2] == ["docker", "port"] else "", "")


def _fake_docker(compose_version: str | None) -> Callable[..., proc.Result]:
    def fake_run(args: list[str], **kwargs: object) -> proc.Result:
        if args[:2] == ["docker", "info"]:
            return proc.Result(0, "27.0.1\n", "")
        if args[:3] == ["docker", "compose", "version"]:
            if compose_version is None:
                return proc.Result(1, "", "err")
            return proc.Result(0, compose_version + "\n", "")
        return OK

    return fake_run


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2.24.5", (2, 24, 5)), ("v2.30", (2, 30)), ("Docker Compose version v2.20.1", (2, 20, 1))],
)
def test_version_parsing(text: str, expected: tuple[int, ...]) -> None:
    assert doctor._version(text) == expected


def test_version_garbage_is_empty() -> None:
    assert doctor._version("nope") == ()


@pytest.mark.parametrize(("version", "level"), [("2.23.9", "fail"), ("2.24.0", "ok")])
def test_compose_version_levels(monkeypatch: pytest.MonkeyPatch, version: str, level: str) -> None:
    monkeypatch.setattr(proc, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(proc, "run", _fake_docker(version))
    compose = next(c for c in doctor.check_docker() if c.name == "compose")
    assert compose.level == level


def test_compose_missing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(proc, "run", _fake_docker(None))
    compose = next(c for c in doctor.check_docker() if c.name == "compose")
    assert compose.level == "fail"


def test_resolver_warns_with_scoped_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(*a: object, **k: object) -> list[tuple[object, ...]]:
        return [(0, 0, 0, "", ("192.168.1.9", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    check = doctor.check_resolution("test.localhost")
    assert check.level == "warn"
    assert check.hint == (
        "browsers, curl and wt's containers are fine; only other local programs may fail "
        "(fix with /etc/hosts, dnsmasq or systemd-resolved)"
    )


def test_port_taken_fails_with_env_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "run", lambda args, **kw: proc.Result(1, "", ""))
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: port == router.http_port())
    checks = {c.name: c for c in doctor.check_routing(fix=False)}
    assert checks["http port"].level == "fail"
    assert "WT_HTTP_PORT" in checks["http port"].hint
    assert checks["https port"].level == "ok"


def test_fix_does_not_start_traefik_when_ports_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(proc, "run", lambda args, **kw: proc.Result(1, "", ""))
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr(router, "ensure_running", lambda *a, **k: started.append(True))
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: True)
    checks = {c.name: c for c in doctor.check_routing(fix=True)}
    assert started == []
    assert checks["http port"].level == "fail"
    assert "WT_HTTP_PORT" in checks["http port"].hint
    assert checks["https port"].level == "fail"
    assert checks["traefik"].level == "warn"


def test_fix_starts_traefik_when_ports_free(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"running": False}

    def start(*a: object, **k: object) -> None:
        state["running"] = True

    monkeypatch.setattr(proc, "run", _traefik_answers)
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr(router, "is_running", lambda: state["running"])
    monkeypatch.setattr(router, "ensure_running", start)
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: False)
    monkeypatch.setattr(doctor, "_traefik_ports", lambda: "")
    monkeypatch.setattr(router, "has_host_gateway", lambda: True)
    checks = {c.name: c for c in doctor.check_routing(fix=True)}
    assert checks["traefik"].level == "ok"


def _stale_traefik(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    state = {"gateway": False}

    def recreate(*a: object, **k: object) -> None:
        state["gateway"] = True

    monkeypatch.setattr(proc, "run", _traefik_answers)
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr(router, "is_running", lambda: True)
    monkeypatch.setattr(router, "has_host_gateway", lambda: state["gateway"])
    monkeypatch.setattr(router, "ensure_running", recreate)
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: True)
    monkeypatch.setattr(doctor, "_traefik_ports", lambda: "")
    return state


def test_stale_traefik_warns_without_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _stale_traefik(monkeypatch)
    check = _by_name(doctor.check_routing(fix=False))["traefik"]
    assert check.level == "warn"
    assert "host.docker.internal" in check.detail
    assert "wt doctor --fix" in check.hint and "wt up" in check.hint
    assert state["gateway"] is False


def test_stale_traefik_is_recreated_by_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _stale_traefik(monkeypatch)
    assert _by_name(doctor.check_routing(fix=True))["traefik"].level == "ok"
    assert state["gateway"] is True


def test_has_host_gateway_reads_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    out = '["host.docker.internal:host-gateway"]'
    monkeypatch.setattr(proc, "run", lambda args, **kw: proc.Result(0, out, ""))
    assert router.has_host_gateway() is True
    monkeypatch.setattr(proc, "run", lambda args, **kw: proc.Result(0, "null", ""))
    assert router.has_host_gateway() is False


# ---- shared Postgres and recorded ports ------------------------------------------------


class PgFake:
    """Stands in for the container and the provider helpers."""

    def __init__(self, *, running: bool, closed: list[str], published: bool = True) -> None:
        self.running = running
        self.ports = {postgres.host_port()} if published else set()
        self.closed = closed
        self.ensured: list[list[str]] = []
        self.repaired = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run(args: list[str], **kw: object) -> proc.Result:
            return _answer(self.running, self.ports)(args, **kw)

        def ensure(databases: list[str], timeout: int = 60) -> None:
            self.ensured.append(databases)
            self.running = True
            self.ports = {postgres.host_port()}
            self.repair(databases)

        monkeypatch.setattr(proc, "run", run)
        monkeypatch.setattr(postgres, "ensure_server", ensure)
        monkeypatch.setattr(postgres, "closed_databases", lambda dbs: list(self.closed))
        monkeypatch.setattr(postgres, "repair_connections", self.repair)

    def repair(self, databases: list[str]) -> None:
        self.repaired += 1
        self.closed = []


def _by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def test_postgres_all_good(monkeypatch: pytest.MonkeyPatch) -> None:
    PgFake(running=True, closed=[]).install(monkeypatch)
    password = postgres.password_file()
    password.parent.mkdir(parents=True)
    password.write_text("s3cret\n")
    password.chmod(0o600)
    checks = _by_name(doctor.check_postgres(["shop"], fix=False))
    assert {c.level for c in checks.values()} == {"ok"}
    assert all("s3cret" not in f"{c.detail}{c.hint}" for c in checks.values())


def test_postgres_down_warns_without_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = PgFake(running=False, closed=[])
    fake.install(monkeypatch)
    checks = _by_name(doctor.check_postgres(["shop"], fix=False))
    assert checks["postgres"].level == "warn"
    assert "postgres databases" not in checks
    assert checks["postgres password"].level == "info"
    assert fake.ensured == []


def test_postgres_fix_starts_server(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = PgFake(running=False, closed=[])
    fake.install(monkeypatch)
    checks = _by_name(doctor.check_postgres(["shop"], fix=True))
    assert fake.ensured == [["shop"]]
    assert checks["postgres"].level == "ok"


def test_postgres_fix_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    PgFake(running=False, closed=[]).install(monkeypatch)

    def boom(databases: list[str], timeout: int = 60) -> None:
        raise WtError("compose failed\nmore")

    monkeypatch.setattr(postgres, "ensure_server", boom)
    checks = _by_name(doctor.check_postgres(["shop"], fix=True))
    assert checks["postgres"].level == "fail"
    assert checks["postgres"].detail == "compose failed"


def test_postgres_password_loose_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    PgFake(running=True, closed=[]).install(monkeypatch)
    password = postgres.password_file()
    password.parent.mkdir(parents=True)
    password.write_text("s3cret\n")
    password.chmod(0o644)
    check = _by_name(doctor.check_postgres(["shop"], fix=False))["postgres password"]
    assert check.level == "warn"
    assert "0644" in check.detail
    fixed = _by_name(doctor.check_postgres(["shop"], fix=True))["postgres password"]
    assert fixed.level == "ok"
    assert password.stat().st_mode & 0o777 == 0o600


def test_postgres_closed_databases_fail_then_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = PgFake(running=True, closed=["shop__feat_a"])
    fake.install(monkeypatch)
    check = _by_name(doctor.check_postgres(["shop"], fix=False))["postgres databases"]
    assert check.level == "fail"
    assert "shop__feat_a" in check.detail
    fixed = _by_name(doctor.check_postgres(["shop"], fix=True))["postgres databases"]
    assert fixed.level == "ok"
    assert fake.repaired == 1


class RedisFake:
    """Stands in for the container and the server start."""

    def __init__(self, *, running: bool, published: bool = True) -> None:
        self.running = running
        self.ports = {redis.host_port()} if published else set()
        self.ensured = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run(args: list[str], **kw: object) -> proc.Result:
            return _answer(self.running, self.ports)(args, **kw)

        def ensure() -> None:
            self.ensured += 1
            self.running = True
            self.ports = {redis.host_port()}

        monkeypatch.setattr(proc, "run", run)
        monkeypatch.setattr(redis, "ensure_server", ensure)


def test_redis_running_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = RedisFake(running=True)
    fake.install(monkeypatch)
    check = doctor.check_redis(fix=False)
    assert check.level == "ok"
    assert fake.ensured == 0


def test_redis_down_fails_with_hint_without_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = RedisFake(running=False)
    fake.install(monkeypatch)
    check = doctor.check_redis(fix=False)
    assert check.level == "fail"
    assert "wt doctor --fix" in check.hint
    assert fake.ensured == 0


def test_redis_fix_starts_server(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = RedisFake(running=False)
    fake.install(monkeypatch)
    check = doctor.check_redis(fix=True)
    assert fake.ensured == 1
    assert check.level == "ok"


def test_redis_fix_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    RedisFake(running=False).install(monkeypatch)

    def boom() -> None:
        raise WtError("compose failed\nmore")

    monkeypatch.setattr(redis, "ensure_server", boom)
    check = doctor.check_redis(fix=True)
    assert check.level == "fail"
    assert check.detail == "compose failed"


class KafkaFake:
    """Stands in for the container and the server start."""

    def __init__(self, *, running: bool, published: bool = True) -> None:
        self.running = running
        self.ports = {kafka.host_port()} if published else set()
        self.ensured = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run(args: list[str], **kw: object) -> proc.Result:
            return _answer(self.running, self.ports)(args, **kw)

        def ensure() -> None:
            self.ensured += 1
            self.running = True
            self.ports = {kafka.host_port()}

        monkeypatch.setattr(proc, "run", run)
        monkeypatch.setattr(kafka, "ensure_server", ensure)


def test_kafka_running_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KafkaFake(running=True)
    fake.install(monkeypatch)
    check = doctor.check_kafka(fix=False)
    assert check.level == "ok"
    assert fake.ensured == 0


def test_kafka_down_fails_with_hint_without_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KafkaFake(running=False)
    fake.install(monkeypatch)
    check = doctor.check_kafka(fix=False)
    assert check.level == "fail"
    assert "wt doctor --fix" in check.hint
    assert fake.ensured == 0


def test_kafka_fix_starts_server(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KafkaFake(running=False)
    fake.install(monkeypatch)
    check = doctor.check_kafka(fix=True)
    assert fake.ensured == 1
    assert check.level == "ok"


def test_kafka_fix_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    KafkaFake(running=False).install(monkeypatch)

    def boom() -> None:
        raise WtError("compose failed\nmore")

    monkeypatch.setattr(kafka, "ensure_server", boom)
    check = doctor.check_kafka(fix=True)
    assert check.level == "fail"
    assert check.detail == "compose failed"


class KeycloakFake:
    """Stands in for the container and the server start."""

    def __init__(self, *, running: bool, published: bool = True) -> None:
        self.running = running
        self.ports = {keycloak.host_port()} if published else set()
        self.ensured = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run(args: list[str], **kw: object) -> proc.Result:
            return _answer(self.running, self.ports)(args, **kw)

        def ensure(realm: str | None = None, timeout: int = 120) -> None:
            self.ensured += 1
            self.running = True
            self.ports = {keycloak.host_port()}

        monkeypatch.setattr(proc, "run", run)
        monkeypatch.setattr(keycloak, "ensure_server", ensure)


def test_keycloak_running_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeycloakFake(running=True)
    fake.install(monkeypatch)
    check = doctor.check_keycloak(fix=False)
    assert check.level == "ok"
    assert fake.ensured == 0


def test_keycloak_down_fails_with_hint_without_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeycloakFake(running=False)
    fake.install(monkeypatch)
    check = doctor.check_keycloak(fix=False)
    assert check.level == "fail"
    assert "wt doctor --fix" in check.hint
    assert fake.ensured == 0


def test_keycloak_fix_starts_server(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeycloakFake(running=False)
    fake.install(monkeypatch)
    check = doctor.check_keycloak(fix=True)
    assert fake.ensured == 1
    assert check.level == "ok"


def test_keycloak_fix_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    KeycloakFake(running=False).install(monkeypatch)

    def boom(realm: str | None = None, timeout: int = 120) -> None:
        raise WtError("compose failed\nmore")

    monkeypatch.setattr(keycloak, "ensure_server", boom)
    check = doctor.check_keycloak(fix=True)
    assert check.level == "fail"
    assert check.detail == "compose failed"


def _loaded(make_repo, infra: Mapping[str, object] | None) -> LoadedConfig:
    """A one-service catalog (repo `app`) whose service needs `infra`."""
    repo = make_repo("app", {"docker-compose.yml": "services: {app: {image: x}}\n"})
    service = {"repo": "app", "compose": "docker-compose.yml", "port": 8000, "subdomain": ""}
    catalog = {
        "project": "app",
        "domain": "app.localhost",
        "repos": {"app": "./app"},
        "services": {"app": {**service, **({"infra": infra} if infra else {})}},
    }
    return write_global_catalog(repo.parent, catalog)


@pytest.mark.parametrize("with_postgres", [False, True])
def test_run_checks_only_checks_postgres_when_configured(
    monkeypatch: pytest.MonkeyPatch, make_repo, with_postgres: bool
) -> None:
    loaded = _loaded(make_repo, {"postgres": {"databases": ["shop"]}} if with_postgres else None)
    calls: list[list[str]] = []
    monkeypatch.setattr(doctor, "check_git", lambda: doctor.Check("git", "ok", ""))
    monkeypatch.setattr(doctor, "check_docker", lambda: [doctor.Check("docker", "ok", "")])
    monkeypatch.setattr(doctor, "check_tls", lambda fix: doctor.Check("mkcert", "ok", ""))
    monkeypatch.setattr(doctor, "check_routing", lambda fix: [])
    monkeypatch.setattr(doctor, "check_resolution", lambda d: doctor.Check("resolver", "ok", ""))

    def fake_pg(dbs: list[str], fix: bool) -> list[doctor.Check]:
        calls.append(dbs)
        return [doctor.Check("pg", "ok", "")]

    monkeypatch.setattr(doctor, "check_postgres", fake_pg)
    names = [c.name for c in doctor.run_checks(loaded, None, fix=False)]
    assert ("pg" in names) is with_postgres
    assert calls == ([["shop"]] if with_postgres else [])


@pytest.mark.parametrize("with_redis", [False, True])
def test_run_checks_only_checks_redis_when_configured(
    monkeypatch: pytest.MonkeyPatch, make_repo, with_redis: bool
) -> None:
    loaded = _loaded(make_repo, {"redis": {"prefix": True}} if with_redis else None)
    calls: list[bool] = []
    monkeypatch.setattr(doctor, "check_git", lambda: doctor.Check("git", "ok", ""))
    monkeypatch.setattr(doctor, "check_docker", lambda: [doctor.Check("docker", "ok", "")])
    monkeypatch.setattr(doctor, "check_tls", lambda fix: doctor.Check("mkcert", "ok", ""))
    monkeypatch.setattr(doctor, "check_routing", lambda fix: [])
    monkeypatch.setattr(doctor, "check_resolution", lambda d: doctor.Check("resolver", "ok", ""))

    def fake_redis(*, fix: bool) -> doctor.Check:
        calls.append(fix)
        return doctor.Check("redis", "ok", "")

    monkeypatch.setattr(doctor, "check_redis", fake_redis)
    names = [c.name for c in doctor.run_checks(loaded, None, fix=True)]
    assert ("redis" in names) is with_redis
    assert calls == ([True] if with_redis else [])


@pytest.mark.parametrize("with_kafka", [False, True])
def test_run_checks_only_checks_kafka_when_configured(
    monkeypatch: pytest.MonkeyPatch, make_repo, with_kafka: bool
) -> None:
    loaded = _loaded(make_repo, {"kafka": {"groups": ["worker"]}} if with_kafka else None)
    calls: list[bool] = []
    monkeypatch.setattr(doctor, "check_git", lambda: doctor.Check("git", "ok", ""))
    monkeypatch.setattr(doctor, "check_docker", lambda: [doctor.Check("docker", "ok", "")])
    monkeypatch.setattr(doctor, "check_tls", lambda fix: doctor.Check("mkcert", "ok", ""))
    monkeypatch.setattr(doctor, "check_routing", lambda fix: [])
    monkeypatch.setattr(doctor, "check_resolution", lambda d: doctor.Check("resolver", "ok", ""))

    def fake_kafka(*, fix: bool) -> doctor.Check:
        calls.append(fix)
        return doctor.Check("kafka", "ok", "")

    monkeypatch.setattr(doctor, "check_kafka", fake_kafka)
    names = [c.name for c in doctor.run_checks(loaded, None, fix=True)]
    assert ("kafka" in names) is with_kafka
    assert calls == ([True] if with_kafka else [])


@pytest.mark.parametrize("with_keycloak", [False, True])
def test_run_checks_only_checks_keycloak_when_configured(
    monkeypatch: pytest.MonkeyPatch, make_repo, with_keycloak: bool
) -> None:
    keycloak = {"keycloak": {"realm": "shop", "client": "web"}}
    loaded = _loaded(make_repo, keycloak if with_keycloak else None)
    calls: list[bool] = []
    monkeypatch.setattr(doctor, "check_git", lambda: doctor.Check("git", "ok", ""))
    monkeypatch.setattr(doctor, "check_docker", lambda: [doctor.Check("docker", "ok", "")])
    monkeypatch.setattr(doctor, "check_tls", lambda fix: doctor.Check("mkcert", "ok", ""))
    monkeypatch.setattr(doctor, "check_routing", lambda fix: [])
    monkeypatch.setattr(doctor, "check_resolution", lambda d: doctor.Check("resolver", "ok", ""))

    def fake_keycloak(*, fix: bool) -> doctor.Check:
        calls.append(fix)
        return doctor.Check("keycloak", "ok", "")

    monkeypatch.setattr(doctor, "check_keycloak", fake_keycloak)
    names = [c.name for c in doctor.run_checks(loaded, None, fix=True)]
    assert ("keycloak" in names) is with_keycloak
    assert calls == ([True] if with_keycloak else [])


def _stub_run_checks_environment(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub everything but the shared-provider checks; return the names they get called by."""
    called: list[str] = []
    monkeypatch.setattr(doctor, "check_git", lambda: doctor.Check("git", "ok", ""))
    monkeypatch.setattr(doctor, "check_docker", lambda: [doctor.Check("docker", "ok", "")])
    monkeypatch.setattr(doctor, "check_tls", lambda fix: doctor.Check("mkcert", "ok", ""))
    monkeypatch.setattr(doctor, "check_routing", lambda fix: [])
    monkeypatch.setattr(doctor, "check_resolution", lambda d: doctor.Check("resolver", "ok", ""))

    def fake_postgres(dbs: list[str], fix: bool) -> list[doctor.Check]:
        called.append("postgres:" + ",".join(dbs))
        return [doctor.Check("postgres", "ok", "")]

    def fake_server(name: str) -> Callable[..., doctor.Check]:
        def check(*, fix: bool) -> doctor.Check:
            called.append(name)
            return doctor.Check(name, "ok", "")

        return check

    monkeypatch.setattr(doctor, "check_postgres", fake_postgres)
    for name in ("redis", "kafka", "keycloak"):
        monkeypatch.setattr(doctor, f"check_{name}", fake_server(name))
    return called


def _infra_catalog(infra: dict[str, dict[str, object]]) -> dict[str, object]:
    """SHOP_CATALOG with `infra` per service name (back is the anchor: first in catalog order)."""
    services = copy.deepcopy(SHOP_CATALOG["services"])
    assert isinstance(services, dict)
    for name, value in infra.items():
        services[name]["infra"] = value
    return {**SHOP_CATALOG, "services": services}


def test_catalog_run_checks_covers_the_infra_of_every_service(
    monkeypatch: pytest.MonkeyPatch, make_repo, tmp_path
) -> None:
    make_repo("shop-api", {"docker-compose.yml": "services: {}\n"})
    called = _stub_run_checks_environment(monkeypatch)
    catalog = _infra_catalog(
        {"back": {"postgres": {"databases": ["shop"]}}, "front": {"redis": {}}}
    )
    loaded = write_global_catalog(tmp_path, catalog)
    assert loaded.config.shared.redis is None  # the anchor (back) alone would miss redis
    doctor.run_checks(loaded, None, fix=False)
    assert sorted(called) == ["postgres:shop", "redis"]


def test_catalog_run_checks_merges_postgres_databases(
    monkeypatch: pytest.MonkeyPatch, make_repo, tmp_path
) -> None:
    make_repo("shop-api", {"docker-compose.yml": "services: {}\n"})
    called = _stub_run_checks_environment(monkeypatch)
    catalog = _infra_catalog(
        {
            "back": {"postgres": {"databases": ["shop"]}},
            "front": {"postgres": {"databases": ["shop", "web"]}},
        }
    )
    doctor.run_checks(write_global_catalog(tmp_path, catalog), None, fix=False)
    assert called == ["postgres:shop,web"]


def test_catalog_run_checks_reports_an_unreadable_catalog(
    monkeypatch: pytest.MonkeyPatch, make_repo, tmp_path
) -> None:
    make_repo("shop-api", {"docker-compose.yml": "services: {}\n"})
    called = _stub_run_checks_environment(monkeypatch)
    loaded = write_global_catalog(tmp_path, _infra_catalog({"back": {"redis": {}}}))
    loaded.path.write_text("services: [")
    checks = doctor.run_checks(loaded, None, fix=False)
    assert called == []
    failed = [c for c in checks if c.name == "catalog"]
    assert [c.level for c in failed] == ["fail"]


def _record_process(port: int | None) -> None:
    with StateStore("app").edit() as state:
        state.branches["feat/a"] = BranchState(
            branch="feat/a",
            slug="feat-a",
            processes={"web": ProcessState(pid=1, pgid=1, port=port, command="x", log="/dev/null")},
        )


def test_ports_free_for_dead_process(monkeypatch: pytest.MonkeyPatch) -> None:
    _record_process(4100)
    monkeypatch.setattr(host, "alive", lambda ps: False)
    monkeypatch.setattr(host, "port_free", lambda port: True)
    (check,) = doctor.check_ports("app")
    assert (check.name, check.level) == ("port 4100", "ok")


def test_ports_taken_for_dead_process_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _record_process(4100)
    monkeypatch.setattr(host, "alive", lambda ps: False)
    monkeypatch.setattr(host, "port_free", lambda port: False)
    (check,) = doctor.check_ports("app")
    assert check.level == "fail"
    assert "feat/a/web" in check.detail


def test_ports_ignore_live_or_portless_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    _record_process(4100)
    monkeypatch.setattr(host, "alive", lambda ps: True)
    assert doctor.check_ports("app") == []
    _record_process(None)
    monkeypatch.setattr(host, "alive", lambda ps: False)
    assert doctor.check_ports("app") == []


def _mounts(monkeypatch: pytest.MonkeyPatch, after_restart: list[str]) -> list[str]:
    restarts: list[str] = []
    missing = {"now": ["shop-main.yml"]}

    def restart(*a: object, **k: object) -> None:
        restarts.append("restart")
        missing["now"] = after_restart

    monkeypatch.setattr(proc, "run", _traefik_answers)
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr(router, "is_running", lambda: True)
    monkeypatch.setattr(router, "has_host_gateway", lambda: True)
    monkeypatch.setattr(router, "missing_in_container", lambda: missing["now"])
    monkeypatch.setattr(router, "restart", restart)
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: True)
    monkeypatch.setattr(doctor, "_traefik_ports", lambda: "")
    return restarts


def test_mounts_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    restarts = _mounts(monkeypatch, [])
    monkeypatch.setattr(router, "missing_in_container", lambda: [])
    assert _by_name(doctor.check_routing(fix=False))["traefik mounts"].level == "ok"
    assert restarts == []


def test_mounts_missing_fails_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    restarts = _mounts(monkeypatch, [])
    check = _by_name(doctor.check_routing(fix=False))["traefik mounts"]
    assert check.level == "fail"
    assert "shop-main.yml" in check.detail
    assert "wt doctor --fix" in check.hint
    assert restarts == []


def test_mounts_fixed_by_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    restarts = _mounts(monkeypatch, [])
    assert _by_name(doctor.check_routing(fix=True))["traefik mounts"].level == "ok"
    assert restarts == ["restart"]


def test_mounts_still_missing_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    restarts = _mounts(monkeypatch, ["shop-main.yml"])
    check = _by_name(doctor.check_routing(fix=True))["traefik mounts"]
    assert check.level == "fail"
    assert restarts == ["restart"]


def test_mounts_recreate_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _mounts(monkeypatch, [])

    def boom(*a: object, **k: object) -> None:
        raise WtError("mount source gone")

    monkeypatch.setattr(router, "restart", boom)
    check = _by_name(doctor.check_routing(fix=True))["traefik mounts"]
    assert check.level == "fail"
    assert "mount source gone" in check.detail
    assert "wt doctor --fix" in check.hint


def test_mounts_fix_passes_force_recreate_to_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _mounts(monkeypatch, [])
    monkeypatch.undo()
    state = {"missing": ["shop-main.yml"]}

    def run(args: list[str], **kw: object) -> proc.Result:
        calls.append(list(args))
        if args[:2] == ["docker", "compose"]:
            state["missing"] = []
        return _traefik_answers(args, **kw)

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr(router, "is_running", lambda: True)
    monkeypatch.setattr(router, "has_host_gateway", lambda: True)
    monkeypatch.setattr(router, "missing_in_container", lambda: state["missing"])
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: True)
    monkeypatch.setattr(doctor, "_traefik_ports", lambda: "")
    assert _by_name(doctor.check_routing(fix=True))["traefik mounts"].level == "ok"
    assert any("--force-recreate" in c for c in calls)


# ---- published host ports ---------------------------------------------------------------


class TraefikFake:
    """A running Traefik whose published ports change when it is recreated."""

    def __init__(self, *, published: bool) -> None:
        self.ports = {router.http_port(), router.https_port()} if published else set()
        self.recreated = 0

    def install(self, monkeypatch: pytest.MonkeyPatch, *, running: bool = True) -> None:
        def recreate(*a: object, **k: object) -> None:
            assert k.get("force_recreate") is True
            self.recreated += 1
            self.ports = {router.http_port(), router.https_port()}

        monkeypatch.setattr(proc, "run", lambda args, **kw: _answer(running, self.ports)(args))
        monkeypatch.setattr(compose, "ensure_network", lambda: None)
        monkeypatch.setattr(router, "is_running", lambda: running)
        monkeypatch.setattr(router, "has_host_gateway", lambda: True)
        monkeypatch.setattr(router, "missing_in_container", lambda: [])
        monkeypatch.setattr(router, "ensure_running", recreate)
        monkeypatch.setattr(doctor, "_port_in_use", lambda port: True)
        monkeypatch.setattr(doctor, "_traefik_ports", lambda: "")


def test_traefik_without_published_ports_fails_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = TraefikFake(published=False)
    fake.install(monkeypatch)
    check = _by_name(doctor.check_routing(fix=False))["traefik ports"]
    assert check.level == "fail"
    assert check.detail.startswith(f"{router.container()} runs without its published ports")
    assert str(router.http_port()) in check.detail
    assert "\n" not in check.detail
    assert check.hint == "run `wt doctor --fix`"
    assert fake.recreated == 0


def test_traefik_names_only_the_missing_port(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = TraefikFake(published=True)
    fake.ports.discard(router.https_port())
    fake.install(monkeypatch)
    check = _by_name(doctor.check_routing(fix=False))["traefik ports"]
    assert check.level == "fail"
    assert check.detail.endswith(f"(missing {router.https_port()})")


def test_traefik_with_published_ports_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = TraefikFake(published=True)
    fake.install(monkeypatch)
    assert _by_name(doctor.check_routing(fix=False))["traefik ports"].level == "ok"
    assert fake.recreated == 0


def test_traefik_fix_recreates_once_and_the_next_check_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = TraefikFake(published=False)
    fake.install(monkeypatch)
    assert _by_name(doctor.check_routing(fix=True))["traefik ports"].level == "ok"
    assert fake.recreated == 1
    assert _by_name(doctor.check_routing(fix=False))["traefik ports"].level == "ok"
    assert fake.recreated == 1


def test_traefik_fix_that_cannot_publish_stays_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = TraefikFake(published=False)
    fake.install(monkeypatch)

    def recreate(*a: object, **k: object) -> None:
        fake.recreated += 1

    monkeypatch.setattr(router, "ensure_running", recreate)
    check = _by_name(doctor.check_routing(fix=True))["traefik ports"]
    assert (check.level, fake.recreated) == ("fail", 1)
    assert "runs without its published ports" in check.detail


def test_traefik_fix_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    TraefikFake(published=False).install(monkeypatch)

    def boom(*a: object, **k: object) -> None:
        raise WtError("port 80 is already in use by another program\nmore")

    monkeypatch.setattr(router, "ensure_running", boom)
    check = _by_name(doctor.check_routing(fix=True))["traefik ports"]
    assert check.level == "fail"
    assert check.detail == "port 80 is already in use by another program"
    assert "wt doctor --fix" in check.hint


def test_stopped_traefik_has_no_ports_check(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = TraefikFake(published=False)
    fake.install(monkeypatch, running=False)
    checks = _by_name(doctor.check_routing(fix=False))
    assert "traefik ports" not in checks
    assert checks["traefik"].detail == "not running"
    assert fake.recreated == 0


ServerFake = RedisFake | KafkaFake | KeycloakFake
SERVERS: dict[str, tuple[Callable[..., ServerFake], Callable[..., doctor.Check]]] = {
    "redis": (RedisFake, doctor.check_redis),
    "kafka": (KafkaFake, doctor.check_kafka),
    "keycloak": (KeycloakFake, doctor.check_keycloak),
}


HOST_PORTS: dict[str, Callable[[], int]] = {
    "redis": redis.host_port,
    "kafka": kafka.host_port,
    "keycloak": keycloak.host_port,
}


@pytest.mark.parametrize("name", SERVERS)
def test_server_without_published_port_fails_with_hint(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    make_fake, check_server = SERVERS[name]
    fake = make_fake(running=True, published=False)
    fake.install(monkeypatch)
    check = check_server(fix=False)
    assert check.level == "fail"
    port = HOST_PORTS[name]()
    assert check.detail.endswith(f"runs without its published ports (missing {port})")
    assert "\n" not in check.detail
    assert check.hint == "run `wt doctor --fix`"
    assert fake.ensured == 0


@pytest.mark.parametrize("name", SERVERS)
def test_server_with_published_port_is_ok(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    make_fake, check_server = SERVERS[name]
    fake = make_fake(running=True, published=True)
    fake.install(monkeypatch)
    assert check_server(fix=False).level == "ok"
    assert fake.ensured == 0


@pytest.mark.parametrize("name", SERVERS)
def test_server_fix_recreates_once_and_the_next_check_passes(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    make_fake, check_server = SERVERS[name]
    fake = make_fake(running=True, published=False)
    fake.install(monkeypatch)
    assert check_server(fix=True).level == "ok"
    assert fake.ensured == 1
    assert check_server(fix=False).level == "ok"
    assert fake.ensured == 1


@pytest.mark.parametrize("name", SERVERS)
def test_server_fix_that_cannot_publish_stays_failed(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    make_fake, check_server = SERVERS[name]
    fake = make_fake(running=True, published=False)
    fake.install(monkeypatch)
    module = {"redis": redis, "kafka": kafka, "keycloak": keycloak}[name]
    calls: list[int] = []
    monkeypatch.setattr(module, "ensure_server", lambda *a, **k: calls.append(1))
    check = check_server(fix=True)
    assert (check.level, len(calls)) == ("fail", 1)
    assert "runs without its published ports" in check.detail


@pytest.mark.parametrize("name", SERVERS)
def test_stopped_server_keeps_the_not_running_failure(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    make_fake, check_server = SERVERS[name]
    fake = make_fake(running=False, published=False)
    fake.install(monkeypatch)
    check = check_server(fix=False)
    assert check.level == "fail"
    assert check.detail.endswith("not running")
    assert fake.ensured == 0


def test_postgres_without_published_port_fails_with_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = PgFake(running=True, closed=[], published=False)
    fake.install(monkeypatch)
    check = _by_name(doctor.check_postgres(["shop"], fix=False))["postgres"]
    assert check.level == "fail"
    assert check.detail == (
        f"{postgres.container()} runs without its published ports (missing {postgres.host_port()})"
    )
    assert check.hint == "run `wt doctor --fix`"
    assert fake.ensured == []


def test_postgres_fix_recreates_once_and_the_next_check_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = PgFake(running=True, closed=[], published=False)
    fake.install(monkeypatch)
    assert _by_name(doctor.check_postgres(["shop"], fix=True))["postgres"].level == "ok"
    assert fake.ensured == [["shop"]]
    assert _by_name(doctor.check_postgres(["shop"], fix=False))["postgres"].level == "ok"
    assert fake.ensured == [["shop"]]


def test_postgres_fix_that_cannot_publish_stays_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    PgFake(running=True, closed=[], published=False).install(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(postgres, "ensure_server", lambda dbs, timeout=60: calls.append(dbs))
    check = _by_name(doctor.check_postgres(["shop"], fix=True))["postgres"]
    assert (check.level, len(calls)) == ("fail", 1)


def test_stopped_postgres_keeps_the_not_running_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = PgFake(running=False, closed=[], published=False)
    fake.install(monkeypatch)
    check = _by_name(doctor.check_postgres(["shop"], fix=False))["postgres"]
    assert check.level == "warn"
    assert check.detail.endswith("not running")
    assert fake.ensured == []
