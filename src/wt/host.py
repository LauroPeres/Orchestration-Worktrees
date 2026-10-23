"""Host processes for `run:` services: start in their own session, find a free port, stop."""

import contextlib
import os
import signal
import socket
import subprocess
import time
from collections.abc import Collection, Mapping
from datetime import datetime
from pathlib import Path

from wt.errors import WtError
from wt.state import ProcessState, now


def port_free(port: int) -> bool:
    """True when nothing accepts connections on the port.

    Connect-based on purpose: a bind probe reports ports in TIME_WAIT as taken (when the old
    server did not set SO_REUSEADDR) and succeeds over a listener on BSD with SO_REUSEADDR.
    """
    return not tcp_ok(port, timeout=0.5)


def tcp_ok(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def pick_port(service: str, taken: Collection[int], requested: int) -> int:
    """An explicit `port` of a `run:` service, checked. `taken` = ports state records for any
    other (branch, service)."""
    if requested in taken:
        raise WtError(f"{service}: port {requested} is already used by another service of wt")
    if not port_free(requested):
        raise WtError(f"{service}: port {requested} is in use; free it or change 'port'")
    return requested


def start(
    command: str,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    port: int | None = None,
) -> ProcessState:
    """Run `sh -c command` in a new session, appending stdout and stderr to the log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            ["sh", "-c", command],
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    # The session leader's pid is the group id.
    return ProcessState(
        pid=proc.pid, pgid=proc.pid, port=port, command=command, started_at=now(), log=str(log_path)
    )


def _reap(pid: int) -> None:
    """Collect the exit status if the process is our own child, so it is not left as a zombie."""
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def start_time(pid: int) -> float | None:
    """Epoch start time of a process, or None when it cannot be read."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # field 22 (starttime, in clock ticks since boot); the name (field 2) may hold spaces
        ticks = int(stat.rsplit(")", 1)[1].split()[19])
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return int(line.split()[1]) + ticks / os.sysconf("SC_CLK_TCK")
        return None
    except (OSError, ValueError, IndexError):
        pass
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    try:
        return time.mktime(time.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _leader_is_ours(ps: ProcessState) -> bool:
    """False when the pid was recycled: another group, or started after we recorded ours."""
    try:
        if os.getpgid(ps.pid) != ps.pgid:
            return False
    except ProcessLookupError:
        return True  # the leader is gone; a live group id is never handed out as a new pid
    except PermissionError:
        return False
    began = start_time(ps.pid)
    return began is None or began <= datetime.fromisoformat(ps.started_at).timestamp() + 2


def alive(ps: ProcessState) -> bool:
    _reap(ps.pid)
    try:
        os.killpg(ps.pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # someone else's group: not ours
    return _leader_is_ours(ps)


def _wait_dead(ps: ProcessState, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while alive(ps):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def stop(ps: ProcessState, grace: float = 10.0) -> None:
    """SIGTERM to the process group, SIGKILL if it is still there after `grace` seconds."""
    if not alive(ps):
        return
    try:
        os.killpg(ps.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if _wait_dead(ps, grace):
        return
    try:
        os.killpg(ps.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    _wait_dead(ps, 5.0)
