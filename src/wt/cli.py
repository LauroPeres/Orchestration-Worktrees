"""wt command line."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from wt import __version__, doctor
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
console = Console(highlight=False)
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


def main() -> None:
    try:
        app()
    except WtError as exc:
        err.print(f"[red]error:[/] {escape(str(exc))}")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
