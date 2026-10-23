"""wt command line."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console, ConsoleDimensions
from rich.markup import escape
from rich.table import Table

from wt import __version__, doctor
from wt.catalog import catalog_path, load_catalog, rows
from wt.config import CONFIG_NAME, STARTER, LoadedConfig, find_config, load_config
from wt.errors import WtError
from wt.naming import slugify
from wt.orchestrator import Orchestrator, ServiceReport

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
err = Console(stderr=True, highlight=False)

OUTCOME = {"ok": "[green]ok[/]", "fail": "[red]fail[/]", "skipped": "[dim]skipped[/]"}
LEVEL = {
    "ok": "[green]ok[/]",
    "info": "[blue]info[/]",
    "warn": "[yellow]warn[/]",
    "fail": "[red]fail[/]",
}


def _log(message: str) -> None:
    console.print(f"[dim]>[/] {message}")


def _config_path(ctx: typer.Context) -> Path | None:
    explicit: Path | None = ctx.obj
    return explicit if explicit else find_config(Path.cwd())


def _load(ctx: typer.Context) -> LoadedConfig:
    path = _config_path(ctx)
    if path is None:
        raise WtError(
            f"no {CONFIG_NAME} in this folder or its parents; run `wt init` or pass --config"
        )
    return load_config(path)


def _version(value: bool) -> None:
    if value:
        console.print(f"wt {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", envvar="WT_CONFIG", help=f"Path to {CONFIG_NAME}."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    ctx.obj = config


@app.command()
def init(
    project: Annotated[
        str | None, typer.Option(help="Project name (default: folder name).")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a starter worktree.yaml in the current folder."""
    target = Path.cwd() / CONFIG_NAME
    if target.exists() and not force:
        raise WtError(f"{target} already exists (use --force to overwrite)")
    name = project or slugify(Path.cwd().name)
    if not name[0].isalpha():
        name = f"app-{name}"
    target.write_text(STARTER.format(project=name))
    console.print(
        f"wrote {target}\nedit it, then run [bold]wt doctor[/] and [bold]wt up <branch>[/]"
    )


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
    build: Annotated[bool, typer.Option("--build", help="Rebuild images.")] = False,
    timeout: Annotated[int, typer.Option(help="Seconds to wait for each step.")] = 120,
) -> None:
    """Create worktrees, start services and routes for BRANCH, then verify them."""
    orch = Orchestrator(_load(ctx), log=_log)
    reports = orch.up(branch, build=build, timeout=timeout)
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
) -> None:
    """Create worktrees and reserve ports for BRANCH without starting anything."""
    if Orchestrator(_load(ctx), log=_log).add(branch):
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
            f"remove {branch}: deletes its containers, volumes, databases, worktrees and the "
            "git branch; unpushed commits are lost. Remove?",
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
            health = (
                "-"
                if no_probe or svc.service == "-"
                else (f"{OUTCOME[svc.outcome]} [dim]{escape(svc.detail)}[/]")
            )
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
    service: Annotated[str, typer.Argument(help="Service name from worktree.yaml.")],
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
    path = _config_path(ctx)
    if path is not None:
        try:
            loaded = load_config(path)
        except WtError as exc:
            config_error = str(exc)
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


def open_tui(config: Path | None) -> None:
    """Open the TUI: the project of `config` (or of the cwd), else every project wt knows."""
    from wt.projects import known_projects  # imported here so other commands start fast
    from wt.tui import OrchestratorLike, ProjectEntry, WtApp

    def build(path: Path) -> Orchestrator:
        return Orchestrator(
            load_config(path),
            log=lambda message: tui_app.log_threadsafe(message),
            out=lambda line: tui_app.out_threadsafe(line),
        )

    path = config or find_config(Path.cwd())
    if path is not None:
        tui_app = WtApp(build(path))
    else:
        built: dict[str, Orchestrator] = {}

        def lazy(name: str, config_path: Path) -> Callable[[], OrchestratorLike]:
            def get() -> Orchestrator:
                if name not in built:
                    built[name] = build(config_path)
                return built[name]

            return get

        def discover() -> list[ProjectEntry]:
            return [
                ProjectEntry(p.name, lazy(p.name, p.config_path), "")
                if p.config_path is not None and p.config_exists
                else ProjectEntry(p.name, None, f"config not found: {p.config_path or 'unknown'}")
                for p in known_projects()
            ]

        tui_app = WtApp(projects=discover)
    tui_app.run()


@app.command()
def tui(ctx: typer.Context) -> None:
    """Open the terminal UI (branches, services, logs). Outside a project: all projects."""
    open_tui(ctx.obj)


worktree_app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


@worktree_app.command()
def worktree_command(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", envvar="WT_CONFIG", help=f"Path to {CONFIG_NAME}."),
    ] = None,
) -> None:
    """Open the terminal UI: this project, or every project wt knows when run outside one."""
    open_tui(config)


@app.command()
def catalog(
    ctx: typer.Context,
    check: Annotated[bool, typer.Option("--check", help="Validate the catalog and exit.")] = False,
) -> None:
    """Show the user catalog (services, repos, infra, who consumes whom)."""
    config = _config_path(ctx)
    explicit = None
    if config is not None:
        try:
            raw = yaml.safe_load(config.read_text())
        except (OSError, yaml.YAMLError):
            raw = None  # a broken worktree.yaml is not this command's problem
        value = raw.get("catalog") if isinstance(raw, dict) else None
        explicit = value if isinstance(value, str) else None
    path = catalog_path(explicit, config.parent if config else None)
    if check:
        try:
            loaded = load_catalog(path)
        except WtError as exc:
            console.print(f"[red]catalog invalid:[/] {escape(str(exc))}")
            raise typer.Exit(1) from None
        console.print(f"[green]catalog ok:[/] {escape(str(loaded.path))}")
        return
    table = Table(box=None, pad_edge=False)
    for col in ("SERVICE", "REPO", "KIND", "INFRA", "CONSUMES", "CONSUMED BY"):
        table.add_column(col, overflow="fold", no_wrap=True)
    for row in rows(load_catalog(path).catalog):
        table.add_row(
            row.service,
            row.repo,
            row.kind,
            ", ".join(row.infra) or "-",
            ", ".join(row.consumes) or "-",
            ", ".join(row.consumed_by) or "-",
        )
    console.print(table)


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
