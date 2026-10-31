"""Start a shared server (one compose project) used by several branches (docs/SPEC.md §14, §18)."""

import time
from pathlib import Path
from typing import Any

import yaml

from wt import compose, proc
from wt.state import atomic_write

MIN_READY_SECONDS = 1.0  # a readiness probe always gets one real try


def up(name: str, folder: Path, doc: dict[str, Any], timeout: int) -> float:
    """Write `folder/compose.yml` atomically, make sure the shared network exists, start it.

    `timeout` is the provider's whole deadline (SPEC §18 "One deadline"). Returns what is left of
    it after the start, at least `MIN_READY_SECONDS`, for the readiness wait.
    """
    started = time.monotonic()
    compose_file = folder / "compose.yml"
    atomic_write(compose_file, yaml.safe_dump(doc, sort_keys=False))
    compose.ensure_network()
    proc.run(
        [
            "docker",
            "compose",
            "-p",
            name,
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            str(timeout),
        ],
        cwd=folder,
    )
    return max(MIN_READY_SECONDS, timeout - (time.monotonic() - started))
