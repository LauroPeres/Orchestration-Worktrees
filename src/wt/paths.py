"""Where wt keeps its own files. Override with the WT_HOME environment variable."""

import os
from pathlib import Path

from wt.errors import WtError


def instance() -> str:
    """Prefix of the shared Docker objects (network, Traefik). WT_INSTANCE isolates test runs."""
    return os.environ.get("WT_INSTANCE", "wt")


def env_port(name: str, default: int) -> int:
    """Read a TCP port from the environment; a bad value is a WtError naming the variable."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise WtError(f"{name} must be a port number between 1 and 65535, got {raw!r}")
    return port


def wt_home() -> Path:
    override = os.environ.get("WT_HOME")
    return Path(override).expanduser() if override else Path.home() / ".wt"


def project_dir(project: str) -> Path:
    return wt_home() / project


def locks_dir() -> Path:
    return wt_home() / "locks"


def cache_dir() -> Path:
    return wt_home() / "cache"


def installs_dir() -> Path:
    """Restorable install entries (SPEC section 24), one folder per key."""
    return cache_dir() / "installs"


def tool_cache_dir(name: str) -> Path:
    """The shared download cache of one tool: `uv` or `npm`."""
    return cache_dir() / name
