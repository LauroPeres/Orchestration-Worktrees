"""Terminal UI: branches, services of the selected branch and a log pane (SPEC §15)."""

from collections.abc import Callable
from typing import ClassVar, Protocol

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Label, ListItem, ListView, RichLog

from wt.config import Config
from wt.errors import WtError
from wt.orchestrator import BranchStatus

REFRESH_SECONDS = 2.0
HEALTH = {"ok": "[green]ok[/]", "fail": "[red]fail[/]", "skipped": "[dim]skipped[/]"}


class OrchestratorLike(Protocol):
    """The part of `Orchestrator` the TUI reads."""

    cfg: Config

    def ls(self, *, probe: bool = True) -> list[BranchStatus]: ...

    def up(self, branch: str) -> object: ...

    def down(self, branch: str, *, force: bool = False) -> bool: ...


def branch_label(row: BranchStatus) -> str:
    return f"{escape(row.branch)}  [dim]{escape(row.status)}[/]"


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no question; dismisses with True on `y`/Enter, False on `n`/Escape."""

    CSS = """
    ConfirmScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $primary; padding: 1 2; background: $surface; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y,enter", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.question))
            yield Label("[dim]y/Enter = yes, n/Esc = no[/]")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class InputScreen(ModalScreen[str | None]):
    """Single-line prompt; dismisses with the text, or None on Escape."""

    CSS = """
    InputScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $primary; padding: 1 2; background: $surface; }
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


class WtApp(App[None]):
    """`wt ls` view plus up/down actions (SPEC §15). Logs are added as a later slice."""

    TITLE = "wt"
    CSS = """
    #main { height: 1fr; }
    #branches { width: 32; border: round $primary; }
    #services { width: 1fr; border: round $primary; }
    #log { height: 10; border: round $primary; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("u", "up_selected", "Up"),
        Binding("n", "up_new", "New"),
        Binding("d", "down_selected", "Down"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, orch: OrchestratorLike) -> None:
        super().__init__()
        self.orch = orch
        self.rows: list[BranchStatus] = []
        self._loading = False
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield ListView(id="branches")
            yield DataTable(id="services", cursor_type="row")
        yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.orch.cfg.project
        branches = self.query_one("#branches", ListView)
        branches.border_title = "Branches"
        branches.focus()
        table = self.query_one("#services", DataTable)
        table.border_title = "Services"
        table.add_columns("SERVICE", "KIND", "URL", "HEALTH")
        log = self.query_one("#log", RichLog)
        log.border_title = "Log"
        log.write("[dim]No log yet.[/]")
        self.set_interval(REFRESH_SECONDS, self.action_refresh)
        self.action_refresh()

    # -- helpers for later actions ------------------------------------------------

    @property
    def selected_branch(self) -> str | None:
        view = self.query_one("#branches", ListView)
        index = view.index
        if index is None or index >= len(self.rows):
            return None
        return self.rows[index].branch

    def write_log(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def log_threadsafe(self, message: str) -> None:
        """For `Orchestrator.log` (markup): callable from a worker thread."""
        self.call_from_thread(self.write_log, message)

    def out_threadsafe(self, line: str) -> None:
        """For `Orchestrator.out` (plain compose/Traefik lines): callable from a worker thread."""
        self.call_from_thread(self._write_plain, line)

    def _write_plain(self, line: str) -> None:
        self.query_one("#log", RichLog).write(Text(line))

    # -- actions --------------------------------------------------------------------

    def _guard(self) -> bool:
        """True when an action may start; otherwise tell the user and return False."""
        if self._busy:
            self.write_log("[yellow]busy: wait for the running action to finish[/]")
            return False
        return True

    def action_up_selected(self) -> None:
        branch = self.selected_branch
        if branch is not None and self._guard():
            self._start(f"up {branch}", lambda: self.orch.up(branch))

    def action_up_new(self) -> None:
        if not self._guard():
            return

        def got(name: str | None) -> None:
            if name and self._guard():
                self._start(f"up {name}", lambda: self.orch.up(name))

        self.push_screen(InputScreen("New branch name"), got)

    def action_down_selected(self) -> None:
        branch = self.selected_branch
        if branch is None or not self._guard():
            return

        def confirmed(yes: bool | None) -> None:
            if yes and self._guard():
                self._start(
                    f"down {branch}",
                    lambda: self.orch.down(branch),
                    on_error=lambda: self._offer_force(branch),
                )

        self.push_screen(ConfirmScreen(f"Bring down {branch}?"), confirmed)

    def _offer_force(self, branch: str) -> None:
        def confirmed(yes: bool | None) -> None:
            if yes and self._guard():
                self._start(f"down --force {branch}", lambda: self.orch.down(branch, force=True))

        self.push_screen(ConfirmScreen(f"down {branch} failed. Retry with --force?"), confirmed)

    def _start(
        self, label: str, call: Callable[[], object], on_error: Callable[[], None] | None = None
    ) -> None:
        self._busy = True
        self.write_log(f"[bold]{escape(label)}[/]")
        self._run_action(label, call, on_error)

    @work(thread=True, group="action", exit_on_error=False)
    def _run_action(
        self, label: str, call: Callable[[], object], on_error: Callable[[], None] | None
    ) -> None:
        try:
            call()
        except WtError as exc:
            self.call_from_thread(self._action_done, f"[red]{escape(str(exc))}[/]", on_error)
        except Exception as exc:  # keep the app open whatever the orchestrator raises
            message = f"[red]{escape(type(exc).__name__)}: {escape(str(exc))}[/]"
            self.call_from_thread(self._action_done, message, on_error)
        else:
            self.call_from_thread(self._action_done, f"[green]{escape(label)}: done[/]", None)

    def _action_done(self, message: str, on_error: Callable[[], None] | None) -> None:
        self._busy = False
        self.write_log(message)
        self.action_refresh()
        if on_error is not None:
            on_error()

    # -- refresh --------------------------------------------------------------------

    def action_refresh(self) -> None:
        if not self._loading:
            self._loading = True
            self._load_rows()

    @work(thread=True, group="refresh")
    def _load_rows(self) -> None:
        try:
            rows = self.orch.ls(probe=True)
        except WtError as exc:
            self.call_from_thread(self._load_failed, str(exc))
        else:
            self.call_from_thread(self._apply_rows, rows)  # type: ignore[arg-type]

    def _load_failed(self, message: str) -> None:
        self._loading = False
        self.write_log(f"[red]{escape(message)}[/]")

    async def _apply_rows(self, rows: list[BranchStatus]) -> None:
        try:
            rows = sorted(rows, key=lambda r: r.branch)
            view = self.query_one("#branches", ListView)
            previous = self.selected_branch
            items = list(view.query(ListItem))
            same = [r.branch for r in rows] == [r.branch for r in self.rows] and len(items) == len(
                rows
            )
            self.rows = rows
            if same:
                for item, row in zip(items, rows, strict=True):
                    item.query_one(Label).update(branch_label(row))
            else:
                await view.clear()
                await view.extend(ListItem(Label(branch_label(r))) for r in rows)
                names = [r.branch for r in rows]
                view.index = names.index(previous) if previous in names else (0 if rows else None)
            self._show_services()
        except NoMatches:
            pass  # the app is closing: its widgets are gone before this late refresh arrived
        finally:
            # Cleared only now: a refresh that starts while the list is being rebuilt would see
            # the new rows but not the new items yet.
            self._loading = False

    @on(ListView.Highlighted, "#branches")
    def _highlighted(self) -> None:
        self._show_services()

    def _show_services(self) -> None:
        table = self.query_one("#services", DataTable)
        table.clear()
        name = self.selected_branch
        row = next((r for r in self.rows if r.branch == name), None)
        if row is None:
            return
        if not row.docker_ok:
            self.sub_title = f"{self.orch.cfg.project} (docker unavailable)"
        else:
            self.sub_title = self.orch.cfg.project
        for svc in row.services:
            table.add_row(
                svc.service,
                svc.kind,
                svc.url or "-",
                f"{HEALTH[svc.outcome]} [dim]{escape(svc.detail)}[/]",
                key=svc.service,
            )
