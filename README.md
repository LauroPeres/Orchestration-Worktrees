# Orchestration-Worktrees

Run a full, isolated copy of your dev stack for every git branch, side by side on one machine,
each on its own HTTPS subdomain.

```bash
wt up feat/cart     # git worktree + containers + route + TLS, then a health check
wt up main          # a second copy, running next to the first
wt ls               # branches, URLs, health
wt down feat/cart   # remove everything wt created for that branch
```

After `wt up feat/cart`, `https://api.feat-cart.shop.localhost` serves that branch's API while
`https://api.main.shop.localhost` keeps serving `main`.

wt ships no services of its own: you describe your stack (any repos, any languages) in
`worktree.yaml`. See [docs/SPEC.md](docs/SPEC.md) for the full design and roadmap.

> **Status: v0.2.2.** Git worktrees, Docker Compose services, host processes (`run:`), Traefik
> routes and local TLS, and a Postgres provider that gives every branch its own cloned
> databases. A complete two-branch example is in [examples/shop](examples/shop/README.md).
> Kafka and Redis come in v0.3.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- git, Docker with Compose 2.24+
- [mkcert](https://github.com/FiloSottile/mkcert) for trusted certificates (optional; without it
  branches get a self-signed certificate). On Debian/Ubuntu:
  `sudo apt install -y mkcert libnss3-tools`
- macOS, Linux, or Windows through WSL2

## Install

```bash
uv tool install git+https://github.com/LauroPeres/Orchestration-Worktrees
wt doctor          # checks docker, git, ports, certificates, name resolution
wt doctor --fix    # installs the local CA, creates the docker network, starts Traefik
```

`wt doctor --fix` creates the Docker network `wt` and starts the `wt-traefik` container on
`127.0.0.1:80/443`. `mkcert -install` may ask for your sudo password.

## Quick start

In your repo:

```bash
wt init            # writes a starter worktree.yaml
$EDITOR worktree.yaml
wt up my-branch    # prints a SERVICE / URL / CHECK table
wt ls              # branches, URLs, health, databases (--no-probe skips HTTP checks, --paths shows folders)
wt logs my-branch api   # follows one service's logs, compose or run: (--no-follow, --tail N)
wt down my-branch       # --force also removes worktrees with uncommitted changes
```

Running `wt up` again for a branch that is already up changes nothing. `wt down main` keeps
your main checkout.

`wt ls` counts `UP` as running/total over every service of a branch (compose containers and live
`run:` processes, e.g. `2/2`). STATUS shows `degraded` when docker is reachable and a service is
not running or a health check fails. Piped or redirected output is never wrapped or cut; only a
terminal gets the fixed-width layout.

## `worktree.yaml`

```yaml
project: shop
domain: shop.localhost          # *.localhost resolves to 127.0.0.1 in browsers

repos:                          # each repo gets a git worktree per branch
  api: ../shop-api
  web: ../shop-web

services:
  api:
    repo: api
    compose: docker-compose.yml # compose file inside the repo
    port: 8000                  # port the container listens on
    subdomain: api              # https://api.<branch>.shop.localhost
    health: /healthz            # must answer 2xx/3xx (or 401/403) after `up`
    env:
      PUBLIC_URL: "{{ url }}"
      WEB_URL: "{{ services.web.url }}"
  db:
    repo: api
    compose: docker-compose.yml # no subdomain: not routed, only checked for "running"
```

| Field | Meaning |
|---|---|
| `compose` | Compose file relative to the repo root. Set exactly one of `compose` or `run`. |
| `run` | Command for a host process (see below). Templates allowed. |
| `compose_service` | Service name in the compose file, if it differs from the key. |
| `port` | Container port Traefik routes to (compose), or the host port of a `run:` service, or `auto`. Required when `subdomain` is set. |
| `subdomain` | `""` = branch root (`https://<branch>.<domain>`); unset = not routed. |
| `health` | Path probed through Traefik after `up` (default `/`). |
| `env` | Extra environment for the container. Values can use templates; a `$` in a value is literal. |

Template values, resolved per branch: `project`, `branch`, `slug` (`feat-cart`), `ident`
(`feat_cart`), `domain`, `branch_domain`, `port`, `url`, `host`, and
`services.<name>.url|host|port`.

## Host processes (`run:`)

A service with `run:` instead of `compose:` is a plain process on your machine, started in that
branch's worktree:

```yaml
  web:
    repo: web
    run: npm run dev -- --host 0.0.0.0 --port {{ port }}
    port: auto                  # a free port per branch, kept for that branch
    subdomain: ""               # https://<branch>.shop.localhost
```

- The command runs with `sh -c`, with your environment plus the rendered `env`. `{{ port }}` and
  `services.<name>.port` hold the resolved port.
- Every `{{ ... }}` value in `run:` is shell-quoted for you, so a branch name like `x;rm -rf ~`
  stays one literal argument. Do not add quotes around template values in `run:`.
- `port: auto` picks a stable free port per (project, branch, service); an explicit number is
  used as is and `up` fails if something else holds it.
- Running `wt up` again keeps a live process as is. If its rendered command, port or `env`
  changed in `worktree.yaml`, `up` stops it and starts the new one.
- Output goes to `~/.wt/<project>/branches/<slug>/logs/<service>.log`; read it with
  `wt logs <branch> <service>`.
- Traefik reaches the process through `host.docker.internal`. **Bind to `0.0.0.0`** (not only
  `127.0.0.1`): on native Linux the name resolves to the Docker bridge gateway, so a process
  listening only on loopback is unreachable. Docker Desktop (macOS, WSL2) also works with
  `127.0.0.1`, but `0.0.0.0` is the portable choice.
- `down` stops the whole process group (SIGTERM, then SIGKILL after 10 s).

## Postgres per branch (`shared.postgres`)

```yaml
shared:
  postgres:
    databases: [shop]       # each one is cloned per branch
    strategy: template      # template | dump-restore
    verify: true            # compare tables and row counts after the clone
    base_branch: main       # whose databases are the clone source (default main)
```

wt runs one shared `wt-postgres` container (`postgres:17-alpine`, superuser `wt`, password
generated into `~/.wt/postgres/password`, published on `127.0.0.1:55432`). Branch `feat/cart`
gets the database `shop__feat_cart`.

- On `base_branch` the databases are created empty; your migrations and seed fill them. Any
  other branch is cloned from the base databases the first time, and an existing clone is never
  re-cloned. If the base database does not exist yet, the clone is created empty with a
  warning: run `wt up <base_branch>` first.
- `template` clones with `CREATE DATABASE ... TEMPLATE`: fast, but base connections are
  dropped for about a second. `dump-restore` uses `pg_dump | pg_restore` and does not block the
  base branch.
- `verify` compares the table list and row counts of source and clone. With `template` a
  mismatch fails `up`; with `dump-restore` count differences only warn.
- Use it in templates: `{{ postgres.url('shop') }}` is a connection URL and
  `{{ postgres.name('shop') }}` the database name. Compose services get the address on the
  shared `wt` network; host processes get `127.0.0.1:55432`. Every compose service listed in
  `services:` joins the `wt` network when Postgres is configured.
- `down` drops that branch's databases and never the `base_branch` ones. It starts the shared
  Postgres container first if it is stopped.
- wt never drops the `base_branch` databases. When you are done with a project, drop them by
  hand, one per database (named `<database>__<base_branch>`):

  ```bash
  docker exec wt-postgres dropdb -U wt --force shop__main
  ```

  To remove the shared server with every database in it:
  `docker rm -f wt-postgres && docker volume rm wt-postgres-data && rm -r ~/.wt/postgres`.
- A branch whose database names equal the base branch's (for example `Main` with the base
  `main`) is refused by `up` before anything is created. Rename the branch.
- Kafka, Redis and Keycloak under `shared:` are validated but ignored until later versions.

## How isolation works

- **Names are derived from (project, branch)**, so the same branch always gets the same
  resources and `down` never guesses. `feat/cart` becomes `feat-cart`.
- **One compose project per branch and repo** (`wt-shop-feat-cart-api`). wt adds an override
  file that removes published host ports and fixed `container_name`s (they would collide
  between branches) and attaches routed services to a shared `wt` Docker network.
- **One shared Traefik** bound to `127.0.0.1`, with one route file per branch in
  `~/.wt/traefik/dynamic/`.
- **One mkcert certificate per branch** for `<branch>.<domain>` and `*.<branch>.<domain>`,
  made again by `up` when `domain` changes.
- **Worktrees** live in `~/.wt/<project>/worktrees/<branch>/<repo>`. A branch that is already
  checked out (for example `main` in your repo) is used where it is and never removed.
- **`down` keeps your work**: it refuses when a worktree has uncommitted or untracked files
  (unless `--force`) and never deletes branches or commits. It does remove the branch's
  containers and volumes. A folder at a worktree path that is not a git worktree is left in
  place with a warning.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `WT_CONFIG` | nearest `worktree.yaml` | config path (same as `--config`) |
| `WT_HOME` | `~/.wt` | where wt keeps state, worktrees and Traefik files |
| `WT_HTTP_PORT` / `WT_HTTPS_PORT` | `80` / `443` | host ports for Traefik |
| `WT_TRAEFIK_IMAGE` | `traefik:v3.5` | Traefik image |
| `WT_POSTGRES_IMAGE` | `postgres:17-alpine` | Postgres image |
| `WT_POSTGRES_PORT` | `55432` | host port of the shared Postgres |
| `WT_INSTANCE` | `wt` | prefix of the shared Docker objects (isolates test runs) |

A port variable (`WT_HTTP_PORT`, `WT_HTTPS_PORT`, `WT_POSTGRES_PORT`) that is not a number from 1
to 65535 makes wt stop with an error that names the variable.

## WSL2

Tested on WSL2 with Docker Desktop. Inside WSL, `*.localhost` resolves for curl and browsers
and HTTPS works with the mkcert CA. Docker Desktop's WSL integration must be turned on for
your distro, otherwise `docker` is not found (see Troubleshooting).

Host processes (`run:`) are reached through `host.docker.internal`, which Docker Desktop
resolves for the Traefik container; the process may listen on `0.0.0.0` or `127.0.0.1`. On
native Linux Docker Engine only `0.0.0.0` works.

Browsers on Windows reach the stack through Docker Desktop's ports on `127.0.0.1`, but they do
not trust the CA that mkcert creates inside Linux. To trust it:

1. Copy `$(mkcert -CAROOT)/rootCA.pem` to a Windows path, for example
   `C:\Users\Public\wt-rootCA.crt`.
2. Double-click it, choose Install Certificate, then Current User.
3. Place it in "Trusted Root Certification Authorities".
4. Restart the browser.

## Troubleshooting

- **`docker` says it "could not be found in this WSL 2 distro"**: turn on Docker Desktop's
  WSL integration for that distro (Settings, Resources, WSL integration).
- **Windows `curl.exe` fails with `CRYPT_E_NO_REVOCATION_CHECK` on mkcert certificates**: use
  `curl.exe --ssl-no-revoke`. Browsers are not affected.
- **`wt doctor` warns that the OS resolver does not resolve `*.<domain>`**: expected. Browsers
  and curl resolve `*.localhost` themselves.

## Development

```bash
uv sync
uv run pytest                # unit tests
uv run pytest -m docker      # integration tests against a real Docker daemon
uv run ruff check && uv run ruff format --check && uv run mypy
```

## License

MIT
