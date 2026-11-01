"""wt command line."""

import json
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import typer
import yaml
from rich.console import Console, ConsoleDimensions
from rich.markup import escape
from rich.table import Table

from wt import __version__, doctor
from wt.catalog import (
    LEGACY_NAME,
    Catalog,
    MergeResult,
    catalog_path,
    load_catalog,
    load_project,
    merge_legacy,
    read_legacy,
    render_check,
    rows,
)
from wt.config import LoadedConfig
from wt.errors import WtError
from wt.messages import CONFIRM_REMOVE
from wt.naming import slugify
from wt.orchestrator import BranchStatus, Choice, Orchestrator, ServiceReport
from wt.projects import KnownProject, known_projects
from wt.state import atomic_write

if TYPE_CHECKING:
    from wt.tui import OrchestratorLike, ProjectEntry

app = typer.Typer(
    help="Run an isolated copy of your dev stack for every git branch.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


class NoLimitConsole(Console):
    """Console that does not limit width when output is not a terminal (D13)."""

    @property
    def size(self) -> ConsoleDimensions:
        if self._width is None and not self.is_terminal:
            return ConsoleDimensions(100_000, super().size.height)
        return super().size

    @size.setter
    def size(self, new_size: tuple[int, int]) -> None:
        Console.size.fset(self, new_size)  # type: ignore[attr-defined]


console = NoLimitConsole(highlight=False)
err = NoLimitConsole(stderr=True, highlight=False)

OUTCOME = {"ok": "[green]ok[/]", "fail": "[red]fail[/]", "skipped": "[dim]skipped[/]"}
LEVEL = {
    "ok": "[green]ok[/]",
    "info": "[blue]info[/]",
    "warn": "[yellow]warn[/]",
    "fail": "[red]fail[/]",
}


def _log(message: str) -> None:
    console.print(f"[dim]>[/] {message}")


def _log_to_stderr(message: str) -> None:
    err.print(f"[dim]>[/] {message}")


def _progress(json_output: bool) -> Callable[[str], None]:
    """Where progress lines go: stdout, or stderr when stdout is reserved for one JSON document."""
    return _log_to_stderr if json_output else _log


CATALOG_HELP = "Path to the catalog file (default: $WT_HOME/catalog.yaml)."
CatalogOption = Annotated[
    Path | None, typer.Option("--catalog", "-c", envvar="WT_CATALOG", help=CATALOG_HELP)
]
JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Print one JSON document on stdout (for scripts and agents)."),
]
UsesOption = Annotated[
    str | None,
    typer.Option("--uses", help="Catalog services for the branch, comma separated: a,b."),
]
LEGACY_KEYS = ("uses", "shared", "catalog")  # keys only a worktree.yaml has
STARTER = """\
# catalog.yaml - every service of this project; `wt add <branch> --uses a,b` picks some per branch.
project: {project}
domain: {project}.localhost      # *.localhost resolves to 127.0.0.1 in browsers

repos:                           # each gets a git worktree per branch; relative paths start here
  app: {repo}

services:
  app:
    repo: app
    compose: docker-compose.yml  # compose file inside the repo
    port: 8000                   # port the container listens on (routed by Traefik)
    subdomain: ""                # "" = https://<branch>.{project}.localhost
    health: /                    # path probed after `up`
    env:
      PUBLIC_URL: "{{{{ url }}}}"
# --- examples: uncomment to try (a host process that needs `app` and a shared Postgres) ---
#   web:
#     repo: app
#     run: npm run dev -- --host 0.0.0.0 --port {{{{ port }}}}  # 0.0.0.0 so Traefik reaches it
#     port: auto                  # a free port per branch, kept for that branch
#     subdomain: web
#     consumes:                   # `app` starts with `web`; its URL arrives in API_URL
#       app: API_URL
#     env:
#       DATABASE_URL: "{{{{ postgres.url('app') }}}}"
#     infra:                      # shared servers this service needs
#       postgres:
#         databases: [app]
#         strategy: template      # template | dump-restore
#         verify: true            # compare row counts after clone
#         base_branch: main
"""


def _catalog_file(ctx: typer.Context) -> Path:
    """The catalog: `--catalog` / WT_CATALOG, else $WT_HOME/catalog.yaml. Never the cwd."""
    explicit: Path | None = ctx.obj
    return explicit if explicit else catalog_path(None, None)


def _load(ctx: typer.Context) -> LoadedConfig:
    return load_project(_catalog_file(ctx))


# ---- JSON output (SPEC §21) ---------------------------------------------------------------
# One pure builder per document; the commands only choose between printing a table and emitting it.

JSON_SCHEMA = 1
FIX_COMMAND = "wt doctor --fix"  # a check whose hint names it can be fixed by that command
JsonDocument = dict[str, Any]
Action = Literal["up", "add", "down", "stop", "remove"]


def _emit(document: JsonDocument) -> None:
    typer.echo(json.dumps(document))


@contextmanager
def _json_errors(json_output: bool) -> Iterator[None]:
    """In --json mode a WtError is the error document on stdout and exit code 1 (SPEC §21)."""
    try:
        yield
    except WtError as exc:
        if not json_output:
            raise
        _emit({"schema": JSON_SCHEMA, "error": str(exc)})
        raise typer.Exit(1) from None


def _service_status(report: ServiceReport, branch_status: str) -> str:
    """A service has no status of its own: its probe says it, else it follows the branch."""
    if report.outcome == "ok":
        return "up"
    if report.outcome == "fail":
        return "degraded"
    return branch_status


def _service_health(report: ServiceReport, *, probed: bool) -> str | None:
    return report.outcome if probed and report.outcome != "skipped" else None


def _branches_json(statuses: list[BranchStatus], *, probed: bool) -> list[JsonDocument]:
    return [
        {
            "branch": branch.branch,
            "status": branch.status,
            "services": [
                {
                    "name": report.service,
                    "kind": report.kind,
                    "status": _service_status(report, branch.status),
                    "health": _service_health(report, probed=probed),
                    "url": report.url,
                    "port": report.port,
                }
                for report in branch.services
            ],
        }
        for branch in sorted(statuses, key=lambda b: b.branch)
    ]


def ls_document(project: str, statuses: list[BranchStatus], *, probed: bool) -> JsonDocument:
    return {
        "schema": JSON_SCHEMA,
        "project": project,
        "branches": _branches_json(statuses, probed=probed),
    }


def ls_all_document(listings: list["ProjectListing"], *, probed: bool) -> JsonDocument:
    """Every known project: its branches, or `error` (and no branches) when it cannot be opened."""
    return {
        "schema": JSON_SCHEMA,
        "projects": [
            {
                "project": listing.project,
                "branches": _branches_json(listing.statuses, probed=probed),
                "error": listing.error or None,
            }
            for listing in listings
        ],
    }


def doctor_document(checks: list[doctor.Check]) -> JsonDocument:
    return {
        "schema": JSON_SCHEMA,
        "ok": not any(c.level == "fail" for c in checks),
        "checks": [
            {
                "name": c.name,
                "ok": c.level != "fail",
                "detail": c.detail,
                "fixable": FIX_COMMAND in c.hint,
            }
            for c in checks
        ],
    }


def catalog_document(cat: Catalog) -> JsonDocument:
    return {
        "schema": JSON_SCHEMA,
        "project": cat.project,
        "domain": cat.domain,
        "services": [
            {
                "name": row.service,
                "repo": row.repo,
                "kind": row.kind,
                "infra": row.infra,
                "consumes": dict(cat.services[row.service].consumes),
                "consumed_by": row.consumed_by,
                "remote": row.remote,
            }
            for row in rows(cat)
        ],
    }


@dataclass(frozen=True)
class ActionOutcome:
    """What an action left behind. `changed` is False for a no-op; `status` None: branch is gone."""

    changed: bool = True
    status: str | None = None
    urls: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def action_document(
    branch: str, action: Action, outcome: ActionOutcome | None = None
) -> JsonDocument:
    outcome = outcome or ActionOutcome()
    return {
        "schema": JSON_SCHEMA,
        "branch": branch,
        "action": action,
        "changed": outcome.changed,
        "status": outcome.status,
        "urls": outcome.urls,
        "warnings": list(outcome.warnings),
    }


def _failed(report: ServiceReport) -> bool:
    return report.outcome == "fail"


def up_document(
    branch: str,
    reports: list[ServiceReport],
    urls: dict[str, str],
    load_warnings: tuple[str, ...],
) -> JsonDocument:
    unhealthy = tuple(f"{r.service} is not healthy: {r.detail}" for r in reports if _failed(r))
    outcome = ActionOutcome(
        status="degraded" if unhealthy else "up",
        urls=urls,
        warnings=(*load_warnings, *unhealthy),
    )
    return action_document(branch, "up", outcome)


def _status_of(orch: Orchestrator, branch: str) -> str | None:
    """The branch's status as `ls` reports it now; None when it is not in state."""
    return next((row.status for row in orch.ls(probe=False) if row.branch == branch), None)


def _version(value: bool) -> None:
    if value:
        console.print(f"wt {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    catalog: CatalogOption = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    ctx.obj = catalog


@app.command()
def init(
    ctx: typer.Context,
    project: Annotated[
        str | None, typer.Option(help="Project name (default: folder name).")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing catalog.")] = False,
) -> None:
    """Write a starter catalog (one project, this folder as its repo, one compose service)."""
    target = _catalog_file(ctx)
    if target.exists() and not force:
        raise WtError(f"catalog already exists at {target}")
    name = project or slugify(Path.cwd().name)
    if not name[0].isalpha():
        name = f"app-{name}"
    # a JSON string is a valid YAML scalar: a path with `:` or `#` cannot break the file
    atomic_write(target, STARTER.format(project=name, repo=json.dumps(str(Path.cwd()))))
    console.print(
        f"wrote {escape(str(target))}\n"
        "edit it, then run [bold]wt doctor[/] and [bold]wt add <branch> --uses app[/]"
    )


def _split_uses(value: str | None) -> list[str] | None:
    """`a, b` -> ['a', 'b']; None when the option was not given (an empty value gives [])."""
    if value is None:
        return None
    return [name.strip() for name in value.split(",") if name.strip()]


def _interactive() -> bool:
    return sys.stdin.isatty()


def _service_picks(answer: str, choices: list[Choice]) -> list[str]:
    """Names for an answer like `1, front 3`: a number is a position in the list shown."""
    picked = []
    for token in answer.replace(",", " ").split():
        if not token.isdigit():
            picked.append(token)
        elif 1 <= int(token) <= len(choices):
            picked.append(choices[int(token) - 1].service)
        else:
            raise WtError(f"there is no service number {token} (choose 1-{len(choices)})")
    return picked


def _ask_services(orch: Orchestrator) -> list[str]:
    """Show the catalog as a numbered list and read the picks; an empty answer cancels."""
    choices = orch.catalog_choices(None)
    for number, choice in enumerate(choices, start=1):
        console.print(f"  {number}. {escape(choice.service)} [dim]({escape(choice.repo)})[/]")
    answer = typer.prompt(
        "Services (numbers or names, comma separated; empty cancels)",
        default="",
        show_default=False,
    )
    picked = _service_picks(answer, choices)
    if not picked:
        err.print("[yellow]cancelled:[/] no services chosen, nothing was created.")
        raise typer.Exit(1)
    return picked


def _report_table(reports: list[ServiceReport]) -> Table:
    table = Table(box=None, pad_edge=False)
    for col in ("SERVICE", "KIND", "URL", "CHECK", "DETAIL"):
        # only DETAIL wraps; a URL is never broken mid-word
        table.add_column(col, overflow="fold", no_wrap=col != "DETAIL")
    for r in reports:
        table.add_row(r.service, r.kind, r.url or "-", OUTCOME[r.outcome], escape(r.detail))
    return table


def _print_up(branch: str, reports: list[ServiceReport]) -> None:
    console.print()
    console.print(_report_table(reports))
    if any(_failed(r) for r in reports):
        err.print(f"\n[red]{branch} is up but not healthy.[/] Check `wt logs {branch} <service>`.")
        raise typer.Exit(1)
    console.print(f"\n[green]{branch} is up.[/]")


@app.command()
def up(  # noqa: PLR0913  (six CLI options; typer needs them as parameters)
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch, e.g. feat/cart.")],
    uses: UsesOption = None,
    build: Annotated[bool, typer.Option("--build", help="Rebuild images.")] = False,
    timeout: Annotated[int, typer.Option(help="Seconds to wait for each step.")] = 120,
    json_output: JsonOption = False,
) -> None:
    """Create worktrees, start services and routes for BRANCH, then verify them."""
    with _json_errors(json_output):
        loaded = _load(ctx)
        orch = Orchestrator(loaded, log=_progress(json_output))
        reports = orch.up(branch, uses=_split_uses(uses), build=build, timeout=timeout)
        if not json_output:
            _print_up(branch, reports)
            return
        _emit(up_document(branch, reports, orch.urls(branch), loaded.warnings))
        if any(_failed(r) for r in reports):
            raise typer.Exit(1)  # same exit code as the table output: up but not healthy


@app.command()
def down(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch.")],
    force: Annotated[
        bool, typer.Option("--force", help="Remove worktrees with uncommitted changes.")
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Stop and remove everything wt created for BRANCH. Branch commits are kept."""
    with _json_errors(json_output):
        done = Orchestrator(_load(ctx), log=_progress(json_output)).down(branch, force=force)
        if json_output:
            _emit(action_document(branch, "down", ActionOutcome(changed=done)))
        elif done:
            console.print(f"[green]{branch} is down.[/]")


@app.command()
def add(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch, e.g. feat/cart.")],
    uses: UsesOption = None,
    json_output: JsonOption = False,
) -> None:
    """Create worktrees and reserve ports for BRANCH without starting anything."""
    with _json_errors(json_output):
        loaded = _load(ctx)
        orch = Orchestrator(loaded, log=_progress(json_output))
        chosen = _split_uses(uses)
        if chosen is None and _interactive() and not json_output:  # --json never prompts
            chosen = _ask_services(orch)
        added = orch.add(branch, chosen)
        if json_output:
            outcome = ActionOutcome(
                changed=added,
                status=_status_of(orch, branch),
                urls=orch.urls(branch),
                warnings=loaded.warnings,
            )
            _emit(action_document(branch, "add", outcome))
        elif added:
            console.print(f"[green]{branch} is added (stopped).[/] Start it with `wt up {branch}`.")


@app.command()
def stop(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch.")],
    json_output: JsonOption = False,
) -> None:
    """Stop BRANCH's services; worktrees, volumes, databases and ports are kept."""
    with _json_errors(json_output):
        orch = Orchestrator(_load(ctx), log=_progress(json_output))
        orch.stop(branch)
        if json_output:
            outcome = ActionOutcome(status=_status_of(orch, branch), urls=orch.urls(branch))
            _emit(action_document(branch, "stop", outcome))
        else:
            console.print(f"[green]{branch} is stopped.[/]")


@app.command()
def remove(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch.")],
    force: Annotated[
        bool, typer.Option("--force", help="Remove worktrees with uncommitted changes.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
    json_output: JsonOption = False,
) -> None:
    """Do `down` for BRANCH, then delete the git branch (unpushed commits are lost)."""
    with _json_errors(json_output):
        if json_output and not yes:
            raise WtError("remove --json never asks for confirmation: pass --yes")
        if not yes:
            # typer.confirm appends ` [y/N]` itself
            question = CONFIRM_REMOVE.format(branch=branch).removesuffix(" y/N")
            typer.confirm(question, abort=True)
        done = Orchestrator(_load(ctx), log=_progress(json_output)).remove(branch, force=force)
        if json_output:
            _emit(action_document(branch, "remove", ActionOutcome()))
        elif done:
            console.print(f"[green]{branch} is removed.[/]")


BRANCH_COLUMNS = ("BRANCH", "STATUS", "UP", "SERVICE", "KIND", "URL", "HEALTH")


@dataclass(frozen=True)
class ProjectListing:
    """One project of `ls --all`: its branches, or `error` when it cannot be opened."""

    project: str
    statuses: list[BranchStatus] = field(default_factory=list)
    error: str = ""


def _branch_table(columns: tuple[str, ...]) -> Table:
    table = Table(box=None, pad_edge=False)
    for col in columns:
        # only HEALTH and UP wrap; a URL (or branch, service) is never broken mid-word
        table.add_column(
            col,
            overflow="fold",
            no_wrap=col not in ("HEALTH", "UP"),
            min_width=3 if col == "UP" else None,
        )
    return table


def _add_branch_rows(
    table: Table, row: BranchStatus, *, no_probe: bool, project: str | None = None
) -> None:
    """The lines of one branch: its first line names the branch, the rest only the services."""
    services = row.services or [ServiceReport("-", "skipped", "")]
    for i, svc in enumerate(services):
        first = i == 0
        # a skipped service was not probed (stopped branch): there is no health to show
        unprobed = no_probe or svc.service == "-" or svc.outcome == "skipped"
        health = "-" if unprobed else f"{OUTCOME[svc.outcome]} [dim]{escape(svc.detail)}[/]"
        containers = f"{row.running}/{row.total}" if row.docker_ok else "docker unavailable"
        cells = [
            row.branch if first else "",
            row.status if first else "",
            containers if first else "",
            svc.service,
            svc.kind if svc.service != "-" else "-",
            svc.url or "-",
            health,
        ]
        if project is not None:
            cells.insert(0, project if first else "")
        table.add_row(*cells)


def _print_details(rows: list[tuple[str, BranchStatus]], *, paths: bool) -> None:
    """The databases and (with `paths`) worktree folders under the table; `label` is a prefix."""
    with_databases = [(label, row) for label, row in rows if row.databases]
    if with_databases:
        console.print("\ndatabases:")
        for label, row in with_databases:
            console.print(
                f"  {label}{row.branch}  {escape(', '.join(row.databases))}", soft_wrap=True
            )
    if paths:
        console.print("\nworktrees:")
        for label, row in rows:
            for repo, path in row.worktrees.items():
                console.print(f"  {label}{row.branch}  {repo}  {escape(path)}", soft_wrap=True)


def _print_branches(rows: list[BranchStatus], *, no_probe: bool, paths: bool) -> None:
    if not rows:
        console.print("no branches are up. Start one with [bold]wt up <branch>[/].")
        return
    table = _branch_table(BRANCH_COLUMNS)
    ordered = sorted(rows, key=lambda r: r.branch)
    for row in ordered:
        _add_branch_rows(table, row, no_probe=no_probe)
    console.print(table)
    _print_details([("", row) for row in ordered], paths=paths)


def _print_all_branches(listings: list[ProjectListing], *, no_probe: bool, paths: bool) -> None:
    """One table for every project: PROJECT first; a project that fails shows its error as a row."""
    if not listings:
        console.print("no projects are known. Start a branch with [bold]wt up <branch>[/].")
        return
    table = _branch_table(("PROJECT", *BRANCH_COLUMNS))
    detail_rows: list[tuple[str, BranchStatus]] = []
    for listing in listings:
        if listing.error:
            table.add_row(listing.project, escape(listing.error), *["-"] * len(BRANCH_COLUMNS[1:]))
        for row in sorted(listing.statuses, key=lambda r: r.branch):
            _add_branch_rows(table, row, no_probe=no_probe, project=listing.project)
            detail_rows.append((f"{listing.project}  ", row))
    console.print(table)
    _print_details(detail_rows, paths=paths)


def _list_projects(*, probe: bool, log: Callable[[str], None]) -> list[ProjectListing]:
    """Every project known from state; one that cannot be opened carries its error text."""
    listings = []
    for known in known_projects():
        path, note = known.config_path, _unusable_note(known)
        if note or path is None:
            listings.append(ProjectListing(known.name, error=note))
            continue
        try:
            statuses = Orchestrator(load_project(path), log=log).ls(probe=probe)
        except WtError as exc:
            listings.append(ProjectListing(known.name, error=str(exc)))
        else:
            listings.append(ProjectListing(known.name, statuses))
    return listings


@app.command("ls")
def ls_(
    ctx: typer.Context,
    no_probe: Annotated[bool, typer.Option("--no-probe", help="Skip HTTP checks.")] = False,
    paths: Annotated[bool, typer.Option("--paths", help="Show worktree folders.")] = False,
    all_projects: Annotated[
        bool,
        typer.Option("--all", help="List every project wt knows, not just the catalog's."),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """List branches, URLs, status and health."""
    with _json_errors(json_output):
        log = _progress(json_output)
        if all_projects:
            listings = _list_projects(probe=not no_probe, log=log)
            if json_output:
                _emit(ls_all_document(listings, probed=not no_probe))
            else:
                _print_all_branches(listings, no_probe=no_probe, paths=paths)
            return
        loaded = _load(ctx)
        statuses = Orchestrator(loaded, log=log).ls(probe=not no_probe)
        if json_output:
            _emit(ls_document(loaded.config.project, statuses, probed=not no_probe))
        else:
            _print_branches(statuses, no_probe=no_probe, paths=paths)


@app.command()
def logs(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch.")],
    service: Annotated[str, typer.Argument(help="Service name from the catalog.")],
    no_follow: Annotated[bool, typer.Option("--no-follow", help="Print and exit.")] = False,
    tail: Annotated[int, typer.Option(help="Lines of history to show.")] = 200,
) -> None:
    """Follow the logs of one service of BRANCH."""
    Orchestrator(_load(ctx), log=_log).logs(branch, service, follow=not no_follow, tail=tail)


def _print_checks(checks: list[doctor.Check]) -> None:
    table = Table(box=None, pad_edge=False, show_header=False)
    for _ in range(3):
        table.add_column(overflow="fold")
    for c in checks:
        table.add_row(LEVEL[c.level], c.name, escape(c.detail))
        if c.hint:
            table.add_row("", "", f"[dim]{escape(c.hint)}[/]")
    console.print(table)


@app.command("doctor")
def doctor_(
    ctx: typer.Context,
    fix: Annotated[
        bool,
        typer.Option(
            "--fix",
            help="Install the local CA, create the network, start Traefik "
            "(and Postgres when configured).",
        ),
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Check docker, git, ports, certificates and name resolution."""
    loaded: LoadedConfig | None = None
    config_error: str | None = None
    try:
        loaded = _load(ctx)
    except WtError as exc:
        config_error = str(exc)  # also the "no catalog at ..." of a missing file
    with _json_errors(json_output):
        checks = doctor.run_checks(loaded, config_error, fix=fix)
        if json_output:
            _emit(doctor_document(checks))
        else:
            _print_checks(checks)
    if any(c.level == "fail" for c in checks):
        raise typer.Exit(1)


def _legacy_file(path: Path, project: str) -> bool:
    """True when `path` is a worktree.yaml, not the catalog of `project` (SPEC §21)."""
    if path.name == LEGACY_NAME:
        return True
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return False  # a broken catalog is reported by loading it, not mistaken for a worktree.yaml
    if not isinstance(raw, dict):
        return False
    return raw.get("project") != project or any(key in raw for key in LEGACY_KEYS)


def _unusable_note(known: KnownProject) -> str:
    """Why a project from state cannot be opened (empty when it can)."""
    path = known.config_path
    if path is None or not known.config_exists:
        return f"config not found: {path or 'unknown'}"
    if _legacy_file(path, known.name):
        return f"created with a worktree.yaml: run wt migrate {path}"
    return ""


def _discover(build: Callable[[Path], Orchestrator]) -> Callable[[], list["ProjectEntry"]]:
    """The all-projects view: every project in state, its orchestrator built on first use."""
    from wt.tui import ProjectEntry  # imported here so other commands start fast

    built: dict[str, Orchestrator] = {}

    def lazy(name: str, path: Path) -> Callable[[], "OrchestratorLike"]:
        def get() -> Orchestrator:
            if name not in built:
                built[name] = build(path)
            return built[name]

        return get

    def entry(known: KnownProject) -> ProjectEntry:
        note, path = _unusable_note(known), known.config_path
        if note or path is None:
            return ProjectEntry(known.name, None, note)
        return ProjectEntry(known.name, lazy(known.name, path), "")

    return lambda: [entry(known) for known in known_projects()]


def open_tui(catalog: Path | None, all_projects: bool = False) -> None:
    """Open the TUI on the catalog's project; with `all_projects` or no catalog, on all projects."""
    from wt.tui import WtApp  # imported here so other commands start fast

    file = (catalog or catalog_path(None, None)).resolve()

    def build(path: Path) -> Orchestrator:
        return Orchestrator(
            load_project(path),
            log=lambda message: tui_app.log_threadsafe(message),
            out=lambda line: tui_app.out_threadsafe(line),
        )

    if file.is_file() and not all_projects:
        tui_app = WtApp(build(file))
    else:
        tui_app = WtApp(projects=_discover(build), missing_catalog=None if file.is_file() else file)
    tui_app.run()


@app.command()
def tui(
    ctx: typer.Context,
    all_projects: Annotated[
        bool, typer.Option("--all", help="List every project wt knows, not just the catalog's.")
    ] = False,
) -> None:
    """Open the terminal UI (branches, services, logs) for the catalog, or all projects."""
    open_tui(ctx.obj, all_projects)


worktree_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


@worktree_app.command()
def worktree_command(catalog: CatalogOption = None) -> None:
    """Open the terminal UI: the catalog's project, or every project wt knows without a catalog."""
    open_tui(catalog)


def _check_catalog(path: Path) -> None:
    """Load the catalog, resolve each service alone (as `wt` does), then render its templates."""
    try:
        loaded = load_catalog(path)
    except WtError as exc:
        console.print(f"[red]catalog invalid:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None
    failures = [
        _service_failures(loaded.path, list(loaded.catalog.services)),
        render_check(loaded),
    ]
    for problems in failures:
        for name, message in problems.items():
            console.print(
                f"[red]catalog invalid:[/] service {escape(repr(name))}: {escape(message)}"
            )
    if any(failures):
        raise typer.Exit(1)
    console.print(f"[green]catalog ok:[/] {escape(str(loaded.path))}")


def _service_failures(catalog_file: Path, names: list[str]) -> dict[str, str]:
    """Why each service fails to resolve on its own (services that resolve are left out)."""
    failures: dict[str, str] = {}
    for name in names:
        try:
            load_project(catalog_file, uses=[name])
        except WtError as exc:
            failures[name] = str(exc)
    return failures


def _print_catalog(cat: Catalog) -> None:
    table = Table(box=None, pad_edge=False)
    for col in ("SERVICE", "REPO", "KIND", "INFRA", "CONSUMES", "CONSUMED BY", "REMOTE"):
        table.add_column(col, overflow="fold", no_wrap=True)
    for row in rows(cat):
        table.add_row(
            row.service,
            row.repo,
            row.kind,
            ", ".join(row.infra) or "-",
            ", ".join(row.consumes) or "-",
            ", ".join(row.consumed_by) or "-",
            row.remote or "-",
        )
    console.print(table)


@app.command()
def catalog(
    ctx: typer.Context,
    check: Annotated[bool, typer.Option("--check", help="Validate the catalog and exit.")] = False,
    json_output: JsonOption = False,
) -> None:
    """Show the catalog (services, repos, infra, who consumes whom)."""
    with _json_errors(json_output):
        path = _catalog_file(ctx)
        if check and json_output:
            raise WtError("--check prints its own report: use it without --json")
        if check:
            _check_catalog(path)
            return
        cat = load_catalog(path).catalog
        if json_output:
            _emit(catalog_document(cat))
        else:
            _print_catalog(cat)


def _read_catalog_mapping(path: Path) -> dict[str, Any] | None:
    """The raw catalog to merge into; None when there is no file (or it is empty)."""
    if not path.exists():
        return None
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise WtError(f"{path} is not valid YAML:\n{exc}") from exc
    if raw is not None and not isinstance(raw, dict):
        raise WtError(f"{path} must contain a YAML mapping")
    return raw


def _write_validated(target: Path, result: MergeResult) -> None:
    """Write the merged catalog at `target` only if it loads; a failure leaves `target` alone.

    The anchor load checks the file as a whole; the load of the migrated stack checks the
    services that came in, which the anchor may never reach.
    """
    text = yaml.safe_dump(result.catalog, sort_keys=False)
    with tempfile.TemporaryDirectory() as folder:
        candidate = Path(folder) / target.name  # same name: errors say `invalid catalog.yaml`
        candidate.write_text(text)
        load_project(candidate)
        load_project(candidate, uses=list(result.uses))
    atomic_write(target, text)


def _report_migration(
    result: MergeResult, adopted: list[str], target: Path, *, rewrote: bool
) -> None:
    for name in result.added:
        console.print(f"added service {escape(name)}")
    for name in result.skipped:
        console.print(f"skipped service {escape(name)} (already in the catalog)")
    console.print(f"adopted branches: {escape(', '.join(adopted)) if adopted else 'none'}")
    if rewrote:
        console.print(
            f"[dim]rewrote {escape(str(target))}; comments in an existing catalog are not "
            "preserved when it is rewritten.[/]"
        )


@app.command()
def migrate(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="A worktree.yaml from wt 0.5.x.")],
) -> None:
    """Import a worktree.yaml into the catalog (created if missing); safe to repeat."""
    target = _catalog_file(ctx)
    existing = _read_catalog_mapping(target)
    result = merge_legacy(
        existing,
        read_legacy(path),
        path.parent,
        path.name,
        catalog_dir=target.parent,
    )
    # An unchanged catalog is not rewritten: dumping it would drop the user's comments.
    rewrite = bool(result.added) or existing is None
    if rewrite:
        _write_validated(target, result)
    adopted = Orchestrator(load_project(target), log=_log).adopt_branches(list(result.uses))
    _report_migration(result, adopted, target, rewrote=rewrite and existing is not None)


def main() -> None:
    _run(app)


def worktree_main() -> None:
    _run(worktree_app)


def _run(entry: typer.Typer) -> None:
    try:
        entry()
    except WtError as exc:
        err.print(f"[red]error:[/] {escape(str(exc))}")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
