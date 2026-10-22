"""Where wt keeps its own files. Override with the WT_HOME environment variable."""

import os
from pathlib import Path


def instance() -> str:
    """Prefix of the shared Docker objects (network, Traefik). WT_INSTANCE isolates test runs."""
    return os.environ.get("WT_INSTANCE", "wt")


def wt_home() -> Path:
    override = os.environ.get("WT_HOME")
    return Path(override).expanduser() if override else Path.home() / ".wt"


def project_dir(project: str) -> Path:
    return wt_home() / project


def locks_dir() -> Path:
    return wt_home() / "locks"
