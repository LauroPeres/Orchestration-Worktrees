"""REPORT ONLY: lets `wt` import on Windows so the CI job can map what else fails.

The real `fcntl` does not exist on Windows. These locks do nothing, so a failure that depends on
locking is hidden by this stub; it is listed in docs/windows.md as a known gap, not tested.
"""

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(fd: object, operation: int) -> None:
    return None
