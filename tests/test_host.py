import os
import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from wt import host, naming
from wt.errors import WtError
from wt.state import ProcessState

SERVE = Path(__file__).parent / "serve.py"
SERVER = f"{sys.executable} {SERVE} {{port}}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return int(s.getsockname()[1])


def wait_for(check: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def started(tmp_path: Path) -> Iterator[list[ProcessState]]:
    items: list[ProcessState] = []
    yield items
    for ps in items:
        host.stop(ps, grace=0.2)


def start_server(tmp_path: Path, started: list[ProcessState]) -> ProcessState:
    port = free_port()
    ps = host.start(
        SERVER.format(port=port), tmp_path, dict(os.environ), tmp_path / "logs" / "web.log", port
    )
    started.append(ps)
    return ps


def test_start_alive_stop(tmp_path: Path, started: list[ProcessState]) -> None:
    ps = start_server(tmp_path, started)
    assert ps.pgid == ps.pid
    assert host.alive(ps)
    assert wait_for(lambda: host.tcp_ok(ps.port or 0))
    assert ps.log == str(tmp_path / "logs" / "web.log")
    host.stop(ps)
    assert not host.alive(ps)
    assert not host.tcp_ok(ps.port or 0)


def test_start_appends_output_to_log(tmp_path: Path, started: list[ProcessState]) -> None:
    log = tmp_path / "out.log"
    log.write_text("old\n")
    ps = host.start("echo hello; echo oops >&2", tmp_path, {}, log)
    started.append(ps)
    assert wait_for(lambda: "oops" in log.read_text())
    text = log.read_text()
    assert text.startswith("old\n")
    assert "hello" in text


def test_start_uses_cwd_and_env(tmp_path: Path, started: list[ProcessState]) -> None:
    log = tmp_path / "out.log"
    ps = host.start("pwd; echo $WT_X", tmp_path, {"WT_X": "42"}, log)
    started.append(ps)
    assert wait_for(lambda: "42" in log.read_text())
    assert str(tmp_path.resolve()) in log.read_text()


def test_stop_falls_back_to_sigkill(tmp_path: Path, started: list[ProcessState]) -> None:
    log = tmp_path / "out.log"
    ps = host.start("trap '' TERM; echo ready; while true; do sleep 1; done", tmp_path, {}, log)
    started.append(ps)
    assert wait_for(lambda: "ready" in log.read_text())
    started_at = time.monotonic()
    host.stop(ps, grace=0.5)
    assert time.monotonic() - started_at >= 0.5  # it waited for SIGTERM first
    assert not host.alive(ps)


def test_stop_dead_process_is_a_noop(tmp_path: Path, started: list[ProcessState]) -> None:
    ps = host.start("true", tmp_path, {}, tmp_path / "out.log")
    started.append(ps)
    assert wait_for(lambda: not host.alive(ps))
    host.stop(ps)


def test_pick_port_prefers_the_stable_port(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = naming.auto_port("shop", "main", "web")
    monkeypatch.setattr(host, "port_free", lambda p: True)
    assert host.pick_port("shop", "main", "web", set()) == expected


def test_pick_port_probes_past_a_bound_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    base = free_port()
    monkeypatch.setattr(naming, "auto_port", lambda *a: base)
    with socket.socket() as s:
        s.bind(("0.0.0.0", base))
        s.listen()
        port = host.pick_port("shop", "main", "web", set())
    assert port != base
    assert base < port <= base + 100


def test_pick_port_skips_taken_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    base = free_port()
    monkeypatch.setattr(naming, "auto_port", lambda *a: base)
    monkeypatch.setattr(host, "port_free", lambda p: True)
    assert host.pick_port("shop", "main", "web", {base, base + 1}) == base + 2


def test_pick_port_gives_up_after_100_tries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "port_free", lambda p: False)
    with pytest.raises(WtError, match="no free port"):
        host.pick_port("shop", "main", "web", set())


def test_explicit_port_is_used_as_is() -> None:
    port = free_port()
    assert host.pick_port("shop", "main", "web", set(), requested=port) == port


def test_explicit_port_in_use_fails() -> None:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        s.listen()
        port = int(s.getsockname()[1])
        with pytest.raises(WtError, match=f"port {port} .* in use"):
            host.pick_port("shop", "main", "web", set(), requested=port)


def test_explicit_port_taken_by_another_service_fails() -> None:
    port = free_port()
    with pytest.raises(WtError, match="another service"):
        host.pick_port("shop", "main", "web", {port}, requested=port)


def test_recycled_pid_in_another_group_is_not_ours(
    tmp_path: Path, started: list[ProcessState], monkeypatch: pytest.MonkeyPatch
) -> None:
    ps = start_server(tmp_path, started)
    assert host.alive(ps)
    monkeypatch.setattr(os, "getpgid", lambda pid: ps.pgid + 1)
    assert not host.alive(ps)
    signalled: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pg, sig: signalled.append(sig))
    host.stop(ps)
    assert [s for s in signalled if s != 0] == []  # only the existence probe


def test_process_started_after_the_record_is_not_ours(
    tmp_path: Path, started: list[ProcessState], monkeypatch: pytest.MonkeyPatch
) -> None:
    ps = start_server(tmp_path, started)
    monkeypatch.setattr(host, "start_time", lambda pid: time.time() + 3600)
    assert not host.alive(ps)
    signalled: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pg, sig: signalled.append(sig))
    host.stop(ps)
    assert [s for s in signalled if s != 0] == []  # only the existence probe


def test_start_time_is_close_to_now(tmp_path: Path, started: list[ProcessState]) -> None:
    ps = start_server(tmp_path, started)
    when = host.start_time(ps.pid)
    assert when is not None
    assert abs(when - time.time()) < 30
