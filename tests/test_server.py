import stat
import time
from pathlib import Path

import pytest
import yaml

from wt import compose, host, proc
from wt.errors import WtError
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


PUBLISHED_DOC = {
    "services": {"db": {"image": "x", "container_name": "t-db", "ports": ["127.0.0.1:55432:5432"]}}
}
PORT_LINE = "5432/tcp -> 127.0.0.1:55432\n"


class Docker:
    """`docker compose` calls take `took` seconds; `docker port` answers from a script."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, port_answers: list[str], took: float = 0):
        self.now = 500.0
        self.took = took
        self.port_answers = port_answers
        self.compose_calls: list[list[str]] = []
        monkeypatch.setattr(proc, "run", self.run)
        monkeypatch.setattr(compose, "ensure_network", lambda: None)
        monkeypatch.setattr(time, "monotonic", lambda: self.now)

    def run(self, cmd: list[str], **kwargs: object) -> proc.Result:
        if cmd[:2] == ["docker", "port"]:
            answer = (
                self.port_answers.pop(0) if len(self.port_answers) > 1 else self.port_answers[0]
            )
            return proc.Result(0, answer, "")
        self.now += self.took
        self.compose_calls.append(cmd)
        return proc.Result(0, "", "")


def test_published_ports_cost_exactly_one_compose_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker(monkeypatch, [PORT_LINE])
    server.up("t-db", tmp_path, PUBLISHED_DOC, 30)
    assert len(docker.compose_calls) == 1
    assert "--force-recreate" not in docker.compose_calls[0]


def test_missing_ports_recreate_the_container_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker(monkeypatch, ["", PORT_LINE])
    server.up("t-db", tmp_path, PUBLISHED_DOC, 30)
    file = tmp_path / "compose.yml"
    assert docker.compose_calls[1] == [
        "docker", "compose", "-p", "t-db", "-f", str(file),
        "up", "-d", "--force-recreate", "--wait", "--wait-timeout", "30",
    ]  # fmt: skip
    assert len(docker.compose_calls) == 2


def test_recreate_gets_only_what_the_deadline_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker(monkeypatch, ["", PORT_LINE], took=50)
    left = server.up("t-db", tmp_path, PUBLISHED_DOC, 60)
    assert docker.compose_calls[1][-1] == "10"
    assert left == 1.0  # both starts together used more than the deadline


def test_recreate_never_gets_less_than_the_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker(monkeypatch, ["", PORT_LINE], took=75)
    server.up("t-db", tmp_path, PUBLISHED_DOC, 60)
    assert docker.compose_calls[1][-1] == "1"


def test_ports_still_missing_after_the_recreate_fail_with_the_container_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = Docker(monkeypatch, [""])
    monkeypatch.setattr(host, "port_free", lambda port: True)
    with pytest.raises(WtError, match="t-db does not publish port 55432"):
        server.up("t-db", tmp_path, PUBLISHED_DOC, 30)
    assert len(docker.compose_calls) == 2  # recreated once, not in a loop


def test_a_port_taken_by_another_program_is_named_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Docker(monkeypatch, [""])
    monkeypatch.setattr(host, "port_free", lambda port: False)
    with pytest.raises(WtError, match="port 55432 is already in use by another program"):
        server.up("t-db", tmp_path, PUBLISHED_DOC, 30)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("127.0.0.1:55432:5432", {55432}),
        ("55432:5432", {55432}),
        ("127.0.0.1:55432:5432/tcp", {55432}),
        ("5432", set()),  # Docker picks the host port
        (5432, set()),
    ],
)
def test_expected_ports_reads_every_form_of_a_ports_entry(
    entry: object, expected: set[int]
) -> None:
    doc = {"services": {"db": {"container_name": "t-db", "ports": [entry]}}}
    assert server.expected_ports("t", doc) == ({"t-db": expected} if expected else {})


def test_expected_ports_derives_the_container_name_the_way_compose_does() -> None:
    doc = {"services": {"db": {"ports": ["55432:5432"]}, "other": {"image": "x"}}}
    assert server.expected_ports("t", doc) == {"t-db-1": {55432}}


def fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Time moves only when the code sleeps; the list holds every sleep."""
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        now[0] += seconds
        slept.append(seconds)

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", sleep)
    return slept


def test_wait_until_returns_as_soon_as_the_probe_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = fake_clock(monkeypatch)
    answers = iter([False, False, True])
    server.wait_until(lambda: next(answers), 10, None, lambda waited: "late")
    assert slept == [1, 1]


def test_wait_until_names_the_total_deadline_not_the_leftover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clock(monkeypatch)
    with pytest.raises(WtError, match=r"gave up after 60s"):
        server.wait_until(lambda: False, 5, 60, lambda waited: f"gave up after {waited:.0f}s")


def test_wait_until_names_its_own_timeout_when_there_is_no_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clock(monkeypatch)
    with pytest.raises(WtError, match=r"gave up after 5s"):
        server.wait_until(lambda: False, 5, None, lambda waited: f"gave up after {waited:.0f}s")


def test_start_gives_the_readiness_wait_what_the_up_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "up", lambda name, folder, doc, timeout: 7.0)
    seen: list[tuple[float, float]] = []
    server.start("t-redis", tmp_path, {}, 30, lambda left, total: seen.append((left, total)))
    assert seen == [(7.0, 30)]
