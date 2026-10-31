---
name: config-and-naming
description: How wt's pure model is written — the worktree.yaml pydantic schema (Strict models, validators, error formatting), the catalog (uses:), deterministic names and ports, sandboxed Jinja templating with shell quoting, and paths. Use when changing src/wt/{config,catalog,naming,templating,paths,errors}.py.
---

# Config and naming — wt model

- **Schema:** models subclass `Strict` (`extra="forbid"`); every field has a
  `Field(..., description=...)`; cross-field rules in one `@model_validator(mode="after")` that raises
  `ValueError` (turned into readable lines by `_format_errors`). New keys keep old files valid
  (a default) — and behavior keys need a `spec-decision` first (SPEC §4).
- **Names:** everything is derived from (project, branch) in `naming` — `slugify`, `fit` (hash
  suffix keeps long names unique), `host_port(worktree_index, service_index)`, `Names`. Limits are
  named constants (`MAX_SLUG`, `MAX_LABEL`, `PORT_STRIDE`). Never a random or time-based name.
- **Templates:** `templating.render` uses a sandboxed Jinja env with `StrictUndefined`; values in
  `run:` commands are shell-quoted (`quote=True`). Errors name where they happened and what's available.
- **Errors:** `WtError(message)` — the message is for the user: what's wrong + what to do.

## Tests
Pure tests, no fixtures needed (`tests/test_naming.py`): table-style cases, edge cases (empty,
too long, unicode), and the exact error text with `pytest.raises(WtError, match=...)`.
