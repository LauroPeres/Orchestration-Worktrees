import stat
import time
from pathlib import Path

import pytest
import yaml

from wt import compose, proc
from wt.providers import server


def test_up_writes_compose_ensures_network_and_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    calls: list[tuple[list[str], Path | None]] = []

    def run(cmd: list[str], **kwargs: object) -> proc.Result:
        order.append("run")
        calls.append((cmd, kwargs.get("cwd")))  # type: ignore[arg-type]
        return proc.Result(0, "", "")

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(compose, "ensure_network", lambda: order.append("network"))
    folder = tmp_path / "srv"
    server.up("t-redis", folder, {"services": {"a": {"image": "x"}}}, 30)

    file = folder / "compose.yml"
    assert yaml.safe_load(file.read_text()) == {"services": {"a": {"image": "x"}}}
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert order == ["network", "run"]
    cmd, cwd = calls[0]
    assert cmd == [
        "docker", "compose", "-p", "t-redis", "-f", str(file),
        "up", "-d", "--wait", "--wait-timeout", "30",
    ]  # fmt: skip
    assert cwd == folder


class Clock:
    def __init__(self) -> None:
        self.now = 500.0


def run_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, took: float, timeout: int) -> float:
    clock = Clock()

    def run(cmd: list[str], **kwargs: object) -> proc.Result:
        clock.now += took
        return proc.Result(0, "", "")

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    return server.up("t-redis", tmp_path, {"services": {}}, timeout)


def test_up_returns_the_seconds_left_of_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_up(tmp_path, monkeypatch, took=50, timeout=60) == pytest.approx(10)


def test_up_never_returns_less_than_one_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_up(tmp_path, monkeypatch, took=59.5, timeout=60) == 1.0
    assert run_up(tmp_path, monkeypatch, took=75, timeout=60) == 1.0
