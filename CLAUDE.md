# Orchestration-Worktrees (`wt`)

A CLI + TUI that runs an isolated copy of a user-defined dev stack per git branch: git worktrees,
Docker Compose or host processes, Traefik routes with TLS, and one slice per branch of shared
Postgres/Kafka/Redis/Keycloak servers. Source of truth for scope and behavior: `docs/SPEC.md`.

## Rules of this repo
- **Clean room.** Write everything from scratch: never code, text, names, hosts or metrics from
  the author's employer. Public knowledge and new work only.
- **Generic.** The tool ships no user services; stack-specific things live in `worktree.yaml` or
  `examples/`. README numbers come only from `examples/`, with the command that measured them.
- **SPEC first.** Behavior changes go through the `spec-decision` skill (update `docs/SPEC.md`)
  before code. Scope follows the roadmap (SPEC §10): small working versions.
- **PRs:** small and focused (one module or concern each). Claude may merge its own PRs when CI is
  green, with "Create a merge commit", lowest stacked PR first.

## Stack
Python 3.11+ · uv · typer (CLI) · textual (TUI) · pydantic (config) · jinja2 (sandboxed templates)
· Docker Compose · Traefik (file provider) · mkcert. Checks: ruff, mypy (strict), import-linter, pytest.

## Commands (repo root)
| What | Command |
|---|---|
| Install | `uv sync` |
| Run from source | `uv run wt --help` |
| **All checks** (what CI runs) | `uv run ruff check && uv run ruff format --check && uv run mypy && uv run lint-imports && uv run pytest -q` |
| Tests / one file / one test | `uv run pytest -q` · `uv run pytest -q tests/test_naming.py` · `… -k <name>` |
| Real runs (Docker daemon) | `uv run pytest -m docker` (slow; e2e agent) |
| Layer contracts | `uv run lint-imports` |

## Folder map — layers (top imports down, never up) and owners
| Layer | Modules in `src/wt/` | Owner |
|---|---|---|
| interface | `cli`, `tui`, `__main__` | `interface-coder` |
| core | `orchestrator`, `doctor` | `core-coder` (+ `tests/conftest.py`) |
| providers | `providers/` (`base` = the `Provider` protocol) | `providers-coder` |
| runtime | `proc`, `git`, `compose`, `host`, `router`, `tls`, `health`, `state`, `projects` | `runtime-coder` |
| model | `config`, `catalog`, `naming`, `templating`, `paths`, `errors` | `model-coder` |
Tests are `tests/test_<module>.py`, owned with their module. `tests/integration/`, `tests/test_example_*`
and `examples/` belong to `e2e`. Exact map: `.claude/ownership.json`.

## Conventions (from the code)
- External commands go through `proc.run`; only `proc` and `host` (long-lived host processes)
  touch `subprocess`. State files are written with `state.atomic_write` under `state.file_lock`.
- User-facing failures raise `WtError` with a clear message; the CLI prints it without a traceback.
- Names and ports are derived from (project, branch) in `naming`, never chosen ad hoc.
- Commands are idempotent (`up` twice is safe; `down` cleans a half-created branch) and a step is
  done when **verified** (healthy URL, checked slice), not when a command returned.

## Never hand-edit
`uv.lock` (use `uv add`) · `.venv/` · `docs/SPEC.md` outside `spec-decision` · `.claude/state/`.

Skills in `.claude/skills/` (start with `architecture`); agents in `.claude/agents/`.
