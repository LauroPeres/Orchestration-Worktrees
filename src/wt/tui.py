"""Terminal UI: one table, one row per worktree, a few green/purple accents (SPEC §16)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol, cast

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Input, Label, SelectionList, Static
from textual.widgets.selection_list import Selection

from wt.config import Config
from wt.errors import WtError
from wt.orchestrator import BranchStatus, Choice, RepoRow

REFRESH_SECONDS = 2.0
ACTIONS = "enter: services  u: up  d: down  a: add  r: remove  q: quit"
CONFIRM_REMOVE = (
    "remove {branch}: deletes its containers, volumes, databases, worktrees and the git "
    "branch; unpushed commits are lost. Remove? y/N"
)


class OrchestratorLike(Protocol):
    """The part of `Orchestrator` the TUI uses."""

    cfg: Config

    def ls(self, *, probe: bool = True) -> list[BranchStatus]: ...

    def up(self, branch: str) -> object: ...

    def stop(self, branch: str) -> object: ...

    def add(self, branch: str) -> object: ...

    def remove(self, branch: str, *, force: bool = False) -> object: ...

    def services(self, branch: str) -> list[RepoRow]: ...

    def catalog_choices(self, branch: str) -> list[Choice] | None: ...

    def configure(self, branch: str, uses: list[str]) -> None: ...


@dataclass(frozen=True)
class ProjectEntry:
    """One project in the all-projects view. `orch` is None when its config file is gone."""

    name: str
    orch: Callable[[], OrchestratorLike] | None
    note: str = ""


@dataclass(frozen=True)
class Row:
    """A table line: one worktree, or a project that has no usable data (`status` None)."""

    project: str
    status: BranchStatus | None = None
    note: str = ""
    stale: bool = False

    @property
    def key(self) -> str:
        return f"{self.project}\0{self.status.branch if self.status else ''}"


GREEN = "green"
GREEN_STATES = {"up", "healthy"}
PURPLE = "purple"
SERVICES_ACTIONS = "c: configure  esc: back  q: quit"
CONFIGURE_HINTS = "space: toggle  enter: confirm  esc: cancel"
NEEDS_CATALOG = "configure needs a catalog project (uses: in worktree.yaml)"
NOTHING_CHECKED = "check at least one service"


def actions_text(actions: str = ACTIONS) -> Text:
    """The action line with each key (the part before the colon) in green."""
    text = Text()
    for i, item in enumerate(actions.split("  ")):
        key, _, label = item.partition(":")
        text.append("  " if i else "")
        text.append(key, style=GREEN)
        text.append(":" + label)
    return text


def state_text(state: str) -> Text:
    return Text(state, style=GREEN if state in GREEN_STATES else "")


class ServicesScreen(Screen[None]):
    """Repos of one worktree: NAME, STATUS, PORT, URL, refreshed every 2 s (SPEC §17)."""

    CSS = """
    ServicesScreen { background: $background; }  /* opaque: hide the main table's actions */
    #services { height: auto; max-height: 1fr; border: solid $foreground; background: transparent; }
    #services:focus { border: solid $foreground; background-tint: transparent; }
    DataTable > .datatable--header { text-style: bold; background: transparent; }
    DataTable > .datatable--cursor { text-style: reverse; background: transparent; }
    #actions, #message { height: 1; background: transparent; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "app.pop_screen", "Back", show=False),
        Binding("q", "app.quit", "Quit", show=False),
        Binding("c", "configure", "Configure", show=False),
    ]

    def __init__(self, orch: Callable[[], OrchestratorLike], branch: str) -> None:
        super().__init__()
        self._orch = orch
        self.branch = branch
        self._loading = False
        self._failed_shown = False

    def compose(self) -> ComposeResult:
        yield DataTable(id="services", cursor_type="row")
        yield Static(actions_text(SERVICES_ACTIONS), id="actions")
        yield Static("", id="message")

    def on_mount(self) -> None:
        table = self.query_one("#services", DataTable)
        table.add_columns("NAME", "STATUS", "PORT", "URL")
        table.focus()
        self.set_interval(REFRESH_SECONDS, self.refresh_rows)
        self.refresh_rows()

    def say(self, text: str) -> None:
        self.query_one("#message", Static).update(Text(text))

    def action_configure(self) -> None:
        app = cast("WtApp", self.app)
        if not app.guard():
            return
        try:
            choices = self._orch().catalog_choices(self.branch)
        except Exception as exc:  # keep the app open whatever the orchestrator raises
            self.say(str(exc) if isinstance(exc, WtError) else f"{type(exc).__name__}: {exc}")
            return
        if choices is None:
            self.say(NEEDS_CATALOG)
            return
        branch = self.branch

        def chosen(uses: list[str] | None) -> None:
            if uses is not None and app.guard():
                app.start(f"configure {branch}", lambda: self._orch().configure(branch, uses))

        app.push_screen(ConfigureScreen(choices), chosen)

    def refresh_rows(self) -> None:
        if not self._loading:
            self._loading = True
            self._load()

    @work(thread=True, group="services", exit_on_error=False)
    def _load(self) -> None:
        try:
            rows = self._orch().services(self.branch)
        except WtError as exc:
            self.app.call_from_thread(self._failed, str(exc))
        except Exception as exc:  # keep the app open whatever the orchestrator raises
            self.app.call_from_thread(self._failed, f"{type(exc).__name__}: {exc}")
        else:
            self.app.call_from_thread(self._apply, rows)

    def _failed(self, message: str) -> None:
        self._loading = False
        if self.is_attached:
            self._failed_shown = True
            self.say(message)

    def _apply(self, rows: list[RepoRow]) -> None:
        try:
            table = self.query_one("#services", DataTable)
            cursor = table.cursor_row
            table.clear()
            for r in rows:
                port = Text(str(r.port), style=PURPLE) if r.port is not None else Text("-")
                table.add_row(Text(r.name), state_text(r.status), port, Text(r.url or "-"))
            if rows:
                table.move_cursor(row=min(cursor, len(rows) - 1))
            if self._failed_shown:
                self._failed_shown = False
                self.say("")
        except NoMatches:
            pass  # the screen was popped before this late refresh arrived
        finally:
            self._loading = False


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no question; dismisses with True on `y`, False on `n`/`Enter`/`Escape`."""

    CSS = """
    ConfirmScreen { align: center bottom; background: transparent; }
    #dialog { width: 100%; height: auto; border: solid; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,enter,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.question))

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class InputScreen(ModalScreen[str | None]):
    """Single-line prompt; dismisses with the text, or None on Escape."""

    CSS = """
    InputScreen { align: center bottom; background: transparent; }
    #dialog { width: 100%; height: auto; border: solid; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.prompt))
            yield Input(id="answer")

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfigureScreen(ModalScreen[list[str] | None]):
    """Check-box list of the catalog services; dismisses with the checked names, None on Escape."""

    CSS = """
    ConfigureScreen { align: center middle; background: transparent; }
    #dialog { width: auto; min-width: 40; max-width: 100%; height: auto; max-height: 100%;
              border: solid $foreground; padding: 0 1; background: $surface; }
    #choices { height: auto; max-height: 20; border: none; padding: 0; background: transparent; }
    #choices > .selection-list--button { color: $panel; background: $panel; }
    #choices > .selection-list--button-highlighted { color: $panel; background: $panel; }
    #choices > .selection-list--button-selected {
        color: green; background: $panel; text-style: bold;
    }
    #choices > .selection-list--button-selected-highlighted {
        color: green; background: $panel; text-style: bold;
    }
    #choices:focus { border: none; background-tint: transparent; }
    #hints, #error { height: 1; }
    #error { color: $error; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "confirm", "Confirm", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, choices: list[Choice]) -> None:
        super().__init__()
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text("Services"))
            yield SelectionList[str](
                *[
                    Selection(Text(f"{c.service} ({c.repo})"), c.service, c.selected)
                    for c in self.choices
                ],
                id="choices",
            )
            yield Static(actions_text(CONFIGURE_HINTS), id="hints")
            yield Static("", id="error")

    def on_mount(self) -> None:
        self.query_one("#choices", SelectionList).focus()

    def action_confirm(self) -> None:
        uses = list(self.query_one("#choices", SelectionList).selected)
        if not uses:
            self.query_one("#error", Static).update(Text(NOTHING_CHECKED))
            return
        self.dismiss(uses)

    def action_cancel(self) -> None:
        self.dismiss(None)


class WtApp(App[None]):
    """Table of worktrees with up/down/add/remove actions (SPEC §16)."""

    TITLE = "wt"
    CSS = """
    Screen { background: transparent; }
    #table { height: auto; max-height: 1fr; border: solid $foreground; background: transparent; }
    #table:focus { border: solid $foreground; background-tint: transparent; }
    DataTable > .datatable--header { text-style: bold; background: transparent; }
    DataTable > .datatable--cursor { text-style: reverse; background: transparent; }
    #actions, #message { height: 1; background: transparent; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("u", "up_selected", "Up", show=False),
        Binding("d", "down_selected", "Down", show=False),
        Binding("a", "add_new", "Add", show=False),
        Binding("r", "remove_selected", "Remove", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        orch: OrchestratorLike | None = None,
        *,
        projects: Callable[[], list[ProjectEntry]] | None = None,
    ) -> None:
        """One project (`orch`), or all projects from `projects` (called on every refresh)."""
        super().__init__()
        if (orch is None) == (projects is None):
            raise ValueError("pass either orch or projects")
        self.orch = orch
        self._discover = projects
        self._entries: dict[str, ProjectEntry] = {}
        self.rows: list[Row] = []
        self._loading = False
        self._busy = False

    def compose(self) -> ComposeResult:
        yield DataTable(id="table", cursor_type="row")
        yield Static(actions_text(), id="actions")
        yield Static("", id="message")

    def on_mount(self) -> None:
        self._main.query_one("#table", DataTable).add_columns("NAME", "STATUS")
        self._main.query_one("#table", DataTable).focus()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)
        self.action_refresh()

    # -- helpers --------------------------------------------------------------------

    @property
    def _main(self) -> Screen[object]:
        """The main screen, also while the services screen or a modal is on top."""
        return self.screen_stack[0]

    @property
    def selected_row(self) -> Row | None:
        table = self._main.query_one("#table", DataTable)
        if table.row_count == 0 or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    def say(self, text: str) -> None:
        """Show `text` (plain, no markup) on the message line of the services screen, else main."""
        screens = [s for s in self.screen_stack if isinstance(s, ServicesScreen)]
        target = screens[-1] if screens else self._main
        target.query_one("#message", Static).update(Text(text))

    def log_threadsafe(self, message: str) -> None:
        """For `Orchestrator.log` (markup): callable from a worker thread."""
        self.call_from_thread(self.say, Text.from_markup(message).plain)

    def out_threadsafe(self, line: str) -> None:
        """For `Orchestrator.out` (plain lines): callable from a worker thread."""
        self.call_from_thread(self.say, line)

    def _orch_for(self, project: str) -> OrchestratorLike:
        """The project's orchestrator (built lazily in the all-projects view); may raise."""
        if self.orch is not None:
            return self.orch
        entry = self._entries.get(project)
        if entry is None or entry.orch is None:
            raise WtError(f"{project}: {entry.note if entry else 'unknown project'}")
        return entry.orch()

    def guard(self) -> bool:
        if self._busy:
            self.say("busy: wait for the running action to finish")
            return False
        return True

    def _selected_worktree(self) -> Row | None:
        """The selected row when an action on its branch is possible; else say why."""
        row = self.selected_row
        if row is None:
            self.say("nothing selected")
        elif row.status is None:
            self.say(f"{row.project}: {row.note}")
        else:
            return row
        return None

    @on(DataTable.RowSelected, "#table")
    def _open_services(self) -> None:
        row = self._selected_worktree()
        if row is not None and row.status is not None:
            project = row.project
            self.push_screen(ServicesScreen(lambda: self._orch_for(project), row.status.branch))

    # -- actions --------------------------------------------------------------------

    def action_up_selected(self) -> None:
        row = self._selected_worktree()
        if row is not None and row.status is not None and self.guard():
            branch = row.status.branch
            self.start(f"up {branch}", lambda: self._orch_for(row.project).up(branch))

    def action_down_selected(self) -> None:
        row = self._selected_worktree()
        if row is not None and row.status is not None and self.guard():
            branch = row.status.branch
            self.start(f"down {branch}", lambda: self._orch_for(row.project).stop(branch))

    def action_add_new(self) -> None:
        row = self.selected_row
        if row is None:
            self.say("nothing selected")
            return
        if row.stale:
            self.say(f"{row.project}: {row.note}")
            return
        if not self.guard():
            return
        project = row.project

        def got(name: str | None) -> None:
            if name and self.guard():
                self.start(f"add {name}", lambda: self._orch_for(project).add(name))

        self.push_screen(InputScreen("New worktree name"), got)

    def action_remove_selected(self) -> None:
        row = self._selected_worktree()
        if row is None or row.status is None or not self.guard():
            return
        branch = row.status.branch

        def confirmed(yes: bool | None) -> None:
            if yes and self.guard():
                self.start(
                    f"remove {branch}",
                    lambda: self._orch_for(row.project).remove(branch, force=True),
                )

        self.push_screen(ConfirmScreen(CONFIRM_REMOVE.format(branch=branch)), confirmed)

    def start(self, label: str, call: Callable[[], object]) -> None:
        self._busy = True
        self.say(label)
        self._run_action(label, call)

    @work(thread=True, group="action", exit_on_error=False)
    def _run_action(self, label: str, call: Callable[[], object]) -> None:
        try:
            call()
        except WtError as exc:
            self.call_from_thread(self._action_done, str(exc))
        except Exception as exc:  # keep the app open whatever the orchestrator raises
            self.call_from_thread(self._action_done, f"{type(exc).__name__}: {exc}")
        else:
            self.call_from_thread(self._action_done, f"{label}: done")

    def _action_done(self, message: str) -> None:
        self._busy = False
        self.say(message)
        self.action_refresh()
        for screen in self.screen_stack:
            if isinstance(screen, ServicesScreen):
                screen.refresh_rows()

    # -- refresh --------------------------------------------------------------------

    def action_refresh(self) -> None:
        if not self._loading:
            self._loading = True
            self._load_rows()

    def _collect(self) -> list[Row]:
        if self.orch is not None:
            rows = sorted(self.orch.ls(probe=True), key=lambda r: r.branch)
            return [Row(self.orch.cfg.project, r) for r in rows]
        assert self._discover is not None
        entries = sorted(self._discover(), key=lambda e: e.name)
        self._entries = {e.name: e for e in entries}
        result: list[Row] = []
        for entry in entries:
            if entry.orch is None:
                result.append(Row(entry.name, note=entry.note, stale=True))
                continue
            try:
                statuses = sorted(entry.orch().ls(probe=True), key=lambda r: r.branch)
            except WtError as exc:
                result.append(Row(entry.name, note=str(exc).splitlines()[0]))
                continue
            result.extend(Row(entry.name, s) for s in statuses)
        return result

    @work(thread=True, group="refresh")
    def _load_rows(self) -> None:
        try:
            rows = self._collect()
        except WtError as exc:
            self.call_from_thread(self._load_failed, str(exc))
        else:
            self.call_from_thread(self._apply_rows, rows)

    def _load_failed(self, message: str) -> None:
        self._loading = False
        if not self._busy:
            self.say(message)

    def _apply_rows(self, rows: list[Row]) -> None:
        try:
            table = self._main.query_one("#table", DataTable)
            previous = self.selected_row
            self.rows = rows
            table.clear()
            multi = self.orch is None
            if multi and len(table.columns) == 2:  # all projects: the project gets its own column
                table.clear(columns=True)
                table.add_columns("PROJECT", "NAME", "STATUS")
            for r in rows:
                cells: tuple[Text, ...]
                if r.status is not None:
                    cells = (Text(r.status.branch), state_text(r.status.status))
                else:
                    cells = (Text("-"), Text(r.note))
                table.add_row(*((Text(r.project), *cells) if multi else cells), key=r.key)
            keys = [r.key for r in rows]
            if previous is not None and previous.key in keys:
                table.move_cursor(row=keys.index(previous.key))
        except NoMatches:
            pass  # the app is closing: its widgets are gone before this late refresh arrived
        finally:
            self._loading = False
