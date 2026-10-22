# Orchestration-Worktrees — Specification

Status: draft v0.1 · 2026-10-18 · Author: Lauro Peres

## 1. Problem

AI coding agents (and humans reviewing PRs) can work on several git branches at the same time.
A typical local dev stack can only run **one** branch at a time: one set of containers, one set of
ports, one database, one Kafka consumer group, one Redis keyspace. Testing a second branch means
tearing down the first, or waiting for a deploy.

## 2. Goal

A CLI that runs a **full, isolated copy of a user-defined stack for every git branch**, side by side
on one machine, each on its own HTTPS subdomain.

It must be **generic**: it ships with no services of its own. Each user describes their stack
(any repos, any languages) in a config file. Stateful dependencies (Postgres, Kafka, Redis, auth)
run as **one shared server each**, isolated per branch by name, so N branches do not cost N servers.

Non-goals (for now): production deploys, Kubernetes, remote/cloud environments, native Windows
(WSL2 is supported), a GUI.

## 3. User experience

```bash
wt init                    # writes a starter worktree.yaml
wt up feat/cart            # git worktree + services + routes + isolated deps
wt ls                      # branches, URLs, status, health
wt logs feat/cart api      # follow one service
wt down feat/cart          # stop and clean everything for that branch
wt doctor                  # check docker, git, ports, certs, hosts resolution
```

After `wt up feat/cart`:

- `https://feat-cart.shop.localhost` → that branch's frontend
- `https://api.feat-cart.shop.localhost` → that branch's API
- its own database `shop__feat_cart`, consumer group `orders__feat_cart`, Redis prefix `feat_cart:`
- `main` keeps running, untouched.

## 4. Configuration (`worktree.yaml`)

```yaml
project: shop
domain: shop.localhost          # *.localhost resolves to 127.0.0.1

repos:                          # one or many repos; each gets a worktree per branch
  api: ../shop-api
  web: ../shop-web

services:
  api:
    repo: api
    compose: docker-compose.yml # container service (compose file inside the repo)
    port: 8000                  # internal port; routed by Traefik
    subdomain: api
    env:
      DATABASE_URL: "{{ postgres.url('shop') }}"
      KAFKA_GROUP: "{{ kafka.group('orders') }}"
      REDIS_PREFIX: "{{ redis.prefix }}"
  web:
    repo: web
    run: npm run dev -- --port {{ port }}   # host process
    port: auto                  # derived free port per branch
    subdomain: ""               # root of the branch domain

shared:
  postgres:
    databases: [shop]
    strategy: template          # template | dump-restore
    verify: true                # compare row counts / checksums after clone
  kafka:
    groups: [orders]
  redis:
    prefix: true
  keycloak:                     # optional
    realm: shop
    client: web
```

Templating: values in `{{ }}` are resolved per branch by the providers (section 6).

## 5. Architecture

```
cli (typer) ──> core.Orchestrator
                  ├── config      load + validate worktree.yaml (pydantic)
                  ├── naming      branch -> slug, db names, ports (deterministic)
                  ├── git         worktree add/remove, per repo
                  ├── runtime     docker compose (project name per branch) | host process
                  ├── router      Traefik file provider: one route file per branch
                  ├── providers/  postgres, kafka, redis, keycloak (plugin interface)
                  └── state       ~/.wt/<project>/state.json (what exists, for clean teardown)
```

Principles:

- **Deterministic naming**: every name/port is derived from (project, branch), so the same
  branch always gets the same resources and teardown never guesses.
- **Idempotent commands**: `up` twice is safe; `down` on a half-created branch cleans what exists.
- **Verify, don't assume**: a step is done when it is verified (HTTP healthy, DB clone checked),
  not when the command returned. "Container running" is not "service healthy".
- **Shared servers, isolated names**: one Postgres/Kafka/Redis/Keycloak for all branches.

## 6. Provider interface

```python
class Provider(Protocol):
    name: str
    def ensure_server(self) -> None: ...                 # start shared server if needed
    def create(self, ctx: BranchContext) -> Resources: ... # db, group, prefix...
    def verify(self, ctx: BranchContext) -> Check: ...
    def destroy(self, ctx: BranchContext) -> None: ...
    def template_vars(self, ctx: BranchContext) -> dict: ...  # for {{ }} in config
```

Built-in providers:

| Provider | Isolation | Notes |
|---|---|---|
| postgres | one database per branch | `CREATE DATABASE … TEMPLATE` from main; fallback dump/restore; verify after clone |
| kafka | one consumer group per branch (+ optional topic prefix) | same group across branches = messages silently split |
| redis | key prefix per branch (or DB index) | avoids one branch serving another's cache |
| keycloak | redirect URIs per branch | optional; SSO across subdomains |

Third-party providers can be added later via entry points (e.g. MySQL, RabbitMQ).

## 7. Routing and TLS

- Traefik runs once (shared), file provider watching `~/.wt/traefik/dynamic/`.
- One route file per branch: written on `up`, deleted on `down`.
- TLS for `*.<domain>` via a local CA (mkcert), installed by `wt doctor --fix`.
- `*.localhost` resolves to loopback in modern browsers; `wt doctor` checks the OS resolver too.

## 8. Example stack (`examples/shop/`)

A small, public, fictional stack so anyone can try the tool in one command:

- `api`: Python (FastAPI) + Postgres + Redis cache
- `worker`: consumes `orders` from Kafka
- `web`: minimal frontend
- optional Keycloak login

Used for the README demo (two branches side by side) and for the benchmarks.

## 9. Quality

- Unit tests for config, naming, templating, providers (mocked).
- Integration tests with Docker (marked, run in CI on Linux).
- `ruff` + `mypy`; GitHub Actions CI.
- Benchmarks measured on the author's machine with `examples/shop` (time to `up`, DB clone time,
  disk used per extra branch). Never reuse numbers from anywhere else.

## 10. Roadmap

| Version | Scope |
|---|---|
| v0.1 | CLI skeleton, config, naming, git worktrees, docker compose runtime, Traefik routes + TLS, `up/down/ls/doctor` |
| v0.2 | Postgres provider (template clone + verify), host-process runtime, `examples/shop` (api + web) |
| v0.3 | Kafka + Redis providers, worker in the example, README with diagram and GIF, benchmarks |
| v0.4 | Keycloak provider (SSO across branch subdomains) |
| later | shared dependency caches, plugin entry points, TUI, native Windows |

## 11. Clean-room rules

This project is written from scratch. It must not contain code, text, names, domains, hosts,
numbers or docs from any employer. Only public knowledge and the author's own new work.

## 12. Open questions

- CLI name: `wt` (decided 2026-10-19).
- Package manager: `uv` (adopted in v0.1, 2026-10-19).
- Minimum Python: 3.11 (adopted in v0.1, 2026-10-19).

## 13. v0.1 implementation notes

Decisions taken while building v0.1. Change them here first if they need to change.

- **Layout.** Worktrees live in `~/.wt/<project>/worktrees/<slug>/<repo>`; compose overrides in
  `~/.wt/<project>/branches/<slug>/`; Traefik files in `~/.wt/traefik/`. `WT_HOME` moves it all.
- **Branches.** If the branch is already checked out somewhere (e.g. `main` in the main repo),
  wt uses that checkout and never removes it. A missing branch is created from `origin/<branch>`
  if present, otherwise from the repo's current `HEAD`. `down` never deletes branches or commits.
- **Dirty worktrees.** `down` refuses (and touches nothing) if a worktree has uncommitted or
  untracked files; `--force` overrides.
- **Compose isolation.** One compose project per (branch, repo): `wt-<project>-<slug>-<repo>`.
  An override file drops published `ports` and `container_name` from every service (`!reset`,
  Compose 2.24+), adds `env`, and puts routed services on the shared `wt` network with a
  per-branch alias. One compose file per repo in v0.1. `down` also removes the branch's volumes.
- **Routing.** One Traefik container (`wt-traefik`) bound to `127.0.0.1`, static config as CLI
  flags, file provider on `~/.wt/traefik/dynamic/`. Route files are written atomically.
  Ports: `WT_HTTP_PORT` / `WT_HTTPS_PORT` (default 80/443). Image: `WT_TRAEFIK_IMAGE`.
- **TLS.** One mkcert certificate per branch for `<slug>.<domain>` and `*.<slug>.<domain>` (a
  wildcard on `<domain>` cannot cover `api.<slug>.<domain>`). Without mkcert, Traefik's default
  self-signed certificate is used and `up` warns.
- **Health.** A routed service is healthy when `GET <health>` through Traefik returns 2xx, 3xx,
  401 or 403 (404 is not healthy: Traefik answers 404 when a route is missing). The probe
  connects to `127.0.0.1` with the real host as SNI, so it does not depend on the OS resolver.
  Unrouted services are healthy when their containers run and are not `unhealthy`.
- **Not in v0.1.** `run:` services are accepted by the config but skipped (v0.2). `shared:` is
  validated but ignored, and provider template names (`postgres`, ...) fail with a clear error.
- **Concurrency.** `flock` locks per branch, per project state file, and for Traefik, so several
  `wt up` can run at once for different branches.
- **Tests.** `WT_INSTANCE` renames the shared network and Traefik container so integration tests
  never touch a real setup.
- **Slugs.** Different branches can map to the same slug (`feat/x`, `feat-x`). `up` and `down`
  refuse a slug that another branch owns in state, and name that branch. URLs stay readable; no
  hash is added.
- **Platforms.** Locks use `fcntl.flock`, so native Windows is not supported; use WSL2.
- **State.** `state.json` has a `version` (1). A wt that finds a newer version refuses to touch
  the file. A corrupt file is reported with its path.
