"""Keep integration runs independent of whatever else runs on the machine.

Host ports are `PORT_BASE + worktree_index * PORT_STRIDE + service_index`, and the first branch
of a fresh WT_HOME gets index 0, so it would collide with a real stack on the default ports.
`reserve_free_indexes` fills the registry of the isolated WT_HOME with dummy owners up to the
first index whose whole port window is free, so the first real branch lands there instead.
"""

import json
import socket
from pathlib import Path

from wt import naming
from wt.state import index_registry_path

DEFAULT_PORT_BASE = naming.PORT_BASE  # read at import: the unit-test fixture patches it later
MAX_FIRST_INDEX = 400  # keeps ports <= 50000
FALLBACK_FIRST_INDEX = 150
MAX_PORT = 65535
_HOSTS = ("127.0.0.1", "0.0.0.0")


def _port_is_free(port: int) -> bool:
    for host in _HOSTS:
        with socket.socket() as probe:
            try:
                probe.bind((host, port))
            except OSError:
                return False
    return True


def _window_is_free(base: int, index: int) -> bool:
    first = base + index * naming.PORT_STRIDE
    if first + naming.PORT_STRIDE - 1 > MAX_PORT:
        return False
    return all(_port_is_free(port) for port in range(first, first + naming.PORT_STRIDE))


def first_free_index() -> int:
    """The first worktree index whose ports are free for the default base (a CLI subprocess)
    and for the current one (the in-process orchestrator, which tests may have moved)."""
    bases = {DEFAULT_PORT_BASE, naming.PORT_BASE}
    for index in range(MAX_FIRST_INDEX + 1):
        if all(_window_is_free(base, index) for base in bases):
            return index
    return FALLBACK_FIRST_INDEX


def reserve_free_indexes(wt_home: Path) -> int:
    """Seed `wt_home`'s registry so the next allocated index is the one returned.

    Call it after WT_HOME points at `wt_home`; every fixture that creates an isolated WT_HOME
    for a stack that brings branches up uses this one helper.
    """
    first = first_free_index()
    registry_path = index_registry_path()
    assert registry_path.parent == wt_home, "WT_HOME must be set to wt_home first"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = {str(index): f"reserved/{index}" for index in range(first)}
    registry_path.write_text(json.dumps(registry, indent=2) + "\n")
    return first
