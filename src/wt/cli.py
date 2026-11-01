"""wt command line."""

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
import yaml
from rich.console import Console, ConsoleDimensions
from rich.markup import escape
from rich.table import Table

from wt import __version__, doctor
from wt.catalog import (
    MergeResult,
    catalog_path,
    load_catalog,
    load_project,
    merge_legacy,
    read_legacy,
    rows,
)
from wt.config import CONFIG_NAME, LoadedConfig
from wt.errors import WtError
from wt.naming import slugify
from wt.orchestrator import Choice, Orchestrator, ServiceReport
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


CATALOG_HELP = "Path to the catalog file (default: $WT_HOME/catalog.yaml)."
CatalogOption = Annotated[
    Path | None, typer.Option("--catalog", "-c", envvar="WT_CATALOG", help=CATALOG_HELP)
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
    choices = orch.catalog_choices(None) or []
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


@app.command()
def up(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch, e.g. feat/cart.")],
    uses: UsesOption = None,
    build: Annotated[bool, typer.Option("--build", help="Rebuild images.")] = False,
    timeout: Annotated[int, typer.Option(help="Seconds to wait for each step.")] = 120,
) -> None:
    """Create worktrees, start services and routes for BRANCH, then verify them."""
    orch = Orchestrator(_load(ctx), log=_log)
    reports = orch.up(branch, uses=_split_uses(uses), build=build, timeout=timeout)
    console.print()
    console.print(_report_table(reports))
    if any(r.outcome == "fail" for r in reports):
        err.print(f"\n[red]{branch} is up but not healthy.[/] Check `wt logs {branch} <service>`.")
        raise typer.Exit(1)
    console.print(f"\n[green]{branch} is up.[/]")


@app.command()
def down(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch.")],
    force: Annotated[
        bool, typer.Option("--force", help="Remove worktrees with uncommitted changes.")
    ] = False,
) -> None:
    """Stop and remove everything wt created for BRANCH. Branch commits are kept."""
    if Orchestrator(_load(ctx), log=_log).down(branch, force=force):
        console.print(f"[green]{branch} is down.[/]")


@app.command()
def add(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch, e.g. feat/cart.")],
    uses: UsesOption = None,
) -> None:
    """Create worktrees and reserve ports for BRANCH without starting anything."""
    orch = Orchestrator(_load(ctx), log=_log)
    chosen = _split_uses(uses)
    if chosen is None and _interactive():
        chosen = _ask_services(orch)
    if orch.add(branch, chosen):
        console.print(f"[green]{branch} is added (stopped).[/] Start it with `wt up {branch}`.")


@app.command()
def stop(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch.")],
) -> None:
    """Stop BRANCH's services; worktrees, volumes, databases and ports are kept."""
    Orchestrator(_load(ctx), log=_log).stop(branch)
    console.print(f"[green]{branch} is stopped.[/]")


@app.command()
def remove(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch.")],
    force: Annotated[
        bool, typer.Option("--force", help="Remove worktrees with uncommitted changes.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Do `down` for BRANCH, then delete the git branch (unpushed commits are lost)."""
    if not yes:
        typer.confirm(
            f"remove {branch}: removes what wt created for it (containers, volumes, databases, "
            "worktrees) and deletes the git branch in every repo of the project where it exists; "
            "unpushed commits are lost. Remove?",
            abort=True,
        )
    if Orchestrator(_load(ctx), log=_log).remove(branch, force=force):
        console.print(f"[green]{branch} is removed.[/]")


@app.command("ls")
def ls_(
    ctx: typer.Context,
    no_probe: Annotated[bool, typer.Option("--no-probe", help="Skip HTTP checks.")] = False,
    paths: Annotated[bool, typer.Option("--paths", help="Show worktree folders.")] = False,
) -> None:
    """List branches, URLs, status and health."""
    rows = Orchestrator(_load(ctx), log=_log).ls(probe=not no_probe)
    if not rows:
        console.print("no branches are up. Start one with [bold]wt up <branch>[/].")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("BRANCH", "STATUS", "UP", "SERVICE", "KIND", "URL", "HEALTH"):
        # only HEALTH and UP wrap; a URL (or branch, service) is never broken mid-word
        table.add_column(
            col,
            overflow="fold",
            no_wrap=col not in ("HEALTH", "UP"),
            min_width=3 if col == "UP" else None,
        )
    for row in sorted(rows, key=lambda r: r.branch):
        services = row.services or [ServiceReport("-", "skipped", "")]
        for i, svc in enumerate(services):
            first = i == 0
            # a skipped service was not probed (stopped branch): there is no health to show
            unprobed = no_probe or svc.service == "-" or svc.outcome == "skipped"
            health = "-" if unprobed else f"{OUTCOME[svc.outcome]} [dim]{escape(svc.detail)}[/]"
            containers = f"{row.running}/{row.total}" if row.docker_ok else "docker unavailable"
            table.add_row(
                row.branch if first else "",
                row.status if first else "",
                containers if first else "",
                svc.service,
                svc.kind if svc.service != "-" else "-",
                svc.url or "-",
                health,
            )
    console.print(table)
    with_databases = [row for row in sorted(rows, key=lambda r: r.branch) if row.databases]
    if with_databases:
        console.print("\ndatabases:")
        for row in with_databases:
            console.print(f"  {row.branch}  {escape(', '.join(row.databases))}", soft_wrap=True)
    if paths:
        console.print("\nworktrees:")
        for row in sorted(rows, key=lambda r: r.branch):
            for repo, path in row.worktrees.items():
                console.print(f"  {row.branch}  {repo}  {escape(path)}", soft_wrap=True)


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
) -> None:
    """Check docker, git, ports, certificates and name resolution."""
    loaded: LoadedConfig | None = None
    config_error: str | None = None
    try:
        loaded = _load(ctx)
    except WtError as exc:
        config_error = str(exc)  # also the "no catalog at ..." of a missing file
    checks = doctor.run_checks(loaded, config_error, fix=fix)
    table = Table(box=None, pad_edge=False, show_header=False)
    for _ in range(3):
        table.add_column(overflow="fold")
    for c in checks:
        table.add_row(LEVEL[c.level], c.name, escape(c.detail))
        if c.hint:
            table.add_row("", "", f"[dim]{escape(c.hint)}[/]")
    console.print(table)
    if any(c.level == "fail" for c in checks):
        raise typer.Exit(1)


def _legacy_file(path: Path, project: str) -> bool:
    """True when `path` is a worktree.yaml, not the catalog of `project` (SPEC §21)."""
    if path.name == CONFIG_NAME:
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
    """Load the catalog, then resolve every service alone, as `wt` does when it is selected."""
    try:
        loaded = load_catalog(path)
    except WtError as exc:
        console.print(f"[red]catalog invalid:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None
    failures = _service_failures(loaded.path, list(loaded.catalog.services))
    for name, message in failures.items():
        console.print(f"[red]catalog invalid:[/] service {escape(repr(name))}: {escape(message)}")
    if failures:
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


@app.command()
def catalog(
    ctx: typer.Context,
    check: Annotated[bool, typer.Option("--check", help="Validate the catalog and exit.")] = False,
) -> None:
    """Show the catalog (services, repos, infra, who consumes whom)."""
    path = _catalog_file(ctx)
    if check:
        _check_catalog(path)
        return
    table = Table(box=None, pad_edge=False)
    for col in ("SERVICE", "REPO", "KIND", "INFRA", "CONSUMES", "CONSUMED BY", "REMOTE"):
        table.add_column(col, overflow="fold", no_wrap=True)
    for row in rows(load_catalog(path).catalog):
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
