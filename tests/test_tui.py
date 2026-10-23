import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from textual.widgets import DataTable, Static

from wt.errors import WtError
from wt.orchestrator import BranchStatus
from wt.tui import CONFIRM_REMOVE, ProjectEntry, WtApp


def status(branch: str, state: str, ports: list[int] | None = None) -> BranchStatus:
    row = BranchStatus(branch, state, 0, 0)
    if ports is not None:
        row.ports = ports  # type: ignore[attr-defined]
    return row


class FakeOrch:
    def __init__(self, project: str = "demo", rows: list[BranchStatus] | None = None) -> None:
        self.cfg: Any = SimpleNamespace(project=project)
        self.rows = (
            rows
            if rows is not None
            else [status("feat-a", "up", [8001, 8002]), status("main", "stopped")]
        )
        self.calls: list[tuple[Any, ...]] = []
        self.ls_count = 0
        self.error: Exception | None = None
        self.gate: threading.Event | None = None

    def ls(self, *, probe: bool = True) -> list[BranchStatus]:
        self.ls_count += 1
        return self.rows

    def _act(self, *call: Any) -> None:
        self.calls.append(call)
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error

    def up(self, branch: str) -> object:
        self._act("up", branch)
        return []

    def stop(self, branch: str) -> object:
        self._act("stop", branch)
        return True

    def add(self, branch: str) -> object:
        self._act("add", branch)
        return True

    def remove(self, branch: str, *, force: bool = False) -> object:
        self._act("remove", branch, force)
        return True


async def settle(pilot: Any) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def cells(app: WtApp) -> list[list[str]]:
    table = app.query_one("#table", DataTable)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


def message(app: WtApp) -> str:
    return str(app.query_one("#message", Static).render())


def test_rows_and_columns() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch())
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#table", DataTable)
            assert [str(c.label) for c in table.columns.values()] == ["NAME", "STATUS", "PORT"]
            assert cells(app) == [["feat-a", "up", "8001, 8002"], ["main", "stopped", "-"]]
            assert "u up  d down  a add  r remove  q quit" in str(
                app.query_one("#actions", Static).render()
            )

    asyncio.run(scenario())


def test_u_and_d_call_up_and_stop_of_selected() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("u")
            await settle(pilot)
            await pilot.press("down", "d")
            await settle(pilot)
            assert orch.calls == [("up", "feat-a"), ("stop", "main")]
            assert message(app) == "down main: done"

    asyncio.run(scenario())


def test_a_asks_for_name_and_cancel_does_nothing() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a", "escape")
            await settle(pilot)
            assert orch.calls == []
            await pilot.press("a", *"new-x", "enter")
            await settle(pilot)
            assert orch.calls == [("add", "new-x")]

    asyncio.run(scenario())


def test_r_confirms_then_removes_with_force() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("r")
            await pilot.pause()
            assert CONFIRM_REMOVE.format(branch="feat-a").startswith("remove feat-a: deletes")
            await pilot.press("n")
            await settle(pilot)
            await pilot.press("r", "enter")  # Enter is "no" (default N)
            await settle(pilot)
            assert orch.calls == []
            await pilot.press("r", "y")
            await settle(pilot)
            assert orch.calls == [("remove", "feat-a", True)]

    asyncio.run(scenario())


def test_busy_note_and_error_message() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.gate = threading.Event()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("u")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert "busy" in message(app)
            assert orch.calls == [("up", "feat-a")]
            orch.error = WtError("boom")
            orch.gate.set()
            await settle(pilot)
            assert message(app) == "boom"
            assert app.is_running

    asyncio.run(scenario())


def test_refresh_after_action_and_on_timer() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            before = orch.ls_count
            orch.rows = [status("feat-a", "up", [8001])]
            await pilot.press("u")
            await settle(pilot)
            assert orch.ls_count > before
            assert cells(app) == [["feat-a", "up", "8001"]]

    asyncio.run(scenario())


def test_q_quits() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch())
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("q")
            await pilot.pause()
        assert not app.is_running

    asyncio.run(scenario())


def test_all_projects_names_and_add_uses_selected_project() -> None:
    async def scenario() -> None:
        shop, notes = FakeOrch("shop"), FakeOrch("notes", [status("main", "up", [9000])])
        orchs = {"shop": shop, "notes": notes}

        def discover() -> list[ProjectEntry]:
            entries = [ProjectEntry(n, lambda o=o: o, "") for n, o in orchs.items()]  # type: ignore[misc]
            return [*entries, ProjectEntry("gone", None, "config not found")]

        app = WtApp(projects=discover)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert [c[0] for c in cells(app)] == [
                "gone", "notes/main", "shop/feat-a", "shop/main",
            ]  # fmt: skip
            await pilot.press("down", "down", "a", *"zz", "enter")
            await settle(pilot)
            assert shop.calls == [("add", "zz")]
            await pilot.press("up", "up", "u")  # stale project row: no call
            await settle(pilot)
            assert "config not found" in message(app)

    asyncio.run(scenario())


def test_cells_have_no_color_markup() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch(rows=[status("[red]x[/]", "up")]))
        async with app.run_test() as pilot:
            await settle(pilot)
            assert cells(app) == [["[red]x[/]", "up", "-"]]
            table = app.query_one("#table", DataTable)
            for cell in table.get_row_at(0):
                assert not cell.spans

    asyncio.run(scenario())
