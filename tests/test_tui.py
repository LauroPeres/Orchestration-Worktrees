import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.style import Style
from textual.widgets import DataTable, Label, Log, SelectionList, Static

from wt.errors import WtError
from wt.messages import CONFIRM_REMOVE
from wt.naming import Names
from wt.orchestrator import BranchStatus, Choice, RepoRow
from wt.tui import (
    CONFIRM_DOWN,
    ConfigureScreen,
    ConfirmScreen,
    InputScreen,
    LogScreen,
    ProjectEntry,
    WtApp,
    status_style,
)


def status(branch: str, state: str, ports: list[int] | None = None) -> BranchStatus:
    row = BranchStatus(branch, state, 0, 0)
    if ports is not None:
        row.ports = ports
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
        self.ls_gate: threading.Event | None = None  # holds `ls` after it has read the rows
        self.service_rows: list[RepoRow] = [
            RepoRow("front", "healthy", 10001, "https://front.demo.localhost"),
            RepoRow("back", "starting", 10002, None),
            RepoRow("db", "stopped", None, None),
        ]
        self.service_calls: list[str] = []
        self.services_error: Exception | None = None
        self.choices: list[Choice] = [
            Choice("front", "web", True),
            Choice("back", "api", True),
            Choice("db", "api", False),
        ]
        # what `catalog_choices(None)` answers: nothing selected, back is a dependency of front
        self.new_choices: list[Choice] = [
            Choice("front", "web", False),
            Choice("back", "api", False, required_by=("front",)),
            Choice("db", "api", False),
        ]
        self.choices_error: Exception | None = None
        self.logs_dir = Path("logs-not-set")

    def names(self, branch: str) -> Names:
        return Names(self.cfg.project, branch, f"{self.cfg.project}.localhost")

    def log_path(self, names: Names, service: str) -> Path:
        return self.logs_dir / names.slug / f"{service}.log"

    def ls(self, *, probe: bool = True) -> list[BranchStatus]:
        self.ls_count += 1
        rows = self.rows
        if self.ls_gate is not None:
            self.ls_gate.wait(5)
        return rows

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

    def add(self, branch: str, uses: list[str]) -> object:
        self._act("add", branch, list(uses))
        return True

    def down(self, branch: str, *, force: bool = False) -> object:
        self._act("down", branch, force)
        return True

    def remove(self, branch: str, *, force: bool = False) -> object:
        self._act("remove", branch, force)
        return True

    def services(self, branch: str) -> list[RepoRow]:
        self.service_calls.append(branch)
        if self.services_error is not None:
            raise self.services_error
        return self.service_rows

    def catalog_choices(self, branch: str | None = None) -> list[Choice]:
        if self.choices_error is not None:
            raise self.choices_error
        return self.new_choices if branch is None else self.choices

    def configure(self, branch: str, uses: list[str]) -> None:
        self._act("configure", branch, list(uses))


async def settle(pilot: Any) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def cells(app: WtApp) -> list[list[str]]:
    table = app.query_one("#table", DataTable)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


def message(app: WtApp) -> str:
    return str(app.query_one("#message", Static).render())


ANSI_GREEN = 2  # Rich's number for the terminal's own green
ANSI_BLACK = 0


def is_green(style: Any) -> bool:
    """Green in the terminal's own palette: a Rich Style (strips) or a Textual Style (Content)."""
    if isinstance(style, Style):
        return style.color is not None and style.color.number == ANSI_GREEN
    foreground = getattr(style, "foreground", None)  # textual.style.Style on a Static's Content
    return foreground is not None and getattr(foreground, "ansi", None) == ANSI_GREEN


def is_green_bar(style: Style) -> bool:
    """Black text on a green background: the selected row or option."""
    black = style.color is not None and style.color.number == ANSI_BLACK
    green = style.bgcolor is not None and style.bgcolor.number == ANSI_GREEN
    return black and green and not style.reverse


def green_keys(text: Any) -> list[str]:
    """The words of a hint line that are green; asserts that nothing else is styled."""
    assert all(is_green(span.style) for span in text.spans)
    return [text.plain[span.start : span.end] for span in text.spans]


def rendered_style(app: WtApp, word: str) -> Style:
    """The style the terminal gets for the first piece of text on screen that contains `word`."""
    for strip in app.screen._compositor.render_strips():
        for segment in strip:
            if word in segment.text:
                return segment.style or Style()
    raise AssertionError(f"{word!r} is not on screen")


def test_rows_and_columns() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch())
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#table", DataTable)
            assert [str(c.label) for c in table.columns.values()] == ["NAME", "STATUS"]
            assert cells(app) == [["feat-a", "up"], ["main", "stopped"]]
            actions: Any = app.query_one("#actions", Static).render()
            assert actions.plain == (
                "enter: services  u: up  d: stop  x: down  a: add  r: remove  q: quit"
            )
            assert green_keys(actions) == ["enter", "u", "d", "x", "a", "r", "q"]  # words are plain

    asyncio.run(scenario())


def test_the_table_has_a_plain_border_and_green_column_names() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch())
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#table", DataTable)
            assert table.styles.border_top[0] == "solid"
            assert is_green(rendered_style(app, "NAME"))
            assert is_green(rendered_style(app, "STATUS"))
            assert not is_green(rendered_style(app, "stopped"))  # cells keep the terminal's colors
            assert is_green_bar(rendered_style(app, "feat-a"))  # the selected row
            assert not is_green_bar(rendered_style(app, "stopped"))
            assert not is_green_bar(rendered_style(app, "NAME"))  # green text, not a bar

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
            assert message(app) == "stop main: done"

    asyncio.run(scenario())


def page_title(app: WtApp) -> str:
    return str(app.screen.query_one("#title", Label).render())


def test_each_action_is_a_page_of_its_own_and_cancel_returns_to_the_table() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            table_page = app.screen
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, InputScreen) and app.screen is not table_page
            assert page_title(app) == "Add a worktree" and not app.screen.query("#table")
            await pilot.press(*"new-x", "enter")
            await pilot.pause()
            assert isinstance(app.screen, ConfigureScreen) and not app.screen.query("#table")
            pages = len(app.screen_stack)
            await pilot.press("a", "r", "u", "d", "x")  # the table's keys do nothing here
            await pilot.pause()
            assert isinstance(app.screen, ConfigureScreen) and len(app.screen_stack) == pages
            assert green_keys(app.screen.query_one("#hints", Static).render()) == [
                "space", "enter", "esc",
            ]  # fmt: skip
            await pilot.press("escape")
            await settle(pilot)
            assert app.screen is table_page and app.query_one("#table", DataTable)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and id(app.screen) != id(table_page)
            assert page_title(app) == "Remove a worktree" and not app.screen.query("#table")
            pages = len(app.screen_stack)
            await pilot.press("a", "r")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and len(app.screen_stack) == pages
            await pilot.press("escape")
            await settle(pilot)
            assert id(app.screen) == id(table_page) and orch.calls == []

    asyncio.run(scenario())


async def add_worktree(pilot: Any, name: str, *keys: str) -> None:
    """`a`, type `name`, Enter, then the keys pressed on the services checklist."""
    await pilot.press("a", *name, "enter")
    await pilot.pause()
    if keys:
        await pilot.press(*keys)
    await settle(pilot)


def test_a_asks_for_name_then_services_and_adds_without_starting() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a")
            await pilot.pause()
            prompt = app.screen.query_one("#dialog Label", Label)
            assert str(prompt.render()) == "Enter name"
            await pilot.press(*"new-x", "enter")
            await pilot.pause()
            assert page_title(app) == "Services for new-x"
            assert checked(app) == []  # a new worktree starts with nothing selected
            await pilot.press("space", "enter")  # front
            await settle(pilot)
            assert orch.calls == [("add", "new-x", ["front"])]  # never an `up`
            assert message(app) == "add new-x: done"

    asyncio.run(scenario())


def test_cancelling_the_name_or_the_services_has_no_side_effect() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a", "escape")  # at the name
            await settle(pilot)
            assert orch.calls == [] and app.screen is app.screen_stack[0]
            await pilot.press("a", *"new-x", "enter")
            await pilot.pause()
            await pilot.press("space", "escape")  # at the services, with a box checked
            await settle(pilot)
            assert orch.calls == [] and app.screen is app.screen_stack[0]
            assert message(app) == ""
            await add_worktree(pilot, "new-y", "down", "down", "space", "enter")
            assert orch.calls == [("add", "new-y", ["db"])]

    asyncio.run(scenario())


def test_an_empty_name_never_reaches_the_services_step() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a", "enter")
            await settle(pilot)
            assert not app.screen.query("#choices") and orch.calls == []

    asyncio.run(scenario())


def test_checking_a_consumer_checks_and_locks_its_dependencies_on_add() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a", *"new-x", "enter")
            await pilot.pause()
            await pilot.press("space")  # front
            await pilot.pause()
            assert checked(app) == ["front", "back"]
            assert labels(app) == ["front (web)", "back (api) (required by front)", "db (api)"]
            await pilot.press("down", "space")  # back is locked
            await pilot.pause()
            assert checked(app) == ["front", "back"]
            assert str(app.screen.query_one("#error", Static).render()) == (
                "back is required by front"
            )
            await pilot.press("up", "space")  # uncheck front: back is free but stays checked
            await pilot.pause()
            assert checked(app) == ["back"]
            assert labels(app)[1] == "back (api)"
            await pilot.press("down", "space", "up", "space")  # back off, front on: back returns
            await pilot.pause()
            assert sorted(checked(app)) == ["back", "front"]
            assert labels(app)[1] == "back (api) (required by front)"
            await pilot.press("enter")
            await settle(pilot)
            assert orch.calls == [("add", "new-x", ["front"])]  # explicit = checked minus locked

    asyncio.run(scenario())


def test_dependencies_are_followed_down_a_chain() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.new_choices = [
            Choice("c", "api", False, required_by=("b",)),
            Choice("b", "api", False, required_by=("a",)),
            Choice("a", "web", False),
        ]
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a", *"new-x", "enter")
            await pilot.pause()
            await pilot.press("down", "down", "space")  # a
            await pilot.pause()
            assert sorted(checked(app)) == ["a", "b", "c"]
            assert labels(app) == [
                "c (api) (required by b)",
                "b (api) (required by a)",
                "a (web)",
            ]
            await pilot.press("enter")
            await settle(pilot)
            assert orch.calls == [("add", "new-x", ["a"])]

    asyncio.run(scenario())


def test_a_one_service_catalog_is_added_with_that_service() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.new_choices = [Choice("app", "app", False)]
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await add_worktree(pilot, "new-x", "space", "enter")
            assert orch.calls == [("add", "new-x", ["app"])]

    asyncio.run(scenario())


def test_confirming_with_nothing_checked_asks_for_a_service() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a", *"new-x", "enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert str(app.screen.query_one("#error", Static).render()) == (
                "check at least one service"
            )
            assert orch.calls == []

    asyncio.run(scenario())


def test_add_error_from_the_core_is_shown_and_the_app_stays_open() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.error = WtError("branch name is not valid")
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await add_worktree(pilot, "bad", "space", "enter")
            assert message(app) == "branch name is not valid"
            assert app.is_running

    asyncio.run(scenario())


def test_add_says_so_when_the_catalog_cannot_be_read() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices_error = WtError("no catalog at /x/catalog.yaml. Run wt init")
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a", *"new-x", "enter")
            await settle(pilot)
            assert message(app) == "no catalog at /x/catalog.yaml. Run wt init"
            assert not app.screen.query("#choices") and orch.calls == []

    asyncio.run(scenario())


def test_add_without_catalog_services_says_so() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.new_choices = []
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a", *"new-x", "enter")
            await settle(pilot)
            assert message(app) == "demo: no catalog services to choose from"
            assert orch.calls == []

    asyncio.run(scenario())


def test_add_is_blocked_while_an_action_runs() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.gate = threading.Event()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("u")
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            assert "busy" in message(app) and app.screen is app.screen_stack[0]
            orch.gate.set()
            await settle(pilot)

    asyncio.run(scenario())


def test_without_a_catalog_a_has_nothing_to_add_to_and_says_where_it_looked() -> None:
    async def scenario() -> None:
        app = WtApp(projects=lambda: [], missing_catalog=Path("/x/catalog.yaml"))
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            assert message(app) == "no catalog at /x/catalog.yaml. Run wt init or wt migrate"
            assert app.screen is app.screen_stack[0]

    asyncio.run(scenario())


def test_without_a_catalog_a_still_adds_to_the_selected_project() -> None:
    async def scenario() -> None:
        shop = FakeOrch("shop")
        app = WtApp(
            projects=lambda: [ProjectEntry("shop", lambda: shop, "")],
            missing_catalog=Path("/x/catalog.yaml"),
        )
        async with app.run_test() as pilot:
            await settle(pilot)
            await add_worktree(pilot, "zz", "space", "enter")
            assert shop.calls == [("add", "zz", ["front"])]

    asyncio.run(scenario())


REMOVE_QUESTION = (
    "remove feat-a: removes what wt created for it (containers, volumes, databases, "
    "worktrees) and deletes the git branch in every repo of the project where it exists; "
    "unpushed commits are lost. Remove? y/N"
)


def test_r_confirms_then_removes_with_force() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("r")
            await pilot.pause()
            assert CONFIRM_REMOVE.format(branch="feat-a") == REMOVE_QUESTION
            shown = str(app.screen.query_one("#dialog Label", Label).render())
            assert shown.replace("\n", " ") == REMOVE_QUESTION
            assert orch.calls == []  # nothing runs before the answer
            await pilot.press("n")
            await settle(pilot)
            await pilot.press("r", "escape")
            await settle(pilot)
            await pilot.press("r", "enter")  # Enter is "no" (default N)
            await settle(pilot)
            assert orch.calls == []
            await pilot.press("r", "y")
            await settle(pilot)
            assert orch.calls == [("remove", "feat-a", True)]

    asyncio.run(scenario())


DOWN_QUESTION = (
    "down feat-a: stops what wt started and removes what wt created for it (containers, "
    "volumes, databases, worktrees); the git branch is kept. Down? y/N"
)


def test_x_confirms_on_its_own_page_then_downs_without_force() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            table_page = app.screen
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and app.screen is not table_page
            assert page_title(app) == "Down a worktree" and not app.screen.query("#table")
            assert CONFIRM_DOWN.format(branch="feat-a") == DOWN_QUESTION
            shown = str(app.screen.query_one("#dialog Label", Label).render())
            assert shown.replace("\n", " ") == DOWN_QUESTION
            assert orch.calls == []  # nothing runs before the answer
            await pilot.press("n")
            await settle(pilot)
            await pilot.press("x", "escape")
            await settle(pilot)
            await pilot.press("x", "enter")  # Enter is "no" (default N)
            await settle(pilot)
            assert orch.calls == [] and app.screen is table_page
            await pilot.press("x", "y")
            await settle(pilot)
            assert orch.calls == [("down", "feat-a", False)]
            assert message(app) == "down feat-a: done"

    asyncio.run(scenario())


def test_a_refused_down_shows_the_error_and_the_force_hint() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.error = WtError("feat-a has uncommitted changes in api")
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("x", "y")
            await settle(pilot)
            assert orch.calls == [("down", "feat-a", False)]
            assert message(app) == "feat-a has uncommitted changes in api; use wt down --force"
            assert app.is_running

    asyncio.run(scenario())


def test_the_other_errors_do_not_get_the_force_hint() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.error = WtError("boom")
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("d")  # stop
            await settle(pilot)
            assert message(app) == "boom"

    asyncio.run(scenario())


def screen_lines(app: WtApp) -> list[str]:
    """What the terminal shows right now, one string per screen row (after clipping)."""
    return [strip.text for strip in app.screen._compositor.render_strips()]


def dialog_text(app: WtApp) -> tuple[str, int]:
    """The words visible inside the `#dialog` box, and the box height."""
    region = app.screen.query_one("#dialog").region
    rows = screen_lines(app)[region.y : region.y + region.height]
    return " ".join(" ".join(rows).split()), region.height


def test_remove_confirmation_wraps_so_the_whole_sentence_is_visible() -> None:
    async def scenario(size: tuple[int, int]) -> None:
        app = WtApp(FakeOrch())
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await pilot.press("r")
            await pilot.pause()
            text, height = dialog_text(app)
            assert text == REMOVE_QUESTION
            assert 1 < height <= size[1]
            assert "unpushed commits are lost. Remove? y/N" in text

    for size in [(100, 30), (60, 20)]:
        asyncio.run(scenario(size))


def test_action_line_keeps_every_hint_on_a_narrow_screen() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch())
        async with app.run_test(size=(40, 15)) as pilot:
            await settle(pilot)
            shown = " ".join(" ".join(screen_lines(app)).split())
            assert "r: remove" in shown and "q: quit" in shown

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
            orch.rows = [status("feat-a", "stopped")]
            await pilot.press("u")
            await settle(pilot)
            assert orch.ls_count > before
            assert cells(app) == [["feat-a", "stopped"]]

    asyncio.run(scenario())


def test_the_table_refreshes_when_a_worker_finishes_even_while_a_refresh_is_running() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            orch.ls_gate = threading.Event()
            app.action_refresh()  # a refresh is now in flight and holds the old rows
            await pilot.pause()
            orch.rows = [status("feat-a", "stopped"), status("main", "stopped")]
            await pilot.press("u")
            for _ in range(100):
                await pilot.pause()
                if message(app) == "up feat-a: done":
                    break
            assert message(app) == "up feat-a: done"
            orch.ls_gate.set()
            await settle(pilot)
            assert cells(app) == [["feat-a", "stopped"], ["main", "stopped"]]  # not the 2 s tick

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
            assert [c[0] for c in cells(app)] == ["gone", "main", "feat-a", "main"]
            await pilot.press("down", "down")
            await add_worktree(pilot, "zz", "space", "enter")
            assert shop.calls == [("add", "zz", ["front"])]
            await pilot.press("up", "up", "u")  # stale project row: no call
            await settle(pilot)
            assert "config not found" in message(app)

    asyncio.run(scenario())


LONG_ERROR = (
    "invalid catalog.yaml:\nservice 'front': run mentions {{ nope }} which is not a template "
    "variable, so wt cannot start it. Fix the template in the catalog and try again."
)


class BrokenOrch(FakeOrch):
    def ls(self, *, probe: bool = True) -> list[BranchStatus]:
        raise WtError(LONG_ERROR)


def test_a_project_that_fails_to_load_shows_its_full_error_when_selected() -> None:
    async def scenario() -> None:
        shop = FakeOrch("shop")

        def discover() -> list[ProjectEntry]:
            return [
                ProjectEntry("bad", lambda: BrokenOrch("bad"), ""),
                ProjectEntry("shop", lambda: shop, ""),
            ]

        app = WtApp(projects=discover)
        async with app.run_test(size=(50, 24)) as pilot:
            await settle(pilot)
            assert cells(app)[0] == ["bad", "invalid catalog.yaml:"]  # the row keeps the first line
            assert message(app) == f"bad: {LONG_ERROR}"  # selected at start: the whole text
            shown = " ".join(" ".join(screen_lines(app)).split())
            assert "Fix the template in the catalog and try again." in shown  # wrapped, not cut
            await pilot.press("down")
            await pilot.pause()
            assert message(app) == ""  # another row: the error no longer applies
            await pilot.press("up")
            await pilot.pause()
            assert message(app) == f"bad: {LONG_ERROR}"

    asyncio.run(scenario())


def test_the_error_of_a_broken_project_does_not_replace_an_action_message_on_refresh() -> None:
    async def scenario() -> None:
        shop = FakeOrch("shop")
        app = WtApp(
            projects=lambda: [
                ProjectEntry("bad", lambda: BrokenOrch("bad"), ""),
                ProjectEntry("shop", lambda: shop, ""),
            ]
        )
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("down", "u")  # shop/feat-a
            await settle(pilot)
            assert message(app) == "up feat-a: done"
            app.action_refresh()
            await settle(pilot)
            assert message(app) == "up feat-a: done"

    asyncio.run(scenario())


def cell_color(cell: Any) -> str | None:
    """The color a table cell is written in, or None for the terminal's own."""
    style = cell.style
    return style.color.name if isinstance(style, Style) and style.color else None


def test_the_name_is_plain_text_and_the_status_has_its_color() -> None:
    async def scenario() -> None:
        rows = [
            status("[red]x[/]", "up"),
            status("b-failed", "failed"),
            status("c-degraded", "degraded"),
            status("d-creating", "creating"),
            status("e-removing", "removing"),
            status("f-stopped", "stopped"),
        ]
        app = WtApp(FakeOrch(rows=rows))
        async with app.run_test() as pilot:
            await settle(pilot)
            assert cells(app)[0] == ["[red]x[/]", "up"]  # the text is as written, no markup
            table = app.query_one("#table", DataTable)
            by_status = {}
            for index in range(table.row_count):
                name, state = table.get_row_at(index)
                assert cell_color(name) is None and not name.spans
                assert not state.spans
                by_status[str(state)] = cell_color(state)
            assert by_status == {
                "up": "green",
                "failed": "red",
                "degraded": "yellow",
                "creating": "yellow",
                "removing": "yellow",
                "stopped": None,
            }

    asyncio.run(scenario())


def test_status_style_maps_each_value_to_its_color() -> None:
    colors = {"green": ["up", "healthy", "ok"], "yellow": ["degraded", "creating", "removing"]}
    colors["red"] = ["failed", "fail", "unhealthy"]
    for color, values in colors.items():
        for value in values:
            style = status_style(value)
            assert style is not None and style.color is not None and style.color.name == color
    for value in ["stopped", "starting", "-", "anything else"]:
        assert status_style(value) is None


def services_cells(app: WtApp) -> list[list[str]]:
    table = app.screen.query_one("#services", DataTable)
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


def test_enter_opens_services_and_esc_goes_back() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            assert orch.service_calls[0] == "feat-a"
            table = app.screen.query_one("#services", DataTable)
            assert [str(c.label) for c in table.columns.values()] == [
                "NAME", "STATUS", "PORT", "URL",
            ]  # fmt: skip
            assert services_cells(app) == [
                ["front", "healthy", "10001", "https://front.demo.localhost"],
                ["back", "starting", "10002", "-"],
                ["db", "stopped", "-", "-"],
            ]
            for row in range(table.row_count):
                name, state, port, url = table.get_row_at(row)
                assert not any(cell.spans or cell.style for cell in (name, port, url))
                assert not state.spans
            assert [cell_color(table.get_row_at(i)[1]) for i in range(3)] == [
                "green", None, None,
            ]  # healthy, starting, stopped  # fmt: skip
            actions: Any = app.screen.query_one("#actions", Static).render()
            assert actions.plain == "c: configure  l: logs  esc: back  q: quit"
            assert green_keys(actions) == ["c", "l", "esc", "q"]
            await pilot.press("escape")
            await settle(pilot)
            assert cells(app) == [["feat-a", "up"], ["main", "stopped"]]

    asyncio.run(scenario())


def test_services_status_cells_have_their_color() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.service_rows = [
            RepoRow("a", "healthy", None, None),
            RepoRow("b", "unhealthy", None, None),
            RepoRow("c", "starting", None, None),
            RepoRow("d", "stopped", None, None),
        ]
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            table = app.screen.query_one("#services", DataTable)
            colors = [cell_color(table.get_row_at(i)[1]) for i in range(table.row_count)]
            assert colors == ["green", "red", None, None]
            assert [str(table.get_row_at(i)[1]) for i in range(4)] == [
                "healthy", "unhealthy", "starting", "stopped",
            ]  # fmt: skip

    asyncio.run(scenario())


def test_services_refresh_on_timer_and_error_on_message_line() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("down", "enter")
            await settle(pilot)
            assert orch.service_calls == ["main"]
            orch.services_error = WtError("boom")
            await pilot.pause(2.3)
            await settle(pilot)
            assert len(orch.service_calls) > 1
            assert str(app.screen.query_one("#message", Static).render()) == "boom"
            assert len(services_cells(app)) == 3  # last rows stay

    asyncio.run(scenario())


def test_services_in_all_projects_uses_selected_project() -> None:
    async def scenario() -> None:
        shop, notes = FakeOrch("shop"), FakeOrch("notes", [status("main", "up")])
        orchs = {"shop": shop, "notes": notes}

        def discover() -> list[ProjectEntry]:
            return [ProjectEntry(n, lambda o=o: o, "") for n, o in orchs.items()]  # type: ignore[misc]

        app = WtApp(projects=discover)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("down", "enter")  # notes/main, shop/feat-a, ...
            await settle(pilot)
            assert shop.service_calls == ["feat-a"] and notes.service_calls == []

    asyncio.run(scenario())


def log_orch(tmp_path: Path) -> FakeOrch:
    """An orchestrator whose services view lists repo `web` (service `front`) of `feat-a`."""
    orch = FakeOrch()
    orch.logs_dir = tmp_path
    orch.service_rows = [RepoRow("web", "healthy", 10001, None)]
    return orch


def write_log(orch: FakeOrch, text: str, branch: str = "feat-a") -> Path:
    path = orch.log_path(orch.names(branch), "front")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def log_lines(app: WtApp) -> list[str]:
    return list(app.screen.query_one("#log", Log).lines)


async def open_log(pilot: Any) -> None:
    await settle(pilot)
    await pilot.press("enter")
    await settle(pilot)
    await pilot.press("l")
    await settle(pilot)


def test_l_opens_a_page_with_the_log_of_the_selected_service(tmp_path: Path) -> None:
    async def scenario() -> None:
        orch = log_orch(tmp_path)
        write_log(orch, "booting\nready\n")
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_log(pilot)
            assert isinstance(app.screen, LogScreen)
            assert str(app.screen.query_one("#title", Label).render()) == "feat-a \u00b7 front"
            hints: Any = app.screen.query_one("#hints", Static).render()
            assert hints.plain == "esc: back"
            assert green_keys(hints) == ["esc"]
            assert log_lines(app) == ["booting", "ready"]
            await pilot.press("escape")
            await settle(pilot)
            assert app.screen.query("#services")  # back on the services view

    asyncio.run(scenario())


def test_the_log_page_shows_the_last_200_lines_scrolled_to_the_end(tmp_path: Path) -> None:
    async def scenario() -> None:
        orch = log_orch(tmp_path)
        write_log(orch, "".join(f"line {n}\n" for n in range(1, 251)))
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_log(pilot)
            lines = log_lines(app)
            assert (len(lines), lines[0], lines[-1]) == (200, "line 51", "line 250")
            log = app.screen.query_one("#log", Log)
            assert log.scroll_y == log.max_scroll_y

    asyncio.run(scenario())


def test_the_log_page_says_so_when_there_is_no_log_yet(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = WtApp(log_orch(tmp_path))
        async with app.run_test() as pilot:
            await open_log(pilot)
            assert log_lines(app) == ["no log yet"]

    asyncio.run(scenario())


def test_the_log_page_follows_the_file_every_second(tmp_path: Path) -> None:
    async def scenario() -> None:
        orch = log_orch(tmp_path)
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_log(pilot)
            assert log_lines(app) == ["no log yet"]
            path = write_log(orch, "first\n")
            await pilot.pause(1.3)
            assert log_lines(app) == ["first"]
            with path.open("a") as handle:
                handle.write("second\n")
            await pilot.pause(1.3)
            assert log_lines(app) == ["first", "second"]

    asyncio.run(scenario())


def test_the_table_keys_do_nothing_on_the_log_page(tmp_path: Path) -> None:
    async def scenario() -> None:
        orch = log_orch(tmp_path)
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_log(pilot)
            await pilot.press("u", "x", "r")
            await settle(pilot)
            assert orch.calls == [] and isinstance(app.screen, LogScreen)

    asyncio.run(scenario())


def test_q_quits_from_services_screen() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch())
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            await pilot.press("q")
            await pilot.pause()
        assert not app.is_running

    asyncio.run(scenario())


def checked(app: WtApp) -> list[str]:
    return list(app.screen.query_one("#choices", SelectionList).selected)


def test_c_toggle_and_confirm_calls_configure_and_refreshes() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            before = len(orch.service_calls)
            await pilot.press("c")
            await pilot.pause()
            assert checked(app) == ["front", "back"]
            hints: Any = app.screen.query_one("#hints", Static).render()
            assert hints.plain == "space: toggle  enter: confirm  esc: cancel"
            await pilot.press("space", "down", "down", "space")  # uncheck front, check db
            assert checked(app) == ["back", "db"]
            await pilot.press("enter")
            await settle(pilot)
            assert orch.calls == [("configure", "feat-a", ["back", "db"])]
            assert len(orch.service_calls) > before
            assert str(app.screen.query_one("#message", Static).render()) == (
                "configure feat-a: done"
            )

    asyncio.run(scenario())


def test_configure_cancel_does_not_call() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            await pilot.press("c", "space", "escape")
            await settle(pilot)
            assert orch.calls == []
            assert app.screen.query("#services")

    asyncio.run(scenario())


def test_configure_empty_selection_is_an_error_without_call() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            await pilot.press("c", "space", "down", "space", "enter")
            await pilot.pause()
            assert checked(app) == []
            assert str(app.screen.query_one("#error", Static).render()) == (
                "check at least one service"
            )
            assert orch.calls == []
            await pilot.press("space", "enter")  # still open: check one and confirm
            await settle(pilot)
            assert orch.calls == [("configure", "feat-a", ["back"])]

    asyncio.run(scenario())


def locked_choices() -> list[Choice]:
    return [
        Choice("front", "web", True),
        Choice("back", "api", True, required_by=("front",)),
        Choice("db", "api", False),
    ]


async def open_configure(pilot: Any) -> None:
    await settle(pilot)
    await pilot.press("enter")
    await settle(pilot)
    await pilot.press("c")
    await pilot.pause()


def test_locked_choice_is_checked_and_labelled() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices = [
            Choice("front", "web", True),
            Choice("mid", "web", True),
            Choice("back", "api", True, required_by=("front", "mid")),
        ]
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_configure(pilot)
            options = app.screen.query_one("#choices", SelectionList)
            labels = [str(options.get_option_at_index(i).prompt) for i in range(3)]
            assert labels == [
                "front (web)",
                "mid (web)",
                "back (api) (required by front, mid)",
            ]
            assert checked(app) == ["front", "mid", "back"]

    asyncio.run(scenario())


def test_space_on_locked_choice_keeps_it_checked_and_says_why() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices = locked_choices()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_configure(pilot)
            await pilot.press("down", "space")
            await pilot.pause()
            assert sorted(checked(app)) == ["back", "front"]
            assert str(app.screen.query_one("#error", Static).render()) == (
                "back is required by front"
            )

    asyncio.run(scenario())


def test_confirm_sends_checked_minus_locked() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices = locked_choices()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_configure(pilot)
            await pilot.press("down", "down", "space", "enter")  # check db
            await settle(pilot)
            assert orch.calls == [("configure", "feat-a", ["front", "db"])]

    asyncio.run(scenario())


def test_explicit_service_that_is_also_needed_keeps_its_toggle() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices = locked_choices()  # front is explicit: it has no required_by
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_configure(pilot)
            await pilot.press("space")  # uncheck front
            assert checked(app) == ["back"]
            await pilot.press("space", "down", "down", "space", "enter")  # recheck front, check db
            await settle(pilot)
            assert orch.calls == [("configure", "feat-a", ["front", "db"])]

    asyncio.run(scenario())


def labels(app: WtApp) -> list[str]:
    options = app.screen.query_one("#choices", SelectionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def test_unchecking_the_consumer_unlocks_the_dependency_and_confirm_sends_it() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices = locked_choices()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_configure(pilot)
            await pilot.press("space")  # uncheck front: back is no longer required
            await pilot.pause()
            assert labels(app)[1] == "back (api)"
            await pilot.press("down", "space")  # back is togglable now: uncheck, check again
            await pilot.press("space")
            await pilot.pause()
            assert checked(app) == ["back"]
            assert str(app.screen.query_one("#error", Static).render()) == ""
            await pilot.press("enter")
            await settle(pilot)
            assert orch.calls == [("configure", "feat-a", ["back"])]

    asyncio.run(scenario())


def test_rechecking_the_consumer_locks_the_dependency_again() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices = locked_choices()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_configure(pilot)
            await pilot.press("space", "space")  # uncheck front, check it again
            await pilot.pause()
            assert labels(app)[1] == "back (api) (required by front)"
            await pilot.press("down", "space")  # refused: still required
            await pilot.pause()
            assert sorted(checked(app)) == ["back", "front"]
            assert str(app.screen.query_one("#error", Static).render()) == (
                "back is required by front"
            )
            await pilot.press("enter")
            await settle(pilot)
            assert orch.calls == [("configure", "feat-a", ["front"])]

    asyncio.run(scenario())


def test_dependency_with_two_consumers_stays_locked_until_both_are_unchecked() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices = [
            Choice("front", "web", True),
            Choice("mid", "web", True),
            Choice("back", "api", True, required_by=("front", "mid")),
        ]
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_configure(pilot)
            await pilot.press("space")  # uncheck front: mid still needs back
            await pilot.pause()
            assert labels(app)[2] == "back (api) (required by mid)"
            await pilot.press("down", "down", "space")
            await pilot.pause()
            assert checked(app) == ["mid", "back"]
            assert str(app.screen.query_one("#error", Static).render()) == (
                "back is required by mid"
            )
            await pilot.press("up", "space")  # uncheck mid: back is free
            await pilot.pause()
            assert labels(app)[2] == "back (api)"
            assert checked(app) == ["back"]
            await pilot.press("enter")
            await settle(pilot)
            assert orch.calls == [("configure", "feat-a", ["back"])]

    asyncio.run(scenario())


def test_confirm_with_nothing_explicit_is_an_error_without_call() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices = locked_choices()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await open_configure(pilot)
            await pilot.press("space", "down", "space")  # uncheck front, then back
            await pilot.pause()
            assert checked(app) == []
            await pilot.press("enter")
            await pilot.pause()
            assert str(app.screen.query_one("#error", Static).render()) == (
                "check at least one service"
            )
            assert orch.calls == []

    asyncio.run(scenario())


def test_configure_is_blocked_while_an_action_runs() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.gate = threading.Event()
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("u")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert not app.screen.query("#choices")
            assert "busy" in str(app.screen.query_one("#message", Static).render())
            orch.gate.set()
            await settle(pilot)

    asyncio.run(scenario())


def test_checklist_shows_its_state_as_text_markers() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch())
        async with app.run_test() as pilot:
            await open_configure(pilot)
            lines = [line.rstrip() for line in screen_lines(app)]
            assert lines[0] == "Services"  # the title, then a blank line, then the boxes
            assert lines[2:5] == ["[x] front (web)", "[x] back (api)", "[ ] db (api)"]
            assert is_green_bar(rendered_style(app, "front (web)"))  # the highlighted option
            assert not is_green_bar(rendered_style(app, "db (api)"))
            await pilot.press("space")  # uncheck front
            await pilot.pause()
            assert screen_lines(app)[2].rstrip() == "[ ] front (web)"

    asyncio.run(scenario())


def test_the_repos_own_checkout_is_not_listed() -> None:
    main = BranchStatus("main", "up", 1, 1, main_checkout=True)
    feat = BranchStatus("feat-a", "up", 1, 1)

    async def scenario() -> None:
        app = WtApp(FakeOrch(rows=[main, feat]))
        async with app.run_test() as pilot:
            await settle(pilot)
            assert [c[0] for c in cells(app)] == ["feat-a"]

    asyncio.run(scenario())


def test_add_works_on_an_empty_table_in_a_single_project() -> None:
    async def scenario() -> None:
        orch = FakeOrch(rows=[BranchStatus("main", "up", 1, 1, main_checkout=True)])
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert cells(app) == []
            assert message(app) == "no worktrees yet: press a to add"
            await add_worktree(pilot, "new-x", "space", "enter")
            assert orch.calls == [("add", "new-x", ["front"])]

    asyncio.run(scenario())


def test_add_on_an_empty_table_with_one_usable_project() -> None:
    async def scenario() -> None:
        shop = FakeOrch("shop", [])

        def discover() -> list[ProjectEntry]:
            return [ProjectEntry("shop", lambda: shop, "")]

        app = WtApp(projects=discover)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert cells(app) == []
            await add_worktree(pilot, "zz", "space", "enter")
            assert shop.calls == [("add", "zz", ["front"])]

    asyncio.run(scenario())


def test_add_on_an_empty_table_without_a_usable_project_says_so() -> None:
    async def scenario() -> None:
        app = WtApp(projects=lambda: [])
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("a")
            await settle(pilot)
            assert message(app) == "no project to add to"
            assert app.screen_stack[-1] is app.screen_stack[0]

    asyncio.run(scenario())


def test_several_usable_projects_on_an_empty_table_give_no_hint_and_a_clear_message() -> None:
    async def scenario() -> None:
        shop, blog = FakeOrch("shop", []), FakeOrch("blog", [])

        def discover() -> list[ProjectEntry]:
            return [
                ProjectEntry("shop", lambda: shop, ""),
                ProjectEntry("blog", lambda: blog, ""),
            ]

        app = WtApp(projects=discover)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert cells(app) == []
            assert message(app) != "no worktrees yet: press a to add"
            await pilot.press("a")
            await settle(pilot)
            assert message(app) == "several projects: run wt tui inside one to add"
            assert shop.calls == [] and blog.calls == []

    asyncio.run(scenario())


def test_no_empty_hint_without_a_usable_project() -> None:
    async def scenario() -> None:
        app = WtApp(projects=lambda: [])
        async with app.run_test() as pilot:
            await settle(pilot)
            assert message(app) != "no worktrees yet: press a to add"

    asyncio.run(scenario())


def test_the_empty_hint_shows_once_per_empty_spell() -> None:
    async def scenario() -> None:
        orch = FakeOrch(rows=[])
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            assert message(app) == "no worktrees yet: press a to add"
            app.say("something else")
            app.action_refresh()
            await settle(pilot)
            assert message(app) == "something else"  # not repeated while still empty
            orch.rows = [status("feat-a", "up", [8001])]
            app.action_refresh()
            await settle(pilot)
            assert len(cells(app)) == 1
            orch.rows = []
            app.action_refresh()
            await settle(pilot)
            assert message(app) == "no worktrees yet: press a to add"  # re-armed

    asyncio.run(scenario())
