---
name: checks
description: How to run and read wt's checks — ruff lint (with complexity limits), ruff format, mypy strict, import-linter layer contracts, pytest (unit and docker) — and what each failure means. Use when running checks or interpreting a failure here.
---

# Checks — wt

Run all, in this order, even if one fails (CI runs the same):
| Step | Command | A failure means |
|---|---|---|
| Lint | `uv run ruff check` | rule + `path:line`; `C901`/`PLR09xx` = too complex / too many args, branches or statements → split it |
| Format | `uv run ruff format --check` | run `uv run ruff format <file>` (the edit hook usually does it) |
| Types | `uv run mypy` | strict mode: add the type, never `Any`/`type: ignore` to silence it |
| Layers | `uv run lint-imports` | a `BROKEN` contract with the import chain → move the code to the right layer |
| Tests | `uv run pytest -q` | test id + assertion; one file `uv run pytest -q <path>`, one test `-k <name>` |
| Real runs | `uv run pytest -m docker` | only when asked or for e2e; needs `docker info` |

- Missing `.venv` or deps: `uv sync`.
- Never silence a check: no `# noqa`, `type: ignore`, `ignore_imports`, skipped tests or raised limits.
- Unit tests never need Docker (`-m 'not docker'` is the default). The test suite takes ~50 s.
