---
name: cli-patterns
description: How commands and TUI screens are written in wt's interface layer — Typer command shape, options, output with rich, errors, and how CLI tests use CliRunner. Use when adding or changing a command, option, output or TUI view in src/wt/cli.py or src/wt/tui.py.
---

# CLI and TUI patterns — wt

## A command
```python
@app.command()
def stop(
    ctx: typer.Context,
    branch: Annotated[str, typer.Argument(help="Git branch.")],
) -> None:
    """Stop BRANCH's services; worktrees, volumes, databases and ports are kept."""
    Orchestrator(_load(ctx), log=_log).stop(branch)
    console.print(f"[green]{branch} is stopped.[/]")
```
- `_load(ctx)` finds and loads `worktree.yaml` (honors `--config`); `_log` is the progress printer.
- The docstring is the help text: one line, says what is kept and what is removed.
- Exactly one core call; the command only chooses what to print (tables: `_report_table`).
- `WtError` propagates; `_run` prints it without a traceback and exits 1.
- Both entry points (`wt`, `worktree`) share `app`; don't add a second Typer app.

## TUI (`tui.py`)
Consumes only the core's view types (`BranchStatus`, `RepoRow`, `Choice`) and calls core methods;
no state or subprocess access. Long work runs in a worker so the UI never blocks.

## Tests (`tests/test_cli.py`)
`runner = CliRunner()`; `runner.invoke(app, [...])`; assert `exit_code` (print `result.output` in the
assert message) and the visible text. Use the `docker`/`orch` fixtures; never call Docker for real.
