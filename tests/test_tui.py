import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from textual.widgets import DataTable, Label, SelectionList, Static

from wt.errors import WtError
from wt.orchestrator import BranchStatus, Choice, RepoRow
from wt.tui import CONFIRM_REMOVE, ProjectEntry, WtApp


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
        self.service_rows: list[RepoRow] = [
            RepoRow("front", "healthy", 10001, "https://front.demo.localhost"),
            RepoRow("back", "starting", 10002, None),
            RepoRow("db", "stopped", None, None),
        ]
        self.service_calls: list[str] = []
        self.services_error: Exception | None = None
        self.choices: list[Choice] | None = [
            Choice("front", "web", True),
            Choice("back", "api", True),
            Choice("db", "api", False),
        ]

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

    def services(self, branch: str) -> list[RepoRow]:
        self.service_calls.append(branch)
        if self.services_error is not None:
            raise self.services_error
        return self.service_rows

    def catalog_choices(self, branch: str) -> list[Choice] | None:
        return self.choices

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


def test_rows_and_columns() -> None:
    async def scenario() -> None:
        app = WtApp(FakeOrch())
        async with app.run_test() as pilot:
            await settle(pilot)
            table = app.query_one("#table", DataTable)
            assert [str(c.label) for c in table.columns.values()] == ["NAME", "STATUS"]
            assert cells(app) == [["feat-a", "up"], ["main", "stopped"]]
            actions: Any = app.query_one("#actions", Static).render()
            assert actions.plain == "enter: services  u: up  d: down  a: add  r: remove  q: quit"
            green = [actions.plain[s.start : s.end] for s in actions.spans]
            assert green == ["enter", "u", "d", "a", "r", "q"]
            table = app.query_one("#table", DataTable)
            up, stopped = table.get_row_at(0)[1], table.get_row_at(1)[1]
            assert up.style == "green" and not stopped.style

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


def screen_lines(app: WtApp) -> list[str]:
    """What the terminal shows right now, one string per screen row (after clipping)."""
    return [strip.text for strip in app.screen._compositor.render_strips()]


def dialog_text(app: WtApp) -> tuple[str, int]:
    """The words visible inside the `#dialog` box, and the box height."""
    region = app.screen.query_one("#dialog").region
    rows = screen_lines(app)[region.y : region.y + region.height]
    inner = [row.strip("│ ") for row in rows if row.startswith("│")]
    return " ".join(" ".join(inner).split()), region.height


def test_remove_confirmation_wraps_so_the_whole_sentence_is_visible() -> None:
    async def scenario(size: tuple[int, int]) -> None:
        app = WtApp(FakeOrch())
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            await pilot.press("r")
            await pilot.pause()
            text, height = dialog_text(app)
            assert text == REMOVE_QUESTION
            assert 3 < height <= size[1]
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
            assert cells(app) == [["[red]x[/]", "up"]]
            table = app.query_one("#table", DataTable)
            for cell in table.get_row_at(0):
                assert not cell.spans

    asyncio.run(scenario())


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
            healthy, port = table.get_row_at(0)[1], table.get_row_at(0)[2]
            assert healthy.style == "green" and port.style == "purple"
            assert not table.get_row_at(1)[1].style
            actions: Any = app.screen.query_one("#actions", Static).render()
            assert actions.plain == "c: configure  esc: back  q: quit"
            await pilot.press("escape")
            await settle(pilot)
            assert cells(app) == [["feat-a", "up"], ["main", "stopped"]]

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
            assert [hints.plain[s.start : s.end] for s in hints.spans] == ["space", "enter", "esc"]
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


def test_configure_on_services_project_shows_note() -> None:
    async def scenario() -> None:
        orch = FakeOrch()
        orch.choices = None
        app = WtApp(orch)
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            await pilot.press("c")
            await pilot.pause()
            assert str(app.screen.query_one("#message", Static).render()) == (
                "configure needs a catalog project (uses: in worktree.yaml)"
            )
            assert not app.screen.query("#choices")

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
            await pilot.press("a", *"new-x", "enter")
            await settle(pilot)
            assert orch.calls == [("add", "new-x")]

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
            await pilot.press("a", *"zz", "enter")
            await settle(pilot)
            assert shop.calls == [("add", "zz")]

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
