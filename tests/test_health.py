import socket
import time
from types import SimpleNamespace

import pytest

from wt import health


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_probe_without_server_fails() -> None:
    check = health.probe("api.x.localhost", "/", free_port(), timeout=0.5)
    assert not check.ok
    assert "no response" in check.detail


@pytest.mark.parametrize(
    ("status", "ok"),
    [
        (200, True),
        (302, True),
        (401, True),
        (403, True),
        (404, False),
        (500, False),
        (503, False),
    ],
)
def test_probe_status_table(monkeypatch: pytest.MonkeyPatch, status: int, ok: bool) -> None:
    monkeypatch.setattr(health._LoopbackHTTPS, "request", lambda self, *a, **k: None)
    monkeypatch.setattr(
        health._LoopbackHTTPS,
        "getresponse",
        lambda self: SimpleNamespace(status=status),
    )
    check = health.probe("api.x.localhost", "/", free_port(), timeout=0.5)
    assert check.ok is ok
    assert check.detail == f"HTTP {status}"


def test_wait_healthy_gives_up() -> None:
    check = health.wait_healthy("api.x.localhost", "/", free_port(), timeout=0)
    assert not check.ok


def test_wait_healthy_returns_before_one_second_past_short_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health,
        "probe",
        lambda host, path, port, timeout=3.0: health.Check(False, "no response"),
    )
    start = time.monotonic()
    check = health.wait_healthy("api.x.localhost", "/", free_port(), timeout=0.3)
    elapsed = time.monotonic() - start
    assert not check.ok
    assert elapsed < 1.0
