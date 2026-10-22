import socket

from wt import health


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_probe_without_server_fails() -> None:
    check = health.probe("api.x.localhost", "/", free_port(), timeout=0.5)
    assert not check.ok
    assert "no response" in check.detail


def test_wait_healthy_gives_up() -> None:
    check = health.wait_healthy("api.x.localhost", "/", free_port(), timeout=0)
    assert not check.ok
