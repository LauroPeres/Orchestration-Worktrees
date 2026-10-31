---
name: e2e-wt
description: How wt is run for real by the e2e agent — Docker prerequisites, the examples/shop and examples/notes stacks, pytest -m docker, the adversarial cases always tried, and cleanup. Use when running end-to-end checks, writing integration tests, or reproducing a bug against Docker.
---

# E2E — wt

## Prerequisites
`docker info` must succeed (else report `blocked`). Tests set `WT_HOME` to a temp dir; never use
the real `~/.wt` or the user's projects.

## Run
- `uv run pytest -m docker` — all integration tests (`tests/integration/`, slow).
- One stack: `uv run pytest -m docker tests/integration/test_example_shop.py`.
- Stacks: `examples/shop/` (api + worker + web, Postgres, Kafka, Redis, Keycloak) and `examples/notes/`;
  each has a `bootstrap.sh` that builds its repos, and a `worktree.yaml`.

## Adversarial cases (always try the ones the change touches)
- Branch names: `feat/Cart_v2`, very long names, names that slug to the same value, only symbols.
- `up` twice; `up` after `stop`; `down` twice; `down` on a branch whose `up` failed halfway.
- Kill a run mid-`up` (Ctrl-C), then `up` and `down` again: no leftovers (`docker ps -a`, databases, keys, routes).
- A service that never gets healthy (timeout) → status `degraded`, clear message.
- Two branches side by side: data isolated (a row/key/topic in one is not visible in the other).

## Cleanup
Leave nothing running: `down` every branch you created; check `docker ps -a` and `docker compose ls`
for `wt`-named leftovers and list them in the report (don't delete things you didn't create).
