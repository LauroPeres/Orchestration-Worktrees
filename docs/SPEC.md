# Orchestration-Worktrees — Specification

Status: draft v0.2 · 2026-10-22 · Author: Lauro Peres

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
| v0.1 ✔ | CLI skeleton, config, naming, git worktrees, docker compose runtime, Traefik routes + TLS, `up/down/ls/doctor` |
| v0.2 ✔ | Postgres provider (template clone + verify), host-process runtime, `examples/shop` (api + web) |
| v0.3 ✔ | Bug fixes (stale Traefik mounts, live `ls` status), `wt tui`, user catalog (`uses:`), `examples/notes` |
| v0.4 | Kafka + Redis providers, worker in the example, README with diagram and GIF, benchmarks |
| v0.5 | Keycloak provider (SSO across branch subdomains) |
| later | shared dependency caches, plugin entry points, native Windows |

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
  wildcard on `<domain>` cannot cover `api.<slug>.<domain>`). A `<key>.hosts` file next to it
  lists the names it was made for; `up` makes a new one when they differ (e.g. after a `domain`
  change) or the file is missing (D17). Without mkcert, Traefik's default
  self-signed certificate is used and `up` warns.
- **Health.** A routed service is healthy when `GET <health>` through Traefik returns 2xx, 3xx,
  401 or 403 (404 is not healthy: Traefik answers 404 when a route is missing). The probe
  connects to `127.0.0.1` with the real host as SNI, so it does not depend on the OS resolver.
  Unrouted services are healthy when their containers run and are not `unhealthy`.
- **Not in v0.1.** `run:` services are accepted by the config but skipped, and `shared:` is
  validated but ignored. Both are built in v0.2, see §14.
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
- **Failed up.** If `up` stops on an error, the branch is marked `failed` in state and shown by
  `wt ls`; `wt down` cleans it.
- **Untrusted CA.** If mkcert is installed but its CA is not, `up` warns and points to
  `wt doctor --fix`.
- **Unknown branch.** `down` of a branch wt does not know says so and exits 0.
- **Repo lock.** `git worktree add` runs under a per-repo lock, so concurrent `up` of
  different branches in the same repo do not race on git's own files.

## 14. v0.2 implementation notes

Decisions taken while planning v0.2. Change them here first if they need to change.

**Providers.** Package `src/wt/providers/`: `base.py` (the `Provider` Protocol, `BranchContext`,
`Resources`, `Check`), `postgres.py`, and `__init__.py` with a plain dict registry of built-in
providers. No entry points yet (§10 "later"). `BranchContext` carries the names, the branch, the
base branch and the state handle. `Provider.template_vars` takes a second argument,
`kind: "compose" | "run"`, so a provider can give a different address per service kind. Every
other method keeps the §6 signature.

**Active providers.** Only `postgres` runs. `kafka`, `redis` and `keycloak` under `shared:` stay
as in v0.1: validated, ignored with a note in `up`, and their template names (`{{ kafka... }}`)
fail with the "arrives in v0.4/v0.5" message.

**Provider hooks.** Per provider, `up` runs `ensure_server()` → `create(ctx)` → `verify(ctx)`
before any service starts. `down` runs `destroy(ctx)` for each provider recorded for the branch,
after compose and routes are removed. A failed `verify` fails `up` with its detail and marks the
branch `failed`. Warnings from `verify` are logged.

**Half-created branches.** The orchestrator writes the provider name into `providers` in state
before `create`, and the resource names after it. `destroy` drops by deterministic names with
`IF EXISTS`, so it is safe whatever got created.

**State.** `state.json` stays `version: 1`. New fields are optional with defaults, so v0.1 files
still load: `BranchState.providers: dict[str, list[str]]` (provider → resource names),
`BranchState.processes: dict[str, ProcessState]`, `BranchState.ports: dict[str, int]` (service →
reserved port) and `BranchState.provider_base` (see Destroy).

**Render order.** Rendering stays first in `up`, before anything is created, so a template typo
fails early. This works because provider template vars are deterministic (names, password file,
ports).

**Network.** When any provider is configured, every compose service listed in `services:` joins
the shared `wt` network (routed or not), so it can reach `wt-postgres`.

**Postgres server.** One shared container `wt-postgres`, renamed by `WT_INSTANCE` like Traefik.
Image `WT_POSTGRES_IMAGE` (default `postgres:17-alpine`). Named volume `wt-postgres-data`, with the
same instance suffix. It runs on the shared `wt` network, published on
`127.0.0.1:${WT_POSTGRES_PORT:-55432}` (not 5432, to avoid clashing with a local Postgres). Superuser
`wt`.

**Postgres password.** Generated once with `secrets.token_urlsafe` and stored at
`$WT_HOME/postgres/password` with mode `0600`. The file is created the first time something needs
it (render or `ensure_server`). It is never printed by `ls`; `doctor` only says whether it exists.
Compose override files in `~/.wt/<project>/branches/<slug>/` can hold the rendered env, which may
contain the password, so they are written with mode `0600`.

**Postgres lock.** `up` and `down` hold `file_lock("postgres")` (same name as `doctor`) around `ensure_server`, `create` (incl. the clone) and `destroy`; order: branch, ports, postgres, state. `password()` is create-once across processes (dedicated `postgres-password` flock).

**Postgres server start.** `ensure_server()` starts the container if needed, the same way Traefik is
started. It waits up to 60 s for `pg_isready` inside the container. Then it re-enables
`datallowconn` on the wt-managed databases (`<db>__*` for each configured `<db>`, `_` escaped in
`LIKE`) that have it off. A user's own database such as `a__b` is never touched. `doctor --fix` runs the same
repair.

**Postgres access.** wt talks to Postgres with `docker exec <container> psql -v ON_ERROR_STOP=1 -U wt ...`
through `wt.proc`. No new Python dependency. Identifiers are built only by `naming` (`[a-z0-9_]`),
checked with the regex `^[a-z0-9_]{1,63}$`, then double-quoted in SQL. Raw user input is never
interpolated.

**Postgres config.** `shared.postgres.base_branch` (default `"main"`) is the branch whose databases
are the clone source. Keys `databases`, `strategy` and `verify` work as in §4.

**Database names.** `<db>__<ident>` passed through `naming.fit(..., 63)`, e.g. `shop__feat_cart`.
The base branch's databases are created empty if missing; the app's own migrations and seed fill
them.

**Create.** For a branch other than `base_branch`: keep `<db>__<ident>` if it exists (never
re-clone). Otherwise clone it from the base database. If the base database does not exist yet,
create it empty and warn `run wt up <base_branch> first to clone real data`.

**Template strategy.** Steps:
1. `ALTER DATABASE src WITH ALLOW_CONNECTIONS false`
2. terminate other backends on `src` (`pg_terminate_backend`)
3. `CREATE DATABASE dst TEMPLATE src`, retried 3 times, 1 s apart, on "being accessed by other users"
4. in `finally`: `ALTER DATABASE src WITH ALLOW_CONNECTIONS true`

The base branch's app sees its connections dropped for about a second and must reconnect.

**Dump-restore strategy.** `pg_dump -Fc src | pg_restore -d dst` inside the container. The base
branch is not blocked.

**Verify.** Compares the list of user tables and the exact `count(*)` per table between source and
clone, only for databases cloned in this run. `template`: a mismatch drops the clone and fails
`up`. `dump-restore`: the table list must match; count differences only warn (the source is live).
`shortcut: exact counts are slow on big tables, move to sampled checksums when someone hits it`

**Destroy.** `down <branch>` runs `DROP DATABASE IF EXISTS ... WITH (FORCE)` for that branch's
databases. At `create` time the provider records the base branch in state
(`BranchState.provider_base`, optional field, state stays version 1). `destroy` trusts that record
over the current config, so changing `base_branch` or removing `shared.postgres` after `up` cannot
make it drop the clone source. It never drops the databases of the recorded base branch; when
neither state nor config says which branch is the base, it drops nothing. Either way it logs that
it keeps them. wt has no command to drop the base databases; the README documents the manual
`dropdb` (D19, so no new flag or command in v0.2). If reopening the source after a template clone fails, the original clone error is
still the one reported.

**Postgres template vars.** `postgres.url('shop')` and `postgres.name('shop')`. Compose services get
`postgresql://wt:<pw>@<container>:5432/<db>`. Host processes get
`postgresql://wt:<pw>@127.0.0.1:<WT_POSTGRES_PORT>/<db>`. An unknown database raises `WtError`.

**Run services.** The command is rendered with templates and run with `sh -c` in the service's
worktree directory. Environment = current env + rendered `env`. Started with
`start_new_session=True`. Output is appended to `~/.wt/<project>/branches/<slug>/logs/<service>.log`.
`wt logs` runs `tail -n <tail> [-f] <log>`, so wt has no follow code of its own.

**Ports.** `port: auto` → `naming.auto_port(project, branch, service)`. If that port is taken, probe
upward (max 100 tries), skipping ports that state records for any other (branch, service). The
chosen port is stored in `BranchState.ports` before the process starts, under a per-project
ports lock held while picking and rendering, so two concurrent `up` never pick the same port, and
it is kept for the branch. An explicit integer port is used as is and
fails clearly if taken (unless it is our own live process). Ports are resolved before rendering.
The context vars `port` and `services.<name>.port` show the resolved port.

**Process state.** `ProcessState(pid, pgid, port, command, started_at, log, env_hash)`. Alive = `os.killpg(pgid, 0)`
succeeds and, while the leader pid exists, it is still in that group and started no later than
`started_at` (Linux `/proc`, else `ps -o lstart=`); otherwise it is treated as dead and never signalled.
`shortcut: a start time that cannot be read is trusted`

**Process stop.** SIGTERM to the process group, wait up to 10 s, then SIGKILL. `up` on an
already-running process is a no-op while its rendered command, port and the sha256 of its
rendered `env` (`env_hash`) match what state recorded. If any of them changed, `up` stops the old
process and starts the new one (D16). A state written before `env_hash` existed counts as unchanged
env.

**Routing to host processes.** The Traefik container gets
`extra_hosts: [host.docker.internal:host-gateway]` (the same as `--add-host`), and the route of a `run:` service points at
`http://host.docker.internal:<port>`. Verified on WSL2 + Docker Desktop with the process bound to
`0.0.0.0` or `127.0.0.1`; macOS Docker Desktop resolves the name natively. On Docker Engine (native
Linux, CI) the name only exists through that `--add-host`, and `host-gateway` is the bridge
gateway, so the process must listen on `0.0.0.0`. Requirement for users: bind `0.0.0.0` in `run:`
commands. `shortcut: bind address is not verified, only documented`.

**Run health.** Routed: same Traefik probe as compose services. Unrouted with a port: TCP connect
to the port. No port: the process is alive.

**Up order.** resolve ports and render env → shared Traefik → worktrees → providers (ensure,
create, verify) → TLS → compose services → host processes → routes → health → URL table.

**Down order.** host processes → compose (with volumes) → routes → certs → providers destroy →
worktrees. Each step skips what state says was never created. Without a state entry, a folder
at the worktree path is removed only if git lists it as a worktree of that repo; any other folder
is left in place with a warning, also with `--force` (D18).

**ls.** A `kind` column (compose or run) on each service row, and the Postgres databases of each
branch. Run services get health and URL like compose services. If the docker CLI fails, `compose.ps`
raises `WtError("docker is not reachable ...")`, and `ls` shows `docker unavailable` instead of `0/0`.
Layout, so URLs stay whole at 80 columns: columns `BRANCH STATUS UP SERVICE KIND URL HEALTH`
(`UP` is `<running>/<total>`, e.g. `2/2`, counting every service of the branch: compose containers plus live `run:` processes; only `UP` and `HEALTH` may wrap, a URL never does); databases are listed in a `databases:` section after the table.

**D13 (width, degraded).** When stdout is not a terminal (piped, redirected, CI), the console does not limit width: no wrapping and no cutting in `ls` or `up`, like `docker ps` piped. A terminal keeps the layout above. STATUS is live (v0.3, see §15 "ls status"): when docker is reachable and the stored status is `up` or `degraded`, STATUS is `up` if `UP` equals its total and no health probe failed, else `degraded`; without probing only the `UP` rule applies. `creating`, `removing` and `failed` are shown as stored.

**logs.** For a `run:` service, a missing log file raises `WtError("no log yet for <svc> on <branch>")`.

**doctor.** Postgres checks only when `shared.postgres` is configured: the container runs; the
password file exists with mode `0600` (its content is never printed); no wt database has
`datallowconn = false`. For `run:` services in state with a dead process, the recorded port is
free. `--fix` starts the server with `ensure_server()`, which also repairs `datallowconn`.

**Example shop web.** The web page fetches items in the browser from `API_URL`. The API allows CORS
from `WEB_ORIGIN`. The `feat/cart` change lives in `examples/shop/feat-cart/` as replacement files,
which `bootstrap.sh` copies onto that branch and commits.

**Base alias.** A non-base branch whose database names equal the base branch's (`Main`, `main_`, `release-1.0` vs `release/1.0`) is refused by `up` in its pre-flight (a provider `preflight` hook, before any state, worktree or container), and `down` never drops those databases.

**D15 (run: quoting).** In a `run:` command every `{{ ... }}` value is inserted shell-quoted
(`shlex.quote`), because the command runs with `sh -c` and git allows `;`, `$()`, backticks and `&`
in branch names. Safe values such as ports and URLs are unchanged. Only `run:` is quoted: `env`
values, compose files and URLs are rendered as they are. Users must not add quotes around template
values in `run:`.

## 15. v0.3 implementation notes

Decisions taken while planning v0.3. Change them here first if they need to change.

**Catalog.** A user catalog (`~/.wt/catalog.yaml` by default) lists the repos and services of
all projects. A worktree.yaml picks services from it with `uses:`. Format:

```yaml
repos:                         # name -> path; ~ expanded, relative = relative to this file
  notes-back: ~/code/notes-back
  notes-front: ~/code/notes-front
services:
  back:
    repo: notes-back
    compose: docker-compose.yml
    port: 8000
    subdomain: api
    health: /healthz
    infra:                     # what this service needs; merged into `shared`
      postgres: {databases: [notes]}
    env:
      DATABASE_URL: "{{ postgres.url('notes') }}"
  front:
    repo: notes-front
    run: python3 server.py --port {{ port }}
    port: auto
    subdomain: ""
    consumes:                  # how the code consumes other services
      back: API_URL            # the front reads the back's branch URL from $API_URL
```

worktree.yaml with a catalog:

```yaml
project: notes
domain: notes.localhost
catalog: ./catalog.yaml        # optional
uses: [front]                  # back comes in because front consumes it
```

Rules:
- A worktree.yaml has exactly one of `services:` (v0.2 style, unchanged) or `uses:`.
- `uses` + every service reachable through `consumes` = the stack; their repos come from the
  catalog; `infra` of all of them is merged into `shared` (union of databases; two different
  `strategy`/`base_branch` values are an error); a local `shared:` is an error with `uses`.
- `consumes: {dep: VAR}` adds `VAR: "{{ services.dep.url }}"` to the consumer's env (error if
  `dep` has no route, or if `env` already sets VAR).
- The env of a consumed service may use `{{ services.<consumer>.url }}` (for CORS).
- Start order: infra (Traefik, providers) first, then services in dependency order: a
  consumed service is started and healthy before its consumers (compose groups and host
  processes sorted topologically; a cycle is an error).
- The result is a normal `Config`, so the rest of wt does not change.
- `wt catalog`: table of the catalog (service, repo, kind, infra, consumes, consumed by);
  `wt catalog --check` validates it.

- Catalog services accept every `Service` key, plus `infra` (keys of `shared`) and `consumes`.
  Env var names in `consumes` match `^[A-Z_][A-Z0-9_]*$`.
- Repo paths are made absolute when the catalog is resolved.
- A local `repos:` together with `uses` is an error (repos come from the catalog). A worktree.yaml
  with neither `services:` nor `uses:` is an error.
- `catalog:` is read by `load_config` and is not part of the resolved `Config`.
- Infra merge conflicts are checked only on explicitly set fields; list fields (`databases`,
  `groups`) are unioned.
- Service order follows discovery: the `uses` entries, then their dependencies.
- `env` and `needs` set on a catalog service are kept; the `consumes` variable is added to `env`
  and the dependency to `needs`.
- A cycle through `consumes` is an error ("cycle through catalog consumes: a -> b -> a").
- **Needs (decision a).** `Service.needs: list[str]` (default `[]`) is filled from `consumes`.
  The v0.2 `services:` style may also set `needs`; names must be known and there must be no cycle.

**Start order.** `up` builds one list of steps: a compose group per repo, and one step per
`run:` service. Edges come from `needs`. Without `needs` the order is the v0.2 order: compose
groups, then run services.

**Healthy before consumers (decision b).** A step starts only after the steps it needs are
healthy, up to `timeout`:
- compose group: healthy when `docker compose up --wait` succeeded for it;
- `run:` service with a `port`: healthy when the port accepts TCP; without a port: healthy when
  the process is alive.

wt waits for a `run:` service to be healthy only when another step needs it; a service nobody
needs is not waited on (v0.2 behavior). A needed step that is not healthy in time fails `up`
with "service 'x' is not healthy: ..." and its consumers are not started.

Routes are still written once, after all services. The HTTP probe through Traefik stays in the
final verify step. A cycle across steps (e.g. through groups) is a WtError before anything starts.

**TUI (`wt tui`).** Module `src/wt/tui.py` on Textual (new dependency, `textual>=0.80`); command
`wt tui` in `cli.py`.
- Layout: left, a list of the branches of the current project with their status (from `ls()`);
  right, the services of the selected branch (SERVICE KIND URL HEALTH, like `wt ls`); bottom, a
  log pane.
- Keys: arrows select; `u` up the selected branch; `n` up a new branch (input box); `d` down the
  selected branch (confirm; `--force` only after a second confirm); `l` tail the selected service's
  log in the log pane; `r` refresh; `q` quit.
- Auto refresh every 2 s. `ls()` runs in a worker thread, so the UI never blocks.
- `up` and `down` run in a worker thread, one at a time; keys are ignored with a notice while busy.
  Errors are shown in the log pane; the app stays open.
- **Output (decision c).** Compose and Traefik output reach the TUI through
  `Orchestrator(..., out: Callable[[str], None] | None = None)`, passed to `proc.run(..., on_line=...)`.
  `None` means the terminal, as today.
- Tests: Textual `App.run_test()` pilot with a fake orchestrator, run with `asyncio.run(...)`
  (no pytest-asyncio, no Docker).

**`worktree` command (opens the TUI from any folder).** A second console script, `worktree`
(`wt.cli:worktree_main`, same `--config`/`WT_CONFIG` option), shares its entry with `wt tui`.
- Inside a project (a `worktree.yaml` found from the cwd, or `--config`): the TUI of that project,
  as above.
- Outside a project: the left pane is a tree, project header then its branches. Projects come from
  `$WT_HOME/<project>/state.json` (`src/wt/projects.py`), sorted by name, re-read on every refresh.
  A project with no branches, or whose state file is unreadable, is skipped. Its `config_path`
  gives its config; each project gets its own `Orchestrator`, built lazily on first use.
- A project whose config file is gone (or never recorded) is shown greyed with a note
  (`config not found: <path>`) and no branches; `u`, `n`, `d` on it only print the note.
  A project whose `ls()` fails shows the error on its header; the others keep working.
- `u`, `d`, `l` act on the selected branch's project; `n` on the project of the selected header or
  branch. The services pane shows the selected branch only.

**Stale Traefik mounts.** After a Docker Desktop restart, Traefik can see empty
`/etc/traefik/dynamic` and `/etc/traefik/certs`, so every route returns 404.
- `doctor` check `traefik mounts`: compares the file names in the host `dynamic/` and `certs/`
  folders with `docker exec <container> ls -1 /etc/traefik/dynamic` and
  `ls -1 /etc/traefik/certs`. Any missing name → `fail` with the hint `wt doctor --fix`.
- `doctor --fix` recreates the container (`docker compose up -d --force-recreate`), waits until
  it is running, and checks again. Not `docker restart`: a restart reuses the old mount source,
  which fails when the host folder was deleted and recreated. A failed recreate is reported as
  `fail` for this check; doctor goes on.
- `up`, after it writes its route file, checks that the file name is listed inside the container.
  If not, it recreates Traefik once; if the name is still missing, `up` fails with a WtError.

**ls status.** The `degraded` rule in D13 (§14) is replaced: when docker is reachable and the
stored status is `up` or `degraded`, STATUS is `up` if `UP` equals its total and no health probe
failed, else `degraded`. `creating`, `removing` and `failed` are shown as stored.

## 16. v0.3: worktree lifecycle and the table TUI

Agreed with the user after the first TUI. Replaces the TUI layout and keys of §15; the
`worktree` command and its project discovery stay.

**Lifecycle.** Besides `up` and `down`, a worktree (branch) can be added, stopped and removed.

| Command | What it does | Keeps |
|---|---|---|
| `wt add <branch>` | Pre-flight (as `up`), git worktrees for every repo, reserved ports; state entry with status `stopped`. No containers, databases, routes or certs. Already in state: no-op with a note. | — |
| `wt up <branch>` | As today. On a `stopped` branch it reuses its worktrees, ports and databases. | — |
| `wt stop <branch>` | Stops the host processes and runs `docker compose down` (no `-v`) for each compose project of the branch; removes its route file. Status `stopped`. Not in state: error. | worktrees, volumes, databases, ports, cert |
| `wt down <branch>` | As today (removes everything wt created, keeps the git branch). | git branch |
| `wt remove <branch> [--force] [--yes]` | Hard delete: everything `down` does, then `git branch -D <branch>` in every repo. Asks for confirmation unless `--yes`. | nothing |

- `remove` never deletes a protected branch: the Postgres `base_branch` (default `main`) and the
  branch checked out in each repo's main folder. It never deletes a branch that is checked out in
  a folder wt did not create. In those cases it does `down` and keeps the branch, with a note.
- `--force` works as in `down` (dirty worktrees are removed).
- State: status gains `stopped`. `ls` shows `stopped` as stored (like `creating`).
- `BranchStatus` (the `ls()` rows) gains `ports: list[int]`, the reserved host ports of the branch
  (`BranchState.ports`), sorted.

**TUI (table).** `wt tui` and `worktree` open the same compact, monochrome screen (no colors):
- One table, one row per worktree, columns `NAME`, `STATUS`, `PORT`. `PORT` is the reserved host
  ports joined with `, `, or `-`. Outside a project (all projects) `NAME` is `<project>/<branch>`.
- Below the table, one line with the actions: `u up  d down  a add  r remove  q quit`, and one
  message line with the last log line or error of the running action.
- `u` = `up` of the selected row. `d` = `stop` (services down, worktree kept). `a` = an input box
  for the new worktree name, then `add` in the current project (outside a project: the project of
  the selected row). `r` = a confirm box (`remove <branch>: deletes its containers, volumes,
  databases, worktrees and the git branch; unpushed commits are lost. Remove? y/N`), then `remove`
  with `--force`. Arrows select, `q` quits.
- One action at a time, in a worker thread; keys pressed while busy show a note on the message
  line. The table refreshes every 2 s and after each action.
- Removed for now: services pane, log pane, `l` (logs), `n`, the refresh key, colors.
- Later (not now): the front of a worktree gets a default URL built from the repo and worktree
  name.
