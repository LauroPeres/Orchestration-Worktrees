"""Start a shared server (one compose project) used by several branches (docs/SPEC.md §14, §18)."""

from pathlib import Path
from typing import Any

import yaml

from wt import compose, proc
from wt.state import atomic_write


def up(name: str, folder: Path, doc: dict[str, Any], timeout: int) -> None:
    """Write `folder/compose.yml` atomically, make sure the shared network exists, start it."""
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
