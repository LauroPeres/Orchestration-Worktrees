# Example: shop (5-minute walkthrough)

Two branches of a small shop stack, each running in its own isolated copy: `main` and `feat/cart`.
`feat/cart` adds a `quantity` column to items. Everything here is fictional and exists only to show
how wt works.

## Prerequisites

- git and Docker with Compose 2.24+. See the [requirements in the top-level README](../../README.md#requirements).
- [mkcert](https://github.com/FiloSottile/mkcert) is optional. Without it, wt uses a self-signed
  certificate and your browser will warn you.
- macOS, Linux, or Windows through WSL2.
- `wt` on your PATH: `uv tool install git+https://github.com/LauroPeres/Orchestration-Worktrees`
  (see [Install](../../README.md#install)). From a clone of this repo you can use
  `uv run --project <path-to-clone> wt` instead of `wt` in every step below.

## 1. Create the example repositories

From the root of this repository:

```bash
examples/shop/bootstrap.sh /tmp/shop
```

This creates `/tmp/shop/shop-api` (branches `main` and `feat/cart`), `/tmp/shop/shop-web` (branch
`main`) and `/tmp/shop/worktree.yaml`. The script refuses to use a non-empty directory.

## 2. Start the stack

```bash
cd /tmp/shop
wt doctor --fix   # checks Docker, git, ports and certificates; creates the network and starts Traefik
wt up main
wt up feat/cart
```

Each `wt up` starts the API and the worker in Docker, the Postgres database cloned from the template,
and the web page as a host process. The shared Kafka and Redis containers (`wt-kafka`, `wt-redis`)
start with the first `wt up`. The `feat/cart` database is cloned from `main`, so the first `wt up` of
a branch may briefly drop the connections of the base branch's app during the clone.

## 3. Open the URLs

| URL | What you should see |
|---|---|
| `https://main.shop.localhost` | "shop on main" and three items: keyboard, mouse, monitor |
| `https://feat-cart.shop.localhost` | "shop on feat/cart" and the same items, each with `x 1` |
| `https://api.main.shop.localhost/items` | JSON rows with `id` and `name` only |
| `https://api.feat-cart.shop.localhost/items` | JSON rows with `id`, `name` and `quantity` |

The web page runs as a host process bound to `0.0.0.0`, so Traefik in Docker can reach it. That
also means it listens on every network interface of your machine, not only on localhost.

## 4. Check the isolation

1. Open `https://feat-cart.shop.localhost` and add an item, for example `cable`.
2. Reload `https://main.shop.localhost`. `cable` is not there.
3. `https://api.feat-cart.shop.localhost/items` lists `cable` with `quantity` 1. The
   `https://api.main.shop.localhost/items` response has no `quantity` key.

## 5. List and stop a branch

```bash
wt ls                # both branches with their URLs, health, kinds and databases
wt down feat/cart    # removes its containers, process, route, certificate, database,
                     # worktrees, Kafka group and topics, and Redis keys
```

After `wt down feat/cart`, `https://main.shop.localhost` still works. You can run `wt up feat/cart`
again to bring it back.

## 6. Events and the worker

Each `POST /items` publishes an event to the Kafka topic `<slug>.items`, for example `main.items`
or `feat-cart.items`. A Kafka failure is logged and does not fail the request.

The `worker` service is a compose service of the `api` repo (same image, `python worker.py`). It
joins the consumer group `worker-<slug>` and, for each event, does two things in Redis:

- `INCR <slug>:events`, the counter
- `SET <slug>:last`, the name of the last item

Add an item on each branch (add `-k` to `curl` if you use the self-signed certificate):

```bash
curl -X POST https://api.main.shop.localhost/items -H 'Content-Type: application/json' -d '{"name": "lamp"}'
curl -X POST https://api.feat-cart.shop.localhost/items -H 'Content-Type: application/json' -d '{"name": "lamp"}'
```

Read the counters. Each branch has its own prefix (`main:` and `feat-cart:`):

```bash
docker exec wt-redis redis-cli GET main:events
docker exec wt-redis redis-cli GET feat-cart:events
docker exec wt-redis redis-cli GET feat-cart:last
```

A key prints nothing until its first event.

`wt down feat/cart` removes only the `feat-cart:` Redis keys, the `worker-feat-cart` consumer group
and the `feat-cart.*` topics. The `main` keys, group and topics stay. Redis and Kafka have no base
branch, so `wt down main` cleans `main`'s slice the same way.
