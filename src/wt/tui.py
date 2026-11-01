"""Terminal UI: a bordered table of worktrees and one page per action (SPEC §16, §21)."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol, Self, TypeVar, cast

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import Screen
from textual.strip import Strip
from textual.widgets import DataTable, Input, Label, SelectionList, Static
from textual.widgets.selection_list import Selection

from wt.config import Config
from wt.errors import WtError
from wt.orchestrator import BranchStatus, Choice, RepoRow

REFRESH_SECONDS = 2.0
KEY_STYLE = Style(color="green")  # the one color in the UI, with the table's column names
KEY_GAP = "  "  # separates the `key: words` items of a hint line
ACTIONS = "enter: services  u: up  d: down  a: add  r: remove  q: quit"
CONFIRM_REMOVE = (
    "remove {branch}: removes what wt created for it (containers, volumes, databases, "
    "worktrees) and deletes the git branch in every repo of the project where it exists; "
    "unpushed commits are lost. Remove? y/N"
)


class OrchestratorLike(Protocol):
    """The part of `Orchestrator` the TUI uses."""

    cfg: Config

    def ls(self, *, probe: bool = True) -> list[BranchStatus]: ...

    def up(self, branch: str) -> object: ...

    def stop(self, branch: str) -> object: ...

    def add(self, branch: str, uses: list[str]) -> object: ...

    def remove(self, branch: str, *, force: bool = False) -> object: ...

    def services(self, branch: str) -> list[RepoRow]: ...

    def catalog_choices(self, branch: str | None = None) -> list[Choice] | None: ...

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


SERVICES_ACTIONS = "c: configure  esc: back  q: quit"
CONFIGURE_HINTS = "space: toggle  enter: confirm  esc: cancel"
NEEDS_CATALOG = "no catalog for this project. Run wt init or wt migrate"
NOTHING_CHECKED = "check at least one service"
NO_CATALOG = "no catalog at {path}. Run wt init or wt migrate"
ENTER_NAME = "Enter name"
ADD_TITLE = "Add a worktree"
REMOVE_TITLE = "Remove a worktree"
INPUT_HINTS = "enter: confirm  esc: cancel"
CONFIRM_HINTS = "y: yes  n: no  esc: cancel"
# The table's keys; on a page that asks something they would stack another page on top.
TABLE_ACTIONS = frozenset({"up_selected", "down_selected", "add_new", "remove_selected"})
MARKER_CHECKED = "[x] "
MARKER_UNCHECKED = "[ ] "
# Textual draws a checkbox as 4 segments (left, inner, right, space) before the option text.
BUTTON_SEGMENTS = 4


def keys_text(line: str) -> Text:
    """A hint line (`key: words  key: words`) with the keys in green and the words plain."""
    text = Text()
    for index, item in enumerate(line.split(KEY_GAP)):
        key, colon, words = item.partition(":")
        if index:
            text.append(KEY_GAP)
        text.append(key, KEY_STYLE)
        text.append(colon + words)
    return text


def choice_label(choice: Choice, holders: tuple[str, ...] = ()) -> Text:
    """`service (repo)`, plus `(required by x, y)` while checked consumers `holders` need it."""
    label = f"{choice.service} ({choice.repo})"
    if holders:
        label += f" (required by {', '.join(holders)})"
    return Text(label)


def error_text(exc: Exception) -> str:
    """What to show for an error that must not close the app."""
    return str(exc) if isinstance(exc, WtError) else f"{type(exc).__name__}: {exc}"


def required_note(service: str, holders: tuple[str, ...]) -> str:
    return f"{service} is required by {', '.join(holders)}"


class ServicesScreen(Screen[None]):
    """Repos of one worktree: NAME, STATUS, PORT, URL, refreshed every 2 s (SPEC §17)."""

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
        yield Static(keys_text(SERVICES_ACTIONS), id="actions")
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
            self.say(error_text(exc))
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
                port = Text(str(r.port) if r.port is not None else "-")
                table.add_row(Text(r.name), Text(r.status), port, Text(r.url or "-"))
            if rows:
                table.move_cursor(row=min(cursor, len(rows) - 1))
            if self._failed_shown:
                self._failed_shown = False
                self.say("")
        except NoMatches:
            pass  # the screen was popped before this late refresh arrived
        finally:
            self._loading = False


PageResult = TypeVar("PageResult")


class PageScreen(Screen[PageResult]):
    """A full-screen step of an action: a title, the step and its hints; nothing else is shown."""

    def __init__(self, heading: str) -> None:
        super().__init__()
        self.heading = heading

    def compose(self) -> ComposeResult:
        yield Label(Text(self.heading), id="title")
        yield from self.compose_page()

    def compose_page(self) -> ComposeResult:
        raise NotImplementedError


class ConfirmScreen(PageScreen[bool]):
    """Yes/no question; dismisses with True on `y`, False on `n`/`Enter`/`Escape`."""

    CSS = """
    #dialog { width: 100%; height: auto; }
    #dialog Label { width: 100%; }  /* a Label is as wide as its text by default: wrap instead */
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,enter,escape", "answer(False)", "No"),
    ]

    def __init__(self, heading: str, question: str) -> None:
        super().__init__(heading)
        self.question = question

    def compose_page(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.question))
        yield Static(keys_text(CONFIRM_HINTS), id="hints")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class InputScreen(PageScreen[str | None]):
    """Single-line prompt; dismisses with the text, or None on Escape."""

    CSS = """
    #dialog { width: 100%; height: auto; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, heading: str, prompt: str) -> None:
        super().__init__(heading)
        self.prompt = prompt

    def compose_page(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.prompt))
            yield Input(id="answer", compact=True)
        yield Static(keys_text(INPUT_HINTS), id="hints")

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceList(SelectionList[str]):
    """Selection list where a service stays checked while a checked consumer needs it (SPEC §20).

    A toggle attempt on such a service is reported; unchecking its consumers lifts the lock.
    """

    class Refused(Message):
        """The user tried to toggle a service that a checked consumer still requires."""

        def __init__(self, service: str, holders: tuple[str, ...]) -> None:
            super().__init__()
            self.service = service
            self.holders = holders

    def __init__(self, choices: list[Choice]) -> None:
        super().__init__(
            *[Selection(choice_label(c), c.service, c.selected) for c in choices],
            id="choices",
            compact=True,
        )
        self._choices = choices

    def holders(self, choice: Choice) -> tuple[str, ...]:
        """The consumers of `choice` that are checked right now: they lock it."""
        checked = self.selected
        return tuple(consumer for consumer in choice.required_by if consumer in checked)

    def explicit(self) -> list[str]:
        """The checked services no checked consumer pulls in, in catalog order (SPEC §20)."""
        checked = self.selected
        return [c.service for c in self._choices if c.service in checked and not self.holders(c)]

    def check_dependencies(self) -> None:
        """Check what a checked consumer needs: it then shows locked, as in an existing stack.

        Each `select` is synchronous, so one pass over the catalog order reaches the whole chain
        only when consumers come first; looping until nothing changes makes order irrelevant.
        """
        changed = True
        while changed:
            changed = False
            for choice in self._choices:
                if choice.service not in self.selected and self.holders(choice):
                    self.select(choice.service)
                    changed = True

    def relabel(self) -> None:
        """Show `(required by ...)` only for the consumers that are still checked."""
        for index, choice in enumerate(self._choices):
            self.replace_option_prompt_at_index(index, choice_label(choice, self.holders(choice)))

    def render_line(self, y: int) -> Strip:
        """Draw `[x]`/`[ ]` in place of Textual's colored checkbox: the state shows as text."""
        strip = super().render_line(y)
        index = self.scroll_offset.y + y
        segments = list(strip)
        if index >= self.option_count or len(segments) < BUTTON_SEGMENTS:
            return strip
        checked = self.get_option_at_index(index).value in self.selected
        marker = MARKER_CHECKED if checked else MARKER_UNCHECKED
        # Keep the option meta so a click on the marker still toggles the line.
        style = (segments[BUTTON_SEGMENTS - 1].style or Style()) + Style(meta={"option": index})
        return Strip([Segment(marker, style), *segments[BUTTON_SEGMENTS:]])

    def toggle(self, selection: Selection[str] | str) -> Self:
        value = selection.value if isinstance(selection, Selection) else selection
        for choice in self._choices:
            if choice.service == value and (holders := self.holders(choice)):
                self.post_message(self.Refused(value, holders))
                return self
        return super().toggle(selection)


class ConfigureScreen(PageScreen[list[str] | None]):
    """Check-box list of the catalog services; dismisses with the checked names, None on Escape."""

    CSS = """
    #choices { height: auto; max-height: 20; }
    #error { height: 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "confirm", "Confirm", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, choices: list[Choice], heading: str = "Services") -> None:
        super().__init__(heading)
        self.choices = choices

    def compose_page(self) -> ComposeResult:
        yield ChoiceList(self.choices)
        yield Static(keys_text(CONFIGURE_HINTS), id="hints")
        yield Static("", id="error")

    def on_mount(self) -> None:
        options = self._options
        options.relabel()  # an initial selection can already have unchecked consumers
        options.focus()

    @property
    def _options(self) -> ChoiceList:
        return self.query_one("#choices", ChoiceList)

    def _say(self, text: str) -> None:
        self.query_one("#error", Static).update(Text(text))

    @on(ChoiceList.Refused)
    def _refused(self, event: ChoiceList.Refused) -> None:
        self._say(required_note(event.service, event.holders))

    @on(SelectionList.SelectedChanged)
    def _selection_changed(self) -> None:
        self._options.check_dependencies()
        self._options.relabel()
        self._say("")  # a note about the previous selection no longer applies

    def action_confirm(self) -> None:
        uses = self._options.explicit()
        if not uses:
            self._say(NOTHING_CHECKED)
            return
        self.dismiss(uses)

    def action_cancel(self) -> None:
        self.dismiss(None)


class WtApp(App[None]):
    """Table of worktrees with up/down/add/remove actions (SPEC §16)."""

    TITLE = "wt"
    # The terminal's own colors, plus green for the column names and the keys. The selected row
    # shows as reverse video, never as a color.
    CSS = """
    DataTable { height: auto; max-height: 1fr; border: solid ansi_default; }
    #actions { height: auto; }  /* wraps on a narrow terminal */
    #message, #hints { height: 1; }
    #title { height: 1; margin-bottom: 1; text-style: bold; }
    DataTable > .datatable--header {
        text-style: none; background: ansi_default; color: ansi_green;
    }
    DataTable > .datatable--hover {
        text-style: none; background: ansi_default; color: ansi_default;
    }
    DataTable > .datatable--cursor,
    DataTable:focus > .datatable--cursor,
    OptionList > .option-list--option-highlighted,
    OptionList:focus > .option-list--option-highlighted,
    Input > .input--cursor,
    Input > .input--selection {
        text-style: reverse; background: ansi_default; color: ansi_default;
    }
    OptionList > .option-list--option-hover { background: ansi_default; }
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
        missing_catalog: Path | None = None,
    ) -> None:
        """One project (`orch`), or all projects from `projects` (called on every refresh).

        `missing_catalog` is the catalog file that was looked for and is not there.
        """
        super().__init__(ansi_color=True)
        self.set_reactive(App.theme, "ansi-dark")  # background and text are the terminal's own
        if (orch is None) == (projects is None):
            raise ValueError("pass either orch or projects")
        self.orch = orch
        self._discover = projects
        self._missing_catalog = missing_catalog
        self._entries: dict[str, ProjectEntry] = {}
        self.rows: list[Row] = []
        self._loading = False
        self._busy = False
        self._empty_hinted = False

    def compose(self) -> ComposeResult:
        yield DataTable(id="table", cursor_type="row")
        yield Static(keys_text(ACTIONS), id="actions")
        yield Static("", id="message")

    def on_mount(self) -> None:
        self._main.query_one("#table", DataTable).add_columns("NAME", "STATUS")
        self._main.query_one("#table", DataTable).focus()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)
        self.action_refresh()

    # -- helpers --------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """The table's keys do nothing on a page that asks something (they would stack pages)."""
        return not (action in TABLE_ACTIONS and isinstance(self.screen, PageScreen))

    @property
    def _main(self) -> Screen[object]:
        """The main screen, also while the services screen or a page is on top."""
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

    def _usable_projects(self) -> list[str]:
        return [name for name, e in self._entries.items() if e.orch is not None]

    def _can_add(self) -> bool:
        """True when `a` has a project to add to even if no row is selected."""
        return self.orch is not None or len(self._usable_projects()) == 1

    def _project_for_add(self) -> str | None:
        """The project a new worktree goes to; else say why there is none."""
        row = self.selected_row
        if row is not None:
            if row.stale:
                self.say(f"{row.project}: {row.note}")
                return None
            return row.project
        if self.orch is not None:
            return self.orch.cfg.project
        usable = self._usable_projects()
        if len(usable) == 1:
            return usable[0]
        if usable:
            self.say("several projects: run wt tui inside one to add")
        elif self._missing_catalog is not None:
            self.say(NO_CATALOG.format(path=self._missing_catalog))
        else:
            self.say("no project to add to")
        return None

    def action_add_new(self) -> None:
        project = self._project_for_add()
        if project is None or not self.guard():
            return

        def got(name: str | None) -> None:
            if name:
                self._choose_services(project, name)

        self.push_screen(InputScreen(ADD_TITLE, ENTER_NAME), got)

    def _choose_services(self, project: str, name: str) -> None:
        """Second step of `a`: pick the catalog services of the new worktree, then add it."""
        try:
            choices = self._orch_for(project).catalog_choices(None)
        except Exception as exc:  # keep the app open whatever the orchestrator raises
            self.say(error_text(exc))
            return
        if not choices:
            self.say(f"{project}: no catalog services to choose from")
            return

        def chosen(uses: list[str] | None) -> None:
            if uses is not None and self.guard():
                self.start(f"add {name}", lambda: self._orch_for(project).add(name, uses))

        self.push_screen(ConfigureScreen(choices, f"Services for {name}"), chosen)

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

        question = CONFIRM_REMOVE.format(branch=branch)
        self.push_screen(ConfirmScreen(REMOVE_TITLE, question), confirmed)

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
            self.call_from_thread(self._action_done, error_text(exc))
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
            return [Row(self.orch.cfg.project, r) for r in rows if not r.main_checkout]
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
            # Only the worktrees wt created; the repos' own checkout (main) is not listed.
            result.extend(Row(entry.name, s) for s in statuses if not s.main_checkout)
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
            for r in rows:
                if r.status is not None:
                    cells = (Text(r.status.branch), Text(r.status.status))
                else:
                    cells = (Text(r.project), Text(r.note))
                table.add_row(*cells, key=r.key)
            if rows:
                self._empty_hinted = False
            elif not self._empty_hinted and not self._busy and self._can_add():
                # once per empty spell: keep later messages visible
                self._empty_hinted = True
                self.say("no worktrees yet: press a to add")
            keys = [r.key for r in rows]
            if previous is not None and previous.key in keys:
                table.move_cursor(row=keys.index(previous.key))
        except NoMatches:
            pass  # the app is closing: its widgets are gone before this late refresh arrived
        finally:
            self._loading = False
