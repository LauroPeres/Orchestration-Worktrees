"""Terminal UI: branches, services of the selected branch and a log pane (SPEC §15)."""

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
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
TAIL_LINES = 200
HEALTH = {"ok": "[green]ok[/]", "fail": "[red]fail[/]", "skipped": "[dim]skipped[/]"}


class OrchestratorLike(Protocol):
    """The part of `Orchestrator` the TUI reads."""

    cfg: Config

    def ls(self, *, probe: bool = True) -> list[BranchStatus]: ...

    def up(self, branch: str) -> object: ...

    def down(self, branch: str, *, force: bool = False) -> bool: ...

    def log_command(self, branch: str, service: str, *, follow: bool, tail: int) -> list[str]: ...


@dataclass(frozen=True)
class ProjectEntry:
    """One project in the all-projects view. `orch` is None when its config file is gone."""

    name: str
    orch: Callable[[], OrchestratorLike] | None
    note: str = ""


@dataclass(frozen=True)
class Row:
    """A line of the left pane: a project header (`status` None) or one of its branches."""

    project: str
    status: BranchStatus | None = None
    note: str = ""
    stale: bool = False

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.project, self.status.branch if self.status else None)


def row_label(row: Row, *, tree: bool) -> str:
    if row.status is not None:
        return ("  " if tree else "") + branch_label(row.status)
    name = escape(row.project)
    if row.stale:
        return f"[dim]{name}  {escape(row.note)}[/]"
    return f"[bold]{name}[/]" + (f"  [red]{escape(row.note)}[/]" if row.note else "")


def _kill_group(tail: subprocess.Popen[str]) -> None:
    """Kill the tail process and the children it started."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(tail.pid, signal.SIGKILL)


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
    """`wt ls` view, up/down actions and a service log tail (SPEC §15)."""

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
        Binding("l", "tail_log", "Logs"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        orch: OrchestratorLike | None = None,
        *,
        projects: Callable[[], list[ProjectEntry]] | None = None,
    ) -> None:
        """One project (`orch`), or a project tree from `projects` (called on every refresh)."""
        super().__init__()
        if (orch is None) == (projects is None):
            raise ValueError("pass either orch or projects")
        self.orch = orch
        self._discover = projects
        self._entries: dict[str, ProjectEntry] = {}
        self.rows: list[Row] = []
        self._said_empty = False
        self._loading = False
        self._busy = False
        self._tail_lock = threading.Lock()
        self._tail_gen = 0
        self._tail_proc: subprocess.Popen[str] | None = None
        self._tail_target: tuple[str, str] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            yield ListView(id="branches")
            yield DataTable(id="services", cursor_type="row")
        yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.orch.cfg.project if self.orch else "all projects"
        branches = self.query_one("#branches", ListView)
        branches.border_title = "Branches" if self.orch else "Projects"
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
    def selected_row(self) -> Row | None:
        index = self.query_one("#branches", ListView).index
        if index is None or index >= len(self.rows):
            return None
        return self.rows[index]

    @property
    def selected_branch(self) -> str | None:
        row = self.selected_row
        return row.status.branch if row and row.status else None

    @property
    def selected_project(self) -> str | None:
        row = self.selected_row
        return row.project if row else None

    def _orch_for(self, project: str) -> OrchestratorLike:
        """The project's orchestrator (built lazily in the all-projects view); may raise."""
        if self.orch is not None:
            return self.orch
        entry = self._entries.get(project)
        if entry is None or entry.orch is None:
            raise WtError(f"{project}: {entry.note if entry else 'unknown project'}")
        return entry.orch()

    def _target(self) -> str | None:
        """The project an action applies to, or None (with a message) when it has none."""
        row = self.selected_row
        if row is None:
            self.write_log("[yellow]nothing selected[/]")
            return None
        if row.stale:
            self.write_log(f"[yellow]{escape(row.project)}: {escape(row.note)}[/]")
            return None
        return row.project

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
        branch, project = self.selected_branch, self._target()
        if branch is not None and project is not None and self._guard():
            self._start(f"up {branch}", lambda: self._orch_for(project).up(branch))

    def action_up_new(self) -> None:
        if not self._guard() or (project := self._target()) is None:
            return

        def got(name: str | None) -> None:
            if name and self._guard():
                self._start(f"up {name}", lambda: self._orch_for(project).up(name))

        self.push_screen(InputScreen("New branch name"), got)

    def action_down_selected(self) -> None:
        branch, project = self.selected_branch, self._target()
        if branch is None or project is None or not self._guard():
            return

        def confirmed(yes: bool | None) -> None:
            if yes and self._guard():
                self._start(
                    f"down {branch}",
                    lambda: self._orch_for(project).down(branch),
                    on_error=lambda: self._offer_force(project, branch),
                )

        self.push_screen(ConfirmScreen(f"Bring down {branch}?"), confirmed)

    def _offer_force(self, project: str, branch: str) -> None:
        def confirmed(yes: bool | None) -> None:
            if yes and self._guard():
                self._start(
                    f"down --force {branch}",
                    lambda: self._orch_for(project).down(branch, force=True),
                )

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

    # -- log tail -------------------------------------------------------------------

    @property
    def selected_service(self) -> str | None:
        table = self.query_one("#services", DataTable)
        if table.row_count == 0:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def action_tail_log(self) -> None:
        branch, service = self.selected_branch, self.selected_service
        project = self.selected_project
        if branch is None or service is None or project is None:
            self.write_log("[yellow]select a service first[/]")
            return
        self._stop_tail()
        with self._tail_lock:
            self._tail_target = (branch, service)
            gen = self._tail_gen
        self.write_log(f"[bold]logs {escape(branch)} / {escape(service)}[/]")
        self._run_tail(project, branch, service, gen)

    def _stop_tail(self) -> None:
        """Kill the running tail (if any) and make its thread's late output a no-op."""
        with self._tail_lock:
            self._tail_gen += 1
            self._tail_target = None
            tail, self._tail_proc = self._tail_proc, None
        if tail is not None and tail.poll() is None:
            _kill_group(tail)

    def _tail_say(self, gen: int, message: str, *, plain: bool = False) -> None:
        if gen != self._tail_gen:
            return
        with contextlib.suppress(RuntimeError):  # app already gone
            self.call_from_thread(self._write_plain if plain else self.write_log, message)

    @work(thread=True, group="tail", exit_on_error=False)
    def _run_tail(self, project: str, branch: str, service: str, gen: int) -> None:
        try:
            args = self._orch_for(project).log_command(
                branch, service, follow=True, tail=TAIL_LINES
            )
            tail = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                start_new_session=True,  # so the kill reaches children of a shell command
            )
        except WtError as exc:
            self._tail_say(gen, f"[red]{escape(str(exc))}[/]")
            return
        except OSError as exc:
            self._tail_say(gen, f"[red]{escape(str(exc))}[/]")
            return
        with self._tail_lock:
            if gen != self._tail_gen:
                _kill_group(tail)
                tail.wait()
                return
            self._tail_proc = tail
        assert tail.stdout is not None
        try:
            for line in tail.stdout:
                self._tail_say(gen, line.rstrip("\r\n"), plain=True)
        finally:
            tail.stdout.close()
            tail.wait()

    async def action_quit(self) -> None:
        self._stop_tail()
        await super().action_quit()

    def on_unmount(self) -> None:
        self._stop_tail()

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
            result.append(Row(entry.name))
            result.extend(Row(entry.name, s) for s in statuses)
        return result

    @work(thread=True, group="refresh")
    def _load_rows(self) -> None:
        try:
            rows = self._collect()
        except WtError as exc:
            self.call_from_thread(self._load_failed, str(exc))
        else:
            self.call_from_thread(self._apply_rows, rows)  # type: ignore[arg-type]

    def _load_failed(self, message: str) -> None:
        self._loading = False
        self.write_log(f"[red]{escape(message)}[/]")

    async def _apply_rows(self, rows: list[Row]) -> None:
        try:
            view = self.query_one("#branches", ListView)
            tree = self.orch is None
            previous = self.selected_row
            items = list(view.query(ListItem))
            same = [r.key for r in rows] == [r.key for r in self.rows] and len(items) == len(rows)
            self.rows = rows
            if same:
                for item, row in zip(items, rows, strict=True):
                    item.query_one(Label).update(row_label(row, tree=tree))
            else:
                await view.clear()
                await view.extend(ListItem(Label(row_label(r, tree=tree))) for r in rows)
                keys = [r.key for r in rows]
                if previous is not None and previous.key in keys:
                    view.index = keys.index(previous.key)
                else:
                    view.index = 0 if rows else None
            if not rows and tree and not self._said_empty:
                self._said_empty = True
                self.write_log(
                    "[dim]No projects with branches yet. Run `wt up <branch>` in one.[/]"
                )
            self._show_services()
        except NoMatches:
            pass  # the app is closing: its widgets are gone before this late refresh arrived
        finally:
            # Cleared only now: a refresh that starts while the list is being rebuilt would see
            # the new rows but not the new items yet.
            self._loading = False

    @on(ListView.Highlighted, "#branches")
    def _highlighted(self) -> None:
        if self._tail_target is not None and self._tail_target[0] != self.selected_branch:
            self._stop_tail()
        self._show_services()

    @on(DataTable.RowHighlighted, "#services")
    def _row_highlighted(self) -> None:
        if self._tail_target is not None and self._tail_target != (
            self.selected_branch,
            self.selected_service,
        ):
            self._stop_tail()

    def _show_services(self) -> None:
        table = self.query_one("#services", DataTable)
        keep = self.selected_service
        table.clear()
        selected = self.selected_row
        row = selected.status if selected else None
        if self.orch is None:
            self.sub_title = "all projects"
        elif row is not None and not row.docker_ok:
            self.sub_title = f"{self.orch.cfg.project} (docker unavailable)"
        else:
            self.sub_title = self.orch.cfg.project
        if row is None:
            return
        for svc in row.services:
            table.add_row(
                svc.service,
                svc.kind,
                svc.url or "-",
                f"{HEALTH[svc.outcome]} [dim]{escape(svc.detail)}[/]",
                key=svc.service,
            )
        if keep is not None:
            with contextlib.suppress(Exception):  # service gone: stay on the first row
                table.move_cursor(row=table.get_row_index(keep))
