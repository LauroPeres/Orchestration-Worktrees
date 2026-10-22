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

> **Status: v0.1.** Git worktrees, Docker Compose services, Traefik routes and local TLS.
> Host processes (`run:`) and the Postgres provider come in v0.2; Kafka and Redis in v0.3.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- git, Docker with Compose 2.24+
- [mkcert](https://github.com/FiloSottile/mkcert) for trusted certificates (optional)
- macOS, Linux, or Windows through WSL2

## Install

```bash
uv tool install git+https://github.com/LauroPeres/Orchestration-Worktrees
wt doctor          # checks docker, git, ports, certificates, name resolution
wt doctor --fix    # creates the local CA, the docker network, and starts Traefik
```

## Quick start

In your repo:

```bash
wt init            # writes a starter worktree.yaml
$EDITOR worktree.yaml
wt up my-branch
```

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
| `compose` | Compose file relative to the repo root. One per repo in v0.1. |
| `compose_service` | Service name in the compose file, if it differs from the key. |
| `port` | Container port Traefik routes to. Required when `subdomain` is set. |
| `subdomain` | `""` = branch root (`https://<branch>.<domain>`); unset = not routed. |
| `health` | Path probed through Traefik after `up` (default `/`). |
| `env` | Extra environment for the container. Values can use templates. |

Template values, resolved per branch: `project`, `branch`, `slug` (`feat-cart`), `ident`
(`feat_cart`), `domain`, `branch_domain`, `port`, `url`, `host`, and
`services.<name>.url|host|port`.

## How isolation works

- **Names are derived from (project, branch)**, so the same branch always gets the same
  resources and `down` never guesses. `feat/cart` becomes `feat-cart`.
- **One compose project per branch and repo** (`wt-shop-feat-cart-api`). wt adds an override
  file that removes published host ports and fixed `container_name`s (they would collide
  between branches) and attaches routed services to a shared `wt` Docker network.
- **One shared Traefik** bound to `127.0.0.1`, with one route file per branch in
  `~/.wt/traefik/dynamic/`.
- **One mkcert certificate per branch** for `<branch>.<domain>` and `*.<branch>.<domain>`.
- **Worktrees** live in `~/.wt/<project>/worktrees/<branch>/<repo>`. A branch that is already
  checked out (for example `main` in your repo) is used where it is and never removed.
- **`down` keeps your work**: it refuses when a worktree has uncommitted or untracked files
  (unless `--force`) and never deletes branches or commits. It does remove the branch's
  containers and volumes.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `WT_CONFIG` | nearest `worktree.yaml` | config path (same as `--config`) |
| `WT_HOME` | `~/.wt` | where wt keeps state, worktrees and Traefik files |
| `WT_HTTP_PORT` / `WT_HTTPS_PORT` | `80` / `443` | host ports for Traefik |
| `WT_TRAEFIK_IMAGE` | `traefik:v3.5` | Traefik image |

## WSL2

Browsers on Windows reach the stack through WSL's localhost forwarding, but they do not trust
the CA that mkcert creates inside Linux. Import the file printed by `wt doctor`
(`$(mkcert -CAROOT)/rootCA.pem`) into Windows' "Trusted Root Certification Authorities".

## Development

```bash
uv sync
uv run pytest                # unit tests
uv run pytest -m docker      # integration tests against a real Docker daemon
uv run ruff check && uv run ruff format --check && uv run mypy
```

## License

MIT
