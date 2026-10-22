from collections.abc import Callable

import pytest

from wt import compose, doctor, proc, router

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
