import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from textual.widgets import DataTable, ListView, RichLog

from wt.errors import WtError
from wt.orchestrator import BranchStatus, ServiceReport
from wt.tui import WtApp


class FakeOrch:
    def __init__(self, rows: list[BranchStatus] | None = None) -> None:
        self.cfg: Any = SimpleNamespace(project="demo")
        self.rows = rows or []
        self.calls = 0
        self.error: str | None = None

    def ls(self, *, probe: bool = True) -> list[BranchStatus]:
        self.calls += 1
        if self.error:
            raise WtError(self.error)
        return self.rows

    def up(self, branch: str) -> object:
        return []

    def down(self, branch: str, *, force: bool = False) -> bool:
        return True


def rows() -> list[BranchStatus]:
    return [
        BranchStatus(
            "feat-a",
            "up",
            2,
            2,
            [
                ServiceReport("web", "ok", "200", "https://a.demo.localhost", "run"),
                ServiceReport("api", "fail", "502", None, "compose"),
            ],
        ),
        BranchStatus("main", "down", 0, 0, [ServiceReport("web", "skipped", "", None, "run")]),
    ]


async def settle(pilot: Any) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def table_cells(app: WtApp) -> list[list[str]]:
    table = app.query_one("#services", DataTable)
    return [[str(c) for c in table.get_row_at(i)[:3]] for i in range(table.row_count)]


def test_lists_branches_and_services_of_selection() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch(rows()))
        async with app.run_test() as pilot:
            await settle(pilot)
            assert len(app.query_one("#branches", ListView)) == 2
            assert app.selected_branch == "feat-a"
            assert table_cells(app) == [
                ["web", "run", "https://a.demo.localhost"],
                ["api", "compose", "-"],
            ]
            await pilot.press("down")
            await settle(pilot)
            assert app.selected_branch == "main"
            assert table_cells(app) == [["web", "run", "-"]]

    asyncio.run(scenario())


def test_r_refreshes_and_keeps_selection() -> None:
    async def scenario() -> None:
        orch = FakeOrch(rows())
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("down")
            before = orch.calls
            await pilot.press("r")
            await settle(pilot)
            assert orch.calls > before
            assert app.selected_branch == "main"

    asyncio.run(scenario())


def test_new_branch_appears_on_refresh() -> None:
    async def scenario() -> None:
        orch = FakeOrch(rows()[:1])
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            orch.rows = rows()
            await pilot.press("r")
            await settle(pilot)
            assert len(app.query_one("#branches", ListView)) == 2

    asyncio.run(scenario())


def test_error_goes_to_log_and_app_stays_open() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.error = "boom"
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            log = app.query_one("#log", RichLog)
            assert any("boom" in line.text for line in log.lines)

    asyncio.run(scenario())


def test_auto_refresh_calls_ls_again(monkeypatch: Any) -> None:
    monkeypatch.setattr("wt.tui.REFRESH_SECONDS", 0.05)

    async def scenario() -> None:
        orch = FakeOrch(rows())
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await asyncio.sleep(0.3)
            await settle(pilot)
            assert orch.calls >= 2

    asyncio.run(scenario())


def test_q_quits() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch(rows()))
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("q")
            await pilot.pause()
        assert not app.is_running

    asyncio.run(scenario())


class ActionOrch(FakeOrch):
    def __init__(self) -> None:
        super().__init__(rows())
        self.actions: list[tuple[str, str, bool]] = []
        self.down_errors: list[str] = []
        self.gate: threading.Event | None = None
        self.emit: Any = None

    def up(self, branch: str) -> list[ServiceReport]:
        if self.gate:
            self.gate.wait(5)
        if self.emit:
            self.emit()
        self.actions.append(("up", branch, False))
        return []

    def down(self, branch: str, *, force: bool = False) -> bool:
        self.actions.append(("down", branch, force))
        if self.down_errors and not force:
            raise WtError(self.down_errors[0])
        return True


def log_text(app: WtApp) -> str:
    return "\n".join(line.text for line in app.query_one("#log", RichLog).lines)


def test_u_ups_selected_branch() -> None:
    async def scenario() -> None:
        orch = ActionOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("down", "u")
            await settle(pilot)
            assert orch.actions == [("up", "main", False)]
            assert "done" in log_text(app)

    asyncio.run(scenario())


def test_n_asks_for_name_and_cancel_does_nothing() -> None:
    async def scenario() -> None:
        orch = ActionOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("n", "escape")
            await settle(pilot)
            assert orch.actions == []
            await pilot.press("n", *"feat-x", "enter")
            await settle(pilot)
            assert orch.actions == [("up", "feat-x", False)]

    asyncio.run(scenario())


def test_d_confirms_and_cancel_does_nothing() -> None:
    async def scenario() -> None:
        orch = ActionOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("d", "n")
            await settle(pilot)
            assert orch.actions == []
            await pilot.press("d", "y")
            await settle(pilot)
            assert orch.actions == [("down", "feat-a", False)]

    asyncio.run(scenario())


def test_force_only_after_second_confirm() -> None:
    async def scenario() -> None:
        orch = ActionOrch()
        orch.down_errors = ["uncommitted changes in repo api"]
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("d", "y")
            await settle(pilot)
            assert orch.actions == [("down", "feat-a", False)]
            assert "uncommitted changes" in log_text(app)
            await pilot.press("n")  # decline force
            await settle(pilot)
            assert orch.actions == [("down", "feat-a", False)]
            await pilot.press("d", "y")
            await settle(pilot)
            await pilot.press("y")  # accept force
            await settle(pilot)
            assert orch.actions[-1] == ("down", "feat-a", True)

    asyncio.run(scenario())


def test_keys_ignored_while_busy_and_log_streams() -> None:
    async def scenario() -> None:
        orch = ActionOrch()
        orch.gate = threading.Event()
        app = WtApp(orch)

        def emit() -> None:
            app.log_threadsafe("[bold]hello[/]")
            app.out_threadsafe("[x] raw")

        orch.emit = emit
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("u")
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            assert "busy" in log_text(app)
            orch.gate.set()
            await settle(pilot)
            assert orch.actions == [("up", "feat-a", False)]
            text = log_text(app)
            assert "hello" in text
            assert "[x] raw" in text

    asyncio.run(scenario())


def test_unexpected_error_shown_and_app_stays_open() -> None:
    async def scenario() -> None:
        orch = ActionOrch()

        def boom() -> None:
            raise RuntimeError("kaput")

        orch.emit = boom
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
            assert "kaput" in log_text(app)
            assert app.is_running

    asyncio.run(scenario())
