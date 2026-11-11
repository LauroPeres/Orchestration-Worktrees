"""Real disk use of a folder (SPEC section 36): apparent size plus what btrfs says is exclusive.

Disk tools add up file sizes, so reflinked copies count in full. On btrfs, `btrfs filesystem du`
splits a tree into bytes only it holds (exclusive) and bytes shared with other files. Everywhere
else, or when the tool fails, those two numbers are unknown (`None`), which is never an error.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from wt import proc
from wt.errors import WtError

BTRFS_DU_TIMEOUT_SECONDS = 600.0  # a tree with hundreds of thousands of files takes minutes
_DU_COLUMNS = 3  # Total, Exclusive, Set shared
_ASCII_NUMBER = re.compile(r"[0-9]+")  # str.isdigit() also accepts "²", which int() rejects


@dataclass(frozen=True)
class DiskUsage:
    apparent_bytes: int  # sum of lstat sizes, symlinks never followed
    exclusive_bytes: int | None  # None = cannot be measured here
    shared_bytes: int | None


def apparent_bytes(path: Path) -> int:
    """Bytes under `path`; symlinks count as links, never followed."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue  # vanished while walking: a concurrent prune or install
    return total


def usage(path: Path) -> DiskUsage:
    """Apparent, exclusive and shared bytes of `path`; a missing path is a `WtError`."""
    if not path.is_dir():
        raise WtError(f"cannot measure disk use, not a folder: {path}")
    exclusive, shared = _btrfs_du(path)
    return DiskUsage(apparent_bytes(path), exclusive, shared)


def _btrfs_du(path: Path) -> tuple[int | None, int | None]:
    """(exclusive, shared) from `btrfs filesystem du`; (None, None) whenever it cannot say."""
    try:
        result = proc.run(
            ["btrfs", "filesystem", "du", "-s", "--raw", str(path)],
            check=False,
            timeout=BTRFS_DU_TIMEOUT_SECONDS,
        )
    except WtError:
        return None, None  # no btrfs binary, or it ran past the timeout
    if not result.ok:
        return None, None  # not a btrfs filesystem, or no permission
    return _parse_du(result.stdout)


def _parse_du(output: str) -> tuple[int | None, int | None]:
    """Exclusive and shared from the last row: Total, Exclusive, Set shared, then the name."""
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) < 2:  # a header and at least one row
        return None, None
    columns = lines[-1].split(maxsplit=_DU_COLUMNS)
    if len(columns) <= _DU_COLUMNS or not all(
        _ASCII_NUMBER.fullmatch(c) for c in columns[:_DU_COLUMNS]
    ):
        return None, None
    return int(columns[1]), int(columns[2])
