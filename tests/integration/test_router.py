"""Shared Traefik against a real daemon, with a stranger holding its port: `pytest -m docker`.

Isolated by WT_INSTANCE, WT_HOME and free host ports; teardown removes only its own container
and network. See docs/SPEC.md §20 "Published ports".
"""

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

from wt import compose, doctor, router
from wt.errors import WtError

pytestmark = pytest.mark.docker

FAIL_FAST_SECONDS = 60  # a taken port must fail clearly, not hang for the 2-minute wait


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


class Stranger:
    """A program that is not wt, listening on a host port."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.sock: socket.socket | None = None

    def hold(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", self.port))
        sock.listen()
        self.sock = sock

    def release(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


@pytest.fixture
def stranger_on_https(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Stranger]:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.fail("docker is not available")
    monkeypatch.setenv("WT_INSTANCE", f"wtrt{free_port()}")
    monkeypatch.setenv("WT_HOME", str(tmp_path_factory.mktemp("wt-home")))
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    stranger = Stranger(router.https_port())
    try:
        yield stranger
    finally:
        stranger.release()
        docker("rm", "-f", "-v", router.container())
        docker("network", "rm", compose.network_name())


def timed_failure() -> tuple[WtError, float]:
    started = time.monotonic()
    with pytest.raises(WtError) as caught:
        router.ensure_running()
    return caught.value, time.monotonic() - started


def ports_check(fix: bool) -> doctor.Check:
    return next(c for c in doctor.check_routing(fix) if c.name == "traefik ports")


def test_a_taken_port_fails_clearly_twice_then_recovers(stranger_on_https: Stranger) -> None:
    stranger_on_https.hold()
    for _ in range(2):
        error, seconds = timed_failure()
        assert str(error).strip()
        assert seconds < FAIL_FAST_SECONDS
    assert not {router.https_port()} <= compose.published_host_ports(router.container())

    stranger_on_https.release()
    router.ensure_running()
    assert {router.http_port(), router.https_port()} <= compose.published_host_ports(
        router.container()
    )
    check = ports_check(fix=False)
    assert check.level == "ok", check


def test_doctor_fix_recreates_a_traefik_that_runs_without_ports(
    stranger_on_https: Stranger,
) -> None:
    stranger_on_https.hold()
    timed_failure()
    stranger_on_https.release()
    docker("start", router.container())
    time.sleep(3)
    # Older Docker engines (seen locally) restart the container without publishing anything;
    # newer ones (seen on the GitHub runner) publish normally. Both outcomes must be handled.
    quirk_reproduced = docker("port", router.container()).stdout.strip() == ""
    check = ports_check(fix=False)
    if quirk_reproduced:
        assert check.level == "fail"
        assert "runs without its published ports" in check.detail
    else:
        assert check.level == "ok", check
        assert router.https_port() in compose.published_host_ports(router.container())

    fixed = ports_check(fix=True)
    assert fixed.level == "ok", fixed
    assert router.https_port() in compose.published_host_ports(router.container())
