"""Start a shared server (one compose project) used by several branches (docs/SPEC.md §14, §18)."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from wt import compose, proc
from wt.errors import WtError
from wt.state import atomic_write

MIN_READY_SECONDS = 1.0  # a readiness probe always gets one real try


def _host_port(entry: object) -> int | None:
    """The host port of one `ports:` entry; None when Docker picks it or the form is not a string.

    Handles `"127.0.0.1:55432:5432"`, `"55432:5432"` and `"55432:5432/tcp"`. A bare container
    port (`"5432"` or an int) has no fixed host port to check.
    """
    if not isinstance(entry, str):
        return None
    parts = entry.split("/")[0].split(":")
    if len(parts) < 2 or not parts[-2].isdecimal():
        return None
    return int(parts[-2])


def expected_ports(project: str, doc: dict[str, Any]) -> dict[str, set[int]]:
    """Host ports each container of the compose doc must publish, by container name.

    The name is the service's `container_name`, else the one compose derives
    (`<project>-<service>-1`). Containers with no fixed host port are left out.
    """
    expected: dict[str, set[int]] = {}
    for service, spec in doc.get("services", {}).items():
        ports = {p for p in map(_host_port, spec.get("ports", [])) if p is not None}
        if ports:
            expected[spec.get("container_name", f"{project}-{service}-1")] = ports
    return expected


def _start_command(name: str, compose_file: Path, timeout: float, *flags: str) -> list[str]:
    return [
        "docker", "compose", "-p", name, "-f", str(compose_file),
        "up", "-d", *flags, "--wait", "--wait-timeout", str(int(timeout)),
    ]  # fmt: skip


def up(name: str, folder: Path, doc: dict[str, Any], timeout: int) -> float:
    """Write `folder/compose.yml` atomically, make sure the shared network exists, start it.

    `timeout` is the provider's whole deadline (SPEC §18 "One deadline"). Afterwards the container
    must publish the host ports the doc declares; if it does not, it is recreated once (SPEC §20
    "Published ports"). Returns what is left of the deadline, at least `MIN_READY_SECONDS`, for the
    readiness wait.
    """
    started = time.monotonic()
    compose_file = folder / "compose.yml"
    atomic_write(compose_file, yaml.safe_dump(doc, sort_keys=False))
    compose.ensure_network()

    def left() -> float:
        return max(MIN_READY_SECONDS, timeout - (time.monotonic() - started))

    def recreate() -> None:
        proc.run(_start_command(name, compose_file, left(), "--force-recreate"), cwd=folder)

    proc.run(_start_command(name, compose_file, timeout), cwd=folder)
    for container, host_ports in expected_ports(name, doc).items():
        compose.ensure_published(container, host_ports, recreate)
    return left()


def wait_until(
    is_ready: Callable[[], bool],
    timeout: float,
    total: float | None,
    failure: Callable[[float], str],
) -> None:
    """Poll `is_ready` every second for `timeout` seconds, then raise `WtError(failure(waited))`.

    `total` is the provider's whole deadline when part of it went to `up`; the message names
    that, not the leftover this loop was given, so the user reads the budget they know.
    """
    deadline = time.monotonic() + timeout
    while not is_ready():
        if time.monotonic() >= deadline:
            raise WtError(failure(total or timeout))
        time.sleep(1)


def start(
    name: str,
    folder: Path,
    doc: dict[str, Any],
    timeout: int,
    wait_ready: Callable[[float, float], None],
) -> None:
    """`up`, then the provider's readiness wait with what is left of the one `timeout` deadline."""
    wait_ready(up(name, folder, doc, timeout), timeout)
