import socket
from collections.abc import Callable

import pytest

from wt import compose, doctor, host, proc, router
from wt.config import STARTER, LoadedConfig, load_config
from wt.errors import WtError
from wt.providers import postgres
from wt.state import BranchState, ProcessState, StateStore

OK = proc.Result(0, "", "")


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

    monkeypatch.setattr(proc, "run", lambda args, **kw: proc.Result(0, "", ""))
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr(router, "is_running", lambda: state["running"])
    monkeypatch.setattr(router, "ensure_running", start)
    monkeypatch.setattr(doctor, "_port_in_use", lambda port: False)
    monkeypatch.setattr(doctor, "_traefik_ports", lambda: "")
    checks = {c.name: c for c in doctor.check_routing(fix=True)}
    assert checks["traefik"].level == "ok"


# ---- shared Postgres and recorded ports ------------------------------------------------


class PgFake:
    """Stands in for the container and the provider helpers."""

    def __init__(self, *, running: bool, closed: list[str]) -> None:
        self.running = running
        self.closed = closed
        self.ensured: list[list[str]] = []
        self.repaired = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def run(args: list[str], **kw: object) -> proc.Result:
            return proc.Result(0, "true\n" if self.running else "false\n", "")

        def ensure(databases: list[str], timeout: int = 60) -> None:
            self.ensured.append(databases)
            self.running = True
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


def _loaded(make_repo, with_postgres: bool) -> LoadedConfig:
    shared = "shared:\n  postgres:\n    databases: [shop]\n" if with_postgres else ""
    repo = make_repo("app", {"worktree.yaml": STARTER.format(project="app") + shared})
    return load_config(repo / "worktree.yaml")


@pytest.mark.parametrize("with_postgres", [False, True])
def test_run_checks_only_checks_postgres_when_configured(
    monkeypatch: pytest.MonkeyPatch, make_repo, with_postgres: bool
) -> None:
    loaded = _loaded(make_repo, with_postgres)
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
