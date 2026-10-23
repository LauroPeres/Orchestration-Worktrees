"""Terminal UI: branches, services of the selected branch and a log pane (SPEC §15)."""

from typing import ClassVar, Protocol

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Label, ListItem, ListView, RichLog

from wt.config import Config
from wt.errors import WtError
from wt.orchestrator import BranchStatus

REFRESH_SECONDS = 2.0
HEALTH = {"ok": "[green]ok[/]", "fail": "[red]fail[/]", "skipped": "[dim]skipped[/]"}


class OrchestratorLike(Protocol):
    """The part of `Orchestrator` the TUI reads."""

    cfg: Config

    def ls(self, *, probe: bool = True) -> list[BranchStatus]: ...


def branch_label(row: BranchStatus) -> str:
    return f"{escape(row.branch)}  [dim]{escape(row.status)}[/]"


class WtApp(App[None]):
    """Read-only view of `wt ls`. Actions (up, down, logs) are added as bindings and workers."""

    TITLE = "wt"
    CSS = """
    #main { height: 1fr; }
    #branches { width: 32; border: round $primary; }
    #services { width: 1fr; border: round $primary; }
    #log { height: 10; border: round $primary; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, orch: OrchestratorLike) -> None:
        super().__init__()
        self.orch = orch
        self.rows: list[BranchStatus] = []
        self._loading = False

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
        self._loading = False
        rows = sorted(rows, key=lambda r: r.branch)
        view = self.query_one("#branches", ListView)
        previous = self.selected_branch
        same = [r.branch for r in rows] == [r.branch for r in self.rows]
        self.rows = rows
        if same:
            for item, row in zip(view.query(ListItem), rows, strict=True):
                item.query_one(Label).update(branch_label(row))
        else:
            await view.clear()
            await view.extend(ListItem(Label(branch_label(r))) for r in rows)
            names = [r.branch for r in rows]
            view.index = names.index(previous) if previous in names else (0 if rows else None)
        self._show_services()

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
