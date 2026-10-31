---
name: provider-pattern
description: How a wt provider is written — the Provider protocol (ensure_server, preflight, create, verify, destroy, template_vars), shared server via server.py, per-branch isolation by deterministic names, registration and tests. Use when adding a provider or changing Postgres, Kafka, Redis or Keycloak behavior in src/wt/providers/.
---

# Provider pattern — wt

Contract: `src/wt/providers/base.py` (`Provider`, `BranchContext`, `Resources`, `Check`) and SPEC §6.
Reference implementation: `src/wt/providers/redis.py` (smallest); Postgres for cloning data.

## Rules
- `__init__(cfg, log)`: **no I/O**; `cfg.shared.<kind>` may be `None` (down after the section was removed).
- `ensure_server`: one shared container for every branch, via `server.up(...)` + a readiness wait.
- `preflight`: refuse the branch before any side effect (e.g. a name too long).
- `create`: idempotent; return the `Resources` names so state can record them.
- `verify`: a **real probe** (query, produce/consume, token) — "container running" isn't enough.
- `destroy`: delete what state recorded **and** the current deterministic name; safe when missing;
  validate a name with a strict regex before deleting anything (see `PREFIX` in redis).
- `template_vars(ctx, kind)`: compose services reach the container by name, `run` services via
  `127.0.0.1:<host port>`.
- Register in `providers/__init__.py` `REGISTRY`. A provider never imports another one.

## Tests
Fake `proc`/`compose` (see `FakeProc` in `tests/test_redis.py`), cover: create twice, verify fail,
destroy on missing/half-created, template vars per kind. Real runs belong to e2e (`tests/integration/`).
