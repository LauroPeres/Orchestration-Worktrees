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
wt init                    # writes a starter catalog.yaml (changed by §21)
wt up feat/cart            # git worktree + services + routes + isolated deps
wt ls                      # branches, URLs, status, health
wt logs feat/cart api      # follow one service
wt down feat/cart          # stop and clean everything for that branch
wt doctor                  # check docker, git, ports, certs, hosts resolution
```

After `wt up feat/cart`:

- `https://feat-cart.shop.localhost` → that branch's frontend
- its own database `shop__feat_cart`, consumer group `orders-shop.feat-cart`, Redis prefix
  `shop:feat-cart:`
- `main` keeps running, untouched.

## 4. Configuration (`worktree.yaml`; replaced by the global `catalog.yaml` in §21)

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
| redis | key prefix per branch | avoids one branch serving another's cache |
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
| v0.3 ✔ | Bug fixes (stale Traefik mounts, live `ls` status), `wt tui`, user catalog (`uses:`), `examples/notes`, services view, selection, deterministic ports (§17) |
| v0.4 ✔ (§18) | Kafka + Redis providers, worker in the example, README with diagram (no GIF), benchmarks |
| v0.5 ✔ (§19) | Keycloak provider (SSO across branch subdomains) |
| v0.5.1 ✔ (§20) | Stabilise (stale-port rule, destroy errors, one deadline, published ports, duplicate hosts), dependencies run together, project-scoped names on the shared servers |
| v0.6.0 (§21) | One global `catalog.yaml` (`$WT_HOME`) replaces the per-repo `worktree.yaml`; `wt add`/`up` from any folder with a chosen set of services; `wt migrate`; `--json` output |
| v0.7 | plugin entry points, shared provider helpers, `wt init --with`, TUI log pane |
| v0.8 | shared dependency caches |
| v0.9 | native Windows (spike first) |

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
  Changed by §17: compose services with a `port` keep their published port, bound to `127.0.0.1`.
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

**Active providers.** Only `postgres` runs (changed by §18 and §19: `redis` and `kafka` run from v0.4, `keycloak` from v0.5).
`kafka`, `redis` and `keycloak` under `shared:` stay
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
started. The start and the `pg_isready` wait inside the container share one 60 s deadline (§18
"One deadline"). Then it re-enables
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
Changed by §17: `port: auto` no longer probes; ports come from the deterministic formula.

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

**Down failures.** When a provider step of `down` fails (its server cannot be started or reached,
a login fails, a command errors, or a local file or lock operation fails with an OS error),
`down` stops there: the branch stays `removing`, its state
record is not removed, and the command exits non-zero with the message
``<provider>: could not clean branch <branch> (<short reason>). Try `wt doctor --fix`, then
`wt down <branch>` again.`` Running `wt down <branch>` again after the fix finishes the job.
The `<short reason>` is one line of at most about 120 characters: for a failed command the last
non-empty line of its output (where Docker and kcadm put the cause), never the command line; for
any other error its first line. When the line is longer, the START and the END are kept with `...` between them (the first 50
and the last 67 characters): Docker puts the cause, for example `address already in use`, at the
end, while path errors such as `not a git repository: <long path>` put it at the start. `down` never continues past a failed provider step, because it
would then delete the record and orphan the resource.

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

Changed by §17: `uses` is the exact stack (no closure through `consumes`), and the service order is the selection order.
Changed back by §20: a service that a selected service consumes runs with it again.

Rules:
- A worktree.yaml has exactly one of `services:` (v0.2 style, unchanged) or `uses:`.
- `uses` + every service reachable through `consumes` = the stack; their repos come from the
  catalog; `infra` of all of them is merged into `shared` (union of databases; two different
  `strategy`/`base_branch` values are an error); a local `shared:` is an error with `uses`.
- `consumes: {dep: VAR}` adds `VAR: "{{ services['dep'].url }}"` to the consumer's env (the
  subscript form, so a hyphenated name such as `music-back` works; error if
  `dep` has no route, or if `env` already sets VAR).
- The env of a consumed service may use `{{ services.<consumer>.url }}` (for CORS).
- Start order: infra (Traefik, providers) first, then services in dependency order: a
  consumed service is started and healthy before its consumers (compose groups and host
  processes sorted topologically; a cycle is an error).
- The result is a normal `Config`, so the rest of wt does not change.
- `wt catalog`: table of the catalog (service, repo, kind, infra, consumes, consumed by);
  `wt catalog --check` validates it: the schema, then each service alone as a selection (unknown or
  route-less dependency, env that already sets the consumes variable, cycle); every failing service is
  reported by name and the exit code is non-zero if any failed. It does not render the templates.

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
(`wt.cli:worktree_main`, same `--catalog`/`WT_CATALOG` option, changed by §21), shares its entry with `wt tui`.
- With a catalog (§21: `--catalog`, `WT_CATALOG` or `$WT_HOME/catalog.yaml`): the TUI of that project,
  as above.
- Without one, or with `--all`: the left pane is a tree, project header then its branches. Projects come from
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
- `remove` also works on a branch that is no longer in state (for example after `wt down <branch>`,
  which keeps the git branch): it deletes the branch `<branch>` in every repo of the project where
  it exists (the repos of the current selection for a catalog project), with the same protections and
  notes as above, and says `nothing to remove` only when there was neither state nor any such branch.
  A failure in one repo (a missing path, a refused delete) is reported with the repo's name and does
  not stop the others; `remove` then fails once at the end listing them.
- `--force` works as in `down` (dirty worktrees are removed).
- State: status gains `stopped`. `ls` shows `stopped` as stored (like `creating`) and does not probe
  its services: their HEALTH is `-`.
- `BranchStatus` (the `ls()` rows) gains `ports: list[int]`, the reserved host ports of the branch
  (`BranchState.ports`), sorted.

**TUI (table).** `wt tui` and `worktree` open the same compact, monochrome screen (no colors; changed by §17):
- One table, one row per worktree, columns `NAME`, `STATUS`, `PORT`. `PORT` is the reserved host
  ports joined with `, `, or `-`. Only the worktrees wt created are listed: a branch whose repos are all the repos' own checkout
  (`BranchStatus.main_checkout`, e.g. `main`) is hidden. Outside a project the rows of every project are
  listed the same way, by worktree name only.
- The table has a plain border and only takes the height of its rows. Right below it, one line
  with the actions: `u: up  d: down  a: add  r: remove  q: quit`, and one
  message line with the last log line or error of the running action.
- `u` = `up` of the selected row. `d` = `stop` (services down, worktree kept). `a` = an input box
  for the new worktree name, then `add` in the current project (outside a project: the project of
  the selected row, or, when the table is empty, the only usable project; with several, `a` says to run
  `wt tui` inside one). `r` = a confirm box (`remove <branch>: removes what wt created for it (containers,
  volumes, databases, worktrees) and deletes the git branch in every repo of the project where it
  exists; unpushed commits are lost. Remove? y/N`), then `remove` with `--force`. The CLI asks
  the same question before `wt remove` (unless `--yes`). Arrows select, `q` quits.
- One action at a time, in a worker thread; keys pressed while busy show a note on the message
  line. The table refreshes every 2 s and after each action.
- Removed for now: services pane, log pane, `l` (logs), `n`, the refresh key, colors.
- Changed by §17: the PORT column is gone (`NAME`, `STATUS`), and colors are back (§17 item 8).
- Later (not now): the front of a worktree gets a default URL built from the repo and worktree
  name.

## 17. v0.3: services view, per-worktree selection, deterministic ports

Decided during the services-view sprint. Replaces the parts of §15 and §16 named there, and the
port rules of §13 (compose isolation) and §14 (ports).

**Decisions**
- **D-a. Selection.** Stored as `BranchState.uses: list[str] | None`. `None` means the `uses:` of
  worktree.yaml. The configure checkboxes start from this selection, not from live status.
- **D-b. Service index.** Position in the catalog's `services:` (catalog projects, whatever the
  selection) or in `services:` (v0.2 projects). More than 100 services is a WtError at load.
  A worktree index above 554 (the port would pass 65535) is a WtError.
- **D-c. Worktree index is global.** The index is shared by all projects of the user, so two
  worktrees never share ports. It is the smallest index not used by any branch of any project,
  kept in the registry `$WT_HOME/worktree-index.json` (`{index: "<project>/<branch>"}`) under the
  ports lock, and recorded as `BranchState.index`. It is freed on `down` and `remove`, kept on
  `stop`. Registry entries missing from the state files are rebuilt from the state files.
- **D-d. `services.<x>.url` for remote services (limited by §20).** A catalog service that is not in the stack renders
  its `remote:` (or `""` if it has none). Its `host` is `""` and its `port` is None. `needs` lists
  selected dependencies only.
- **D-e. Configure.** Validate and store the selection. Tear down the services that left: their
  processes are killed; a repo's compose project gets `docker compose down` (no `-v`) only when
  none of that repo's services stays selected; routes are rewritten. Worktrees, databases and
  volumes of removed services stay until `down` or `remove`. Then `up(branch)`, unless the
  worktree is `stopped` and nothing was added. An empty selection is a WtError
  "select at least one service".
- **D-f. TUI.** The main action line gains `enter: services`.

**Contract**
1. **Exact `uses` (replaced by §20).** The stack is exactly the selection (`BranchState.uses`, else `uses:`).
   `consumes` no longer pulls services in. For `consumes: {dep: VAR}`: if `dep` is selected,
   `VAR = {{ services.dep.url }}` and `dep` is added to `needs` (as before); if not, `VAR` is the
   `remote:` URL of `dep` (literal). `infra` is merged from the selected services only. Service
   order is the selection order. D-d applies to `services.<x>` in templates.
2. **`remote:` (no longer required, §20).** New CatalogService key `remote: str | None`, which must start with `http://` or
   `https://`. Every service that some catalog service consumes must set it, otherwise the
   catalog is invalid: `service 'back' is consumed by 'front' and needs 'remote:' (the URL used
   when it is not selected)`. `wt catalog` gains a REMOTE column.
3. **Selection in state.** `BranchState.uses: list[str] | None = None` (D-a). `LoadedConfig` gains
   `uses: tuple[str, ...] | None` (None for `services:` projects), `remotes: dict[str, str]`
   (non-selected catalog services, mapped to their remote or ""), `service_order: tuple[str, ...]`,
   `service_index(name) -> int`, and `with_uses(uses: list[str]) -> LoadedConfig`. `with_uses`
   re-resolves from the same worktree.yaml and catalog, and raises WtError on unknown names or
   infra conflicts. The Orchestrator runs every branch operation (`up`, `add`, `stop`, `down`,
   `remove`, `ls` rows, `services`) with the config of that branch's selection.
4. **Ports.** `BranchState.index: int | None`. The worktree index is assigned by D-c when the
   entry is created (or on the next `add` or `up` for older entries), kept by `stop`, and freed
   when the entry is deleted. `naming.host_port(worktree_index, service_index) = PORT_BASE +
   worktree_index * 100 + service_index` with `PORT_BASE = 10000`. It applies to `port: auto` run
   services and to compose services with `port` (the container port). An explicit integer `port`
   on a run service is used as is (§14 Ports rule). All host ports go to `BranchState.ports`.
   There is no probing. A port that another state branch records, or that is not free on the
   host, fails `up` or `add` with `port 10101 for service 'back' (worktree index 1) is in use;
   free it`. The one exception is a port this worktree already records as its own: for a `run:`
   service only while its recorded process is alive (a stale `up` or `degraded` status does not
   count, so a stranger that took the port after the process died is refused); for a compose
   service while the branch status is not `stopped` or `removing` (its container publishes the
   port and there is no host process to check; after an interrupted `up` the status is `creating`
   or `failed` while the containers may already run, and `up` must reuse them). A stranger that
   takes the port of a `failed` branch whose containers are gone is not caught here: `docker compose
   up` fails with Docker's own bind error and the branch stays `failed`.
   `naming.auto_port`, `AUTO_PORT_RANGE` and the probing in `host.pick_port` are removed; this
   replaces §14 "Ports".
5. **Compose publish.** The override keeps `ports: !reset`, but sets it to
   `["127.0.0.1:<host_port>:<port>"]` for compose services with a `port`. This replaces the
   "drops published ports" part of §13 Compose isolation. Traefik routes are unchanged.
6. **Services view rows.** `Orchestrator.services(branch) -> list[RepoRow]`, with
   `RepoRow(name: str, status: Literal["healthy", "unhealthy", "starting", "stopped"],
   port: int | None, url: str | None)`. One row per repo of the branch's selected services, in
   service order. PORT and URL are those of the repo's first routed service (`subdomain` set). If
   there is no routed service, they are those of the first service with a port, and URL is None.
   Status comes from all containers of the repo's compose project and from its run processes.
   Checked in this order: `stopped` if the branch is not in state, its status is `stopped`, or
   nothing is running; `starting` if the branch is `creating`, or a container has health
   `starting` or state `created`/`restarting`; `unhealthy` if any container is not running or has
   health `unhealthy`, a selected run process is dead, or its port refuses a TCP connect (0.3 s);
   `healthy` otherwise. No HTTP probe: the view refreshes every 2 s and must stay fast (one
   `docker compose ps` per compose project).
7. **Configure (`Choice` gains `required_by`, §20).** `Orchestrator.catalog_choices(branch) -> list[Choice] | None`, with
   `Choice(service: str, repo: str, selected: bool)` in catalog order, and None for a `services:`
   project. `Orchestrator.configure(branch, uses: list[str]) -> None` follows D-e. A branch not in
   state is a WtError.
8. **TUI.** The main table has `NAME` and `STATUS` columns (PORT is gone). The action line is
   `enter: services  u: up  d: down  a: add  r: remove  q: quit`. Enter opens the services screen of
   the selected worktree (its project's orchestrator). The screen has a table `NAME STATUS PORT URL`
   (`-` for None), with the same border and height rules, refreshed every 2 s in a worker, and the
   action line `c: configure  esc: back  q: quit`, plus a message line. `c` opens a modal with a
   checkbox list (`<service> (<repo>)`): space toggles, enter confirms, esc cancels. Confirm runs
   `configure` as the one running action. For a `services:` project the message is
   `configure needs a catalog project (uses: in worktree.yaml)`. Colors: the initials of the action
   line are green, PORT values are purple, STATUS `healthy` and `up` are green, everything else is
   plain. This replaces "no colors" in §16.
9. **`examples/notes`.** `uses: [front, back]`. `back` gets `remote: https://api.staging.notes.example`.
   (§20: `uses: [front]` and no `remote:`.)

## 18. v0.4: Redis and Kafka providers, shop worker

Decided at the start of v0.4 (2026-10-27). Builds on §6 and §14. The config shapes of §4 stay
(`shared.redis.prefix`, `shared.kafka.groups` and `topic_prefix`).

**Decisions**
- **Both providers** register in `providers/__init__.py`. Their `{{ redis... }}` and `{{ kafka... }}`
  names stop failing with the "arrives in v0.4" message. They use no new Python dependency: every
  operation is a `docker exec` of the CLI tools inside the server container.
- **Redis server.** Container `wt-redis` (official `redis` image, alpine tag) on the shared `wt`
  network, published on `127.0.0.1:56379`. State lives in a named volume.
- **Redis isolation.** Key prefix `<project>:<branch-slug>:` (project-scoped from v0.5.1, §20). Resources = `[prefix]`. Template vars:
  `redis.url` (`redis://wt-redis:6379` for `compose`, `redis://127.0.0.1:56379` for `run`) and
  `redis.prefix`. `verify`: PING, then SET/GET/DEL of a probe key under the prefix. `destroy`:
  delete every key under the prefix (SCAN + UNLINK through `redis-cli`); other prefixes are never
  touched. `prefix: false` means no isolation: allowed, and `verify` returns a warning.
- **Kafka server.** Single-node KRaft container `wt-kafka` (official `apache/kafka` image) on the
  `wt` network, with one listener for containers (`wt-kafka:9092`) and one for host processes on
  `127.0.0.1:59092`. Topic auto-creation stays on, so apps create their own topics.
- **Kafka isolation.** Consumer group `<group>-<project>.<branch-slug>` for each name in `groups`;
  with `topic_prefix: true`, topics are named `<project>.<branch-slug>.<topic>` (project-scoped from
  v0.5.1, §20). Resources = the group names (and
  the prefix). Template vars: `kafka.bootstrap` (by `kind`, as above), `kafka.group('name')` (a
  name not in `groups` is a WtError) and `kafka.topic('name')`. `verify`: the broker answers
  and, with a prefix, a probe topic can be created and deleted. `destroy`: delete the branch's
  consumer groups and every topic with the branch prefix; safe when none exist.
- **doctor.** One check per configured provider (container running, answers), and `--fix` starts
  it, as for Postgres.
- **`examples/shop`** gets a `worker`, a compose service of the `api` repo (so it joins the `wt`
  network and reaches `wt-kafka` and `wt-redis`). `POST /items` publishes an item event; the worker
  consumes it with `kafka.group('worker')` and counts events per branch in Redis under
  `redis.prefix`. The client libraries are installed in the api Dockerfile; `wt` itself has none.
- **README.** An architecture diagram in Mermaid (no GIF, no image file) and a Benchmarks section
  with numbers measured from `examples/shop` on the author's machine, with the exact command. A
  number that cannot be measured is left out (CLAUDE.md "Numbers").
- **Names and env.** Containers `<instance>-redis` and `<instance>-kafka` and named volumes
  `<instance>-redis-data`, `<instance>-kafka-data` follow `WT_INSTANCE` like Postgres. Overrides:
  `WT_REDIS_IMAGE` (default `redis:7-alpine`), `WT_REDIS_PORT` (56379), `WT_KAFKA_IMAGE` (default
  a pinned `apache/kafka` tag), `WT_KAFKA_PORT` (59092).
- **Kafka listeners.** `INTERNAL://<container>:9092` for compose services; `EXTERNAL` on container
  port 19092, published as `127.0.0.1:<WT_KAFKA_PORT>:19092` and advertised as
  `127.0.0.1:<WT_KAFKA_PORT>`; controller on 9093; `KAFKA_AUTO_CREATE_TOPICS_ENABLE=true`.
- **Names (project-scoped from v0.5.1, §20).** Redis prefix `<project>:<slug>:`; Kafka group
  `<group>-<project>.<slug>`; topic `<project>.<slug>.<name>` when
  `topic_prefix`, else `<name>`. A name not in `groups` given to `kafka.group()` is a WtError.
- **No base branch.** Redis and Kafka have none: `down` of any branch (the base too) cleans its own
  slice. With `prefix: false`, `redis.prefix` is `""`, `destroy` deletes nothing and `verify`
  warns. With `topic_prefix: false`, `destroy` deletes only the groups.
- **Destroy is exact.** Deleting branch `feat` never touches `feat-x:*` keys or `feat-x.*` topics:
  the Redis pattern ends with `:`, and Kafka topics are matched on the exact prefix in Python,
  not with a broker-side pattern. `destroy` also cleans the prefix or group names recorded in the
  branch state when the config no longer enables them (only names wt recorded itself). A Kafka
  group that still has a live consumer is not deleted (the tool exits 0 and prints
  `GroupNotEmptyException`): `destroy` retries for up to 20 s (a stopped consumer leaves the group
  when the broker notices it) and, if members remain, raises `consumer group <group> still has
  members`, so `down` stops with the branch kept as `removing` (see 'Down failures') and the group is
  not forgotten. The retry runs inside `file_lock("kafka")`, so for up to 20 s it can delay an `up`
  of another branch that uses Kafka and `doctor`.
- **Locks and waits.** `file_lock("redis")` / `file_lock("kafka")`, also used by doctor.
  `redis-cli PING` answers PONG; Kafka answers
  `kafka-broker-api-versions.sh --bootstrap-server localhost:9092`.
- **One deadline.** `ensure_server` gives `docker compose up --wait` and the readiness wait one
  deadline per provider (Postgres 60 s, Redis 60 s, Kafka 90 s, Keycloak 120 s). The compose
  start gets the whole value as `--wait-timeout`; the readiness wait gets what is left (at least
  1 s). The worst case is the stated value, never twice that. The readiness error names the total.
- **Template values.** `redis.url` is `redis://<container>:6379/0` for `compose` and
  `redis://127.0.0.1:<WT_REDIS_PORT>/0` for `run`; `kafka.bootstrap` is `<container>:9092` for
  `compose` and `127.0.0.1:<WT_KAFKA_PORT>` for `run`.

## 19. v0.5: Keycloak provider (one shared realm, one client per branch)

Decided at the start of v0.5 (2026-10-28). Builds on §6, §14 and §18. The config shape of §4
stays (`shared.keycloak: {realm, client}`); `realm` and `client` must match `^[A-Za-z0-9._-]+$`.

**Decisions**
- **Server.** Container `<instance>-keycloak` (`wt-keycloak`) from the official
  `quay.io/keycloak/keycloak`, pinned to `26.8.1` (`WT_KEYCLOAK_IMAGE` overrides), running
  `start-dev` with the named volume `<instance>-keycloak-data` (H2 file database) on the shared
  `wt` network, published on `127.0.0.1:58080` (`WT_KEYCLOAK_PORT`). The bootstrap admin is `admin`
  with a password generated once into `~/.wt/keycloak/password` (mode 0600) and passed as
  `KC_BOOTSTRAP_ADMIN_USERNAME` / `KC_BOOTSTRAP_ADMIN_PASSWORD`. `KC_HOSTNAME` is
  `http://localhost:<port>`, `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true` and `KC_HTTP_ENABLED=true`.
- **Operations.** Everything goes through `kcadm.sh` inside the container (the image has bash but
  no `curl`); no new Python dependency. Each call is one `docker exec <container> bash -c
  '<script>' kcadm <args...>` whose script logs in with `$KC_BOOTSTRAP_ADMIN_*` from the
  container environment into a unique temporary config file (removed on exit) and then runs
  `kcadm.sh "$@"`. User values are positional arguments, never part of the script, and the admin
  password never appears on the host command line.
- **Readiness.** No compose healthcheck. `ensure_server` waits for `up --wait` (running), then
  polls a `kcadm` login (`get realms/master`) within the shared 120 s deadline of §18 "One
  deadline" (a fresh start logs in after about 17 s). If the password file is gone but the volume is not, the login keeps failing and the
  error names `~/.wt/keycloak/password` and the volume.
- **Realm.** `ensure_server` creates the shared realm if missing (`realm=<realm> enabled=true
  sslRequired=none`; `create realms` exits 1 with "already exists", which counts as done). wt
  never deletes the realm and never creates users, roles or identity providers; the user adds
  users in the console at `http://localhost:<port>` with the generated admin password.
- **Client per branch.** `clientId = <client>-<project>.<branch-slug>` (project-scoped from v0.5.1,
  §20): a public client, standard flow, PKCE
  allowed but not enforced (apps should still send it). `redirectUris` is `<url>/*` and
  `webOrigins` is `<url>` for every routed service of the branch that is selected (§17), where
  `<url>` is the service URL including the HTTPS port when it is not 443. `create` is idempotent:
  it looks the client up by exact `clientId` (matched in Python, never by prefix) and then
  creates or updates it. The id of a new client is on stdout, quoted (`Created new client with
  id '<uuid>'`).
- **Verify and destroy.** `verify`: the client exists with exactly the rendered redirect URIs (a
  missing realm counts as a missing client). `destroy`: delete only this branch's client; branch `feat` never touches
  `feat-x`. The realm and its users stay. A realm or client that does not exist is not an
  error (nothing to do, silent). A kcadm call that cannot log in, cannot reach the server, or
  returns something that is not JSON raises a short `WtError` (`keycloak: <reason>`), for
  `destroy` and `verify` alike: a stopped server is never read as "client not found". A failing
  kcadm call is reported on one line with the cause first, `keycloak: <reason> (kcadm.sh <verb>
  <resource>)` (for example `(kcadm.sh get clients)`), without realm, client or ids, and never with
  the login script or any password. Resources are recorded as `["<realm>/<clientId>"]` so
  `down` works even after `shared.keycloak` leaves the config: without the section
  `ensure_server()` only starts the server and waits for the login (no realm is created) and
  `destroy` deletes the recorded clients.
- **Template values.** `keycloak.url` (`http://localhost:<port>`), `keycloak.issuer`
  (`<url>/realms/<realm>`, the same for every branch: one login works across the branch
  subdomains), `keycloak.realm`, `keycloak.client_id` and `keycloak.internal_url`
  (`http://<container>:8080` for `compose`, the same as `url` for `run`).
- **Provider context.** `BranchContext` gets `urls: tuple[str, ...] = ()`, filled by the
  orchestrator with the URL of every selected routed service, so providers see the HTTPS port.
- **doctor.** One check for the Keycloak container, `--fix` starts it, under `file_lock("keycloak")`.
- **`examples/shop`** gets `shared.keycloak: {realm: shop, client: web}` and a "Sign in" link on the
  web page built from `KEYCLOAK_URL`, `KEYCLOAK_REALM` and `KEYCLOAK_CLIENT_ID`. It enforces
  nothing; it proves the per-branch client and redirect URI.

## 20. v0.5.1: dependencies run together

Decided 2026-10-31. There is no paid staging, so a service that is not running cannot be replaced by
a staging URL. This replaces §17 Contract 1 (exact `uses`), Contract 2 (`remote:` required), Contract
7 (`Choice`) and Contract 9 (`examples/notes`), and limits §17 D-d. `services:` projects are unchanged.

**Decisions**
- **The stack is the closure.** For a catalog project the stack is the explicit selection
  (`BranchState.uses`, else `uses:`) plus every service reachable through `consumes`, transitively.
  Selecting `front`, which consumes `back`, starts both; `down` and `stop` handle both. A cycle is
  still an error ("cycle through catalog consumes: a -> b -> a") and so is an unknown name.
- **`BranchState.uses` stays the explicit selection only.** The closure is computed on every load,
  `with_uses` and `configure`, so dropping a consumer also drops a dependency that was not selected
  explicitly. Service order is the selection entries, then their dependencies (§15); the service
  index stays the position in the catalog (§17 D-b).
- **Environment and order.** `consumes: {dep: VAR}` always sets `VAR: "{{ services['dep'].url }}"` (the
  local URL of the running dependency) and adds `dep` to `needs`, so the dependency starts first. The
  literal-`remote` branch of §17 Contract 1 is gone. `infra` is merged from the whole stack.
- **`remote:` is optional.** It must still be an `http://` or `https://` URL when present. The error
  "is consumed by … and needs 'remote:'" is removed. A `remote:` on a service that is in the stack only
  because a selected service consumes it is ignored, with one warning per load (a service selected
  explicitly never used its `remote:`, so it gets no warning): `remote of 'back' is ignored:
  'front' needs it, so it runs locally`. It is now used only to render `services.<x>.url` (D-d: `host` `""`, `port` None)
  of a catalog service that is neither selected nor consumed by a selected service. Existing files
  keep loading. `wt catalog` keeps its REMOTE column.
- **Configure.** `Choice(service: str, repo: str, selected: bool, required_by: tuple[str, ...] = ())`.
  `selected` is true for every service in the stack. `required_by` lists the stack services that
  consume a service that is in the stack only because of them (a service selected explicitly has
  `required_by=()`); with `a -> b -> c` and `uses: [a]`, `c` is required by `b`. `configure(branch, uses)` stores the explicit list, validates names, and keeps the
  "select at least one service" error. Services that leave the stack are torn down as §17 D-e; those
  that enter are started, dependencies first. Unchecking a service that a selected service still
  consumes changes the stored selection but not the running stack.
- **TUI.** A choice with `required_by` is shown checked and labelled `(required by front)`, cannot be
  toggled while one of its consumers is still checked, and is left out of the list sent on confirm
  (explicit = checked minus locked). Unchecking the consumer lifts the lock at once: the dependency
  stays checked and now counts as an explicit selection, so `back` alone can be configured. The
  confirmation of `remove` wraps over as many lines as the box needs (it is never cut at one line).
- **`examples/notes`.** `uses: [front]` brings `back` in; the fictional staging URL is removed from
  its catalog.

**Published ports (v0.5.1).** After it starts Traefik or a shared server (Postgres, Redis, Kafka,
Keycloak), wt checks that the running container really publishes its host ports (`docker port
<container>`). A container can run without them: after a start that failed because the host port was
taken, Docker's next start attaches no network and publishes nothing, and the container still reports
healthy. In that case wt recreates the container once (`docker compose up -d --force-recreate`, for
Traefik and for a shared server alike; when a recreate was already requested it is not repeated). If
the recreate fails because the port is taken, or the port is still not published afterwards, `up`
fails at once and says why: `port N is already in use by another program` when something listens on
it, else `<container> does not publish port N`. `wt doctor` reports the same condition as a failed check
(`<container> runs without its published ports`) and `wt doctor --fix` recreates the container. The
check applies to the ports wt publishes on `127.0.0.1` (and the HTTP/HTTPS ports of Traefik), not to
containers of the user's services.

**Duplicate hosts (v0.5.1).** Every route file Traefik reads lives in one shared folder, so two
projects with the same `domain` and the same branch would write identical `Host(...)` rules and
the answering backend would be undefined, while verification still passes. Before `up` writes a
branch's route file, wt compares the hosts it is about to route with the hosts of every other route
file in that folder (the files of other projects and branches; its own file is ignored). A host
already routed by another file fails `up` before anything is created, with
`host <host> is already routed by <project>/<slug>; give one of the projects another domain` (the
route file name only keeps the project and the slug). Two `up` started at the very same moment can
still race; the check is a safeguard, not a lock.

**Project-scoped names (v0.5.1).** The shared Redis, Kafka and Keycloak servers serve every project,
but their per-branch names used only the branch slug, so `wt down main` in one project deleted the
keys, groups, topics and client that another project's `main` owned. The project is now part of
every name:

| Provider | Name (project `shop`, branch `feat/cart`) |
|---|---|
| Redis | prefix `shop:feat-cart:` |
| Kafka | group `<group>-shop.feat-cart`; with `topic_prefix`, topics `shop.feat-cart.<name>` |
| Keycloak | client id `<client>-shop.feat-cart` |

`.` and `:` cannot appear in a project or a slug (both are lowercase letters, digits and `-`), so
`a-b` + `c` and `a` + `b-c` never produce the same name. `destroy` deletes exactly the recorded names
(and the exact prefix match of §18 still holds), and it accepts both formats: a branch created by
0.5.0 (old names, without the project) is still cleaned by `wt down`. The provider resources
recorded for a branch accumulate (the union of what was recorded before and what `create` returns),
so re-running `up` on a 0.5.0 branch with the new version keeps the old names recorded until `down`. For the same reason a name that
leaves the config (a database removed from `databases:`) stays recorded and visible in `wt ls`
until `down`.
`realm` and `client` in `shared.keycloak` are limited to 64 characters (a `worktree.yaml` with a
longer one now fails to load, where it used to fail only at `up` above about 200), so `<client>-<project>.<slug>`
stays far under Keycloak's own limit. **Upgrading from 0.5.0:** run `wt down` on branches that use
Redis, Kafka or Keycloak before upgrading when you can; otherwise their old keys, groups and clients
are removed by the `wt down` of that same branch after the upgrade. A name recorded by 0.5.0 can be
ambiguous: an old Redis prefix `x:` or Kafka topic prefix `x.` is a plain prefix match, and `x` may
also be the name of a project that already uses the new format (`app` against `app:main:...`), the
current project included. So `destroy` deletes an old one-segment prefix or topic prefix only when
`x` is not the name of any known project (the current one and the projects wt knows); otherwise it
leaves those keys or topics alone and logs `kept <prefix>: it could belong to project <x>; delete
by hand if it is yours`. Old consumer groups and Keycloak clients are matched by exact name and are
always deleted.

## 21. v0.6.0: one global catalog, `wt` from anywhere, JSON output

Decided 2026-11-01 (approval step skipped in away mode; the user's choices are in the plan log of
`global-catalog`). Replaces §4 (`worktree.yaml`), the `uses:`/`catalog:` keys of §15 and the project
discovery of §16. Everything else (state, naming, ports, hosts, providers, routing) is unchanged.

**One catalog = one project.**
- **File.** The catalog is the only configuration file. Location, first match wins: `--catalog PATH`
  (`-c`), `WT_CATALOG`, `$WT_HOME/catalog.yaml`. wt never looks in the current folder or its parents.
  Another project uses another catalog file (`WT_CATALOG`); its state lives in `$WT_HOME/<project>/` as before.
- **Keys.** `project` and `domain` (required; same patterns as the old `worktree.yaml`), `repos`,
  `services` (§15, unchanged: every `Service` key plus `infra`, `consumes`, `remote`). Unknown keys are
  an error. `catalog:`, `uses:` and `shared:` do not exist; the shared servers come from `infra`.
  Relative `repos` paths are relative to the catalog file's folder.
- **Missing file.** `WtError("no catalog at <path>. Run wt init, or wt migrate <worktree.yaml>")`.
- **Removed.** `worktree.yaml`, `find_config`, `--config`, `WT_CONFIG`. A `worktree.yaml` is only read by `wt migrate`.

**Anchor and selection.**
- `load_project(path, uses=None) -> LoadedConfig` (model layer). `LoadedConfig.path` and `.catalog`
  are the catalog file; `.uses` is always a tuple; `service_order` is the catalog order (ports, §17 D-b).
- The orchestrator holds an **anchor** (`uses=None` means the first service in catalog order, plus its
  closure), used only for project-level work (project, domain, names, `ls`, choices). Every branch
  operation that creates or starts things uses the branch's own stored selection (`BranchState.uses`,
  §17 D-a), closure through `consumes` as in §20.
- A branch recorded before v0.6.0 without `uses` has no selection.

**Creating a wt (a git branch plus its services).**
- `wt add BRANCH [--uses a,b]` and `wt up BRANCH [--uses a,b]`. `--uses` takes catalog service names,
  comma separated. `add` of a branch already in state is a no-op with a note: the stored selection is
  not changed (use the TUI configure screen). `up` of a branch that is not in state with `--uses` is
  `add` then `up`.
- No selection (no `--uses`, branch unknown or without `uses`): `up` and `add` fail with
  `branch <b> has no services yet; run wt add <b> --uses a,b`. On a terminal, `add` without `--uses`
  instead asks for the services as a numbered checklist (empty answer cancels, nothing is created).
- Empty `--uses` value: `choose at least one service (wt add <b> --uses a,b)`. Unknown name: the usual
  `unknown service` error listing the catalog's names.
- `ls`, `stop`, `down`, `remove`, `logs` need no selection (they use the anchor and what the state records).
- The TUI `a` key (any folder where a catalog loads): `InputScreen("Enter name")`, then the configure
  checklist (§20: dependencies checked and locked, title `Services for <name>`), then `add`. Esc at
  either step cancels without side effects. No catalog: the message line says
  `no catalog at <path>. Run wt init or wt migrate`.
- `wt tui` opens the project of the catalog. `wt tui --all`, or no catalog, lists every project
  known from state (§16 "outside a project"); `a` there behaves as in §16 (the project of the selected row).
  A project whose state points to a file that is not a catalog with that `project` shows
  `created with a worktree.yaml: run wt migrate <path>`.
- `BranchState`/`state.json` gain nothing; `config_path` now records the catalog path.

**`wt init`.** Writes a starter catalog at the catalog location (project = name of the current
folder, one repo = the current folder as an absolute path, one compose service). It refuses to
overwrite: `catalog already exists at <path>`.

**`wt migrate PATH`.** Imports a v0.5.x `worktree.yaml` into the catalog (created when missing).
- `project`/`domain`: set when the catalog is new; an existing catalog with another project or domain is
  an error (`catalog is project <x>, <path> is project <y>`).
- `repos`: made absolute against the worktree.yaml's folder.
- `services:` style: every service is copied; the project-level `shared:` becomes `infra`: a service gets
  the `shared` provider sections its `env`, `run` or `health` text mentions as `{{ <provider>.`; a section
  mentioned by no service goes to the first service.
- `uses:` style (own `catalog:` file or the default one): the services named by `uses` (and their
  dependencies) are copied from that catalog with their repos.
- A name that already exists in the catalog with a different definition is an error listing every such name
  (`service 'api' differs from the catalog's`); an identical one is skipped. Running it twice changes
  nothing. The catalog is validated before it is written (atomic write); a failure leaves it untouched.
- Branches of that project in state that have no `uses` get `uses` = the migrated stack (all services for
  `services:` style, the file's `uses` for `uses:` style), so they keep working.
- Output: one line per service added or skipped, then the adopted branches.

**JSON output.** `--json` on `ls`, `doctor`, `catalog`, `add`, `up`, `down`, `stop`, `remove` prints one
document on stdout and nothing else (progress goes to stderr, no rich markup). Keys are lower_snake_case,
absent values are `null`, and every document starts with `"schema": 1`:
```
ls:      {"schema":1,"project":"notes","branches":[{"branch":"feat/x","status":"up","services":[
          {"name":"back","kind":"compose","status":"up","health":"ok","url":"https://…","port":8000}]}]}
doctor:  {"schema":1,"ok":true,"checks":[{"name":"docker","ok":true,"detail":"…","fixable":false}]}
catalog: {"schema":1,"project":"notes","domain":"notes.localhost","services":[{"name":"back","repo":"notes-back",
          "kind":"compose","infra":["postgres"],"consumes":{"back":"API_URL"},"consumed_by":["front"],"remote":null}]}
actions: {"schema":1,"branch":"feat/x","action":"up","status":"up","urls":{"front":"https://…"},"warnings":[]}
error:   {"schema":1,"error":"<the text the CLI prints>"}      (stdout, exit code 1)
```
`health` is `ok`, `fail` or `null` (not probed). `kind` is `compose` or `run`. The TUI snapshot for agents is `wt ls --json`.

**Examples.** `examples/shop/bootstrap.sh` and `examples/notes` produce a `catalog.yaml` (the walkthroughs
use `WT_CATALOG`); `examples/shop/legacy/worktree.yaml` keeps the old format for `wt migrate` demos.
