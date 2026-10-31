# Example: notes (front and back that talk)

A small notes app split in two repos: `notes-back` (an HTTP API in Docker with a Postgres
database) and `notes-front` (a host process serving one page). The two are described in a
catalog, and the worktree says `uses: [front]`: the front consumes the back, so selecting it
brings the back along. Each branch gets its own front, back and database. `feat/tags` adds a `tags`
column to the back. Everything here is fictional.

## Prerequisites

- git and Docker with Compose 2.24+. See the [requirements in the top-level README](../../README.md#requirements).
- [mkcert](https://github.com/FiloSottile/mkcert) is optional (without it wt uses a self-signed
  certificate and your browser will warn you).
- `wt` on your PATH (see [Install](../../README.md#install)), or `uv run --project <path-to-clone> wt`.

## 1. Create the example repositories

From the root of this repository:

```bash
examples/notes/bootstrap.sh /tmp/notes
```

This creates `/tmp/notes/notes-back` (branches `main` and `feat/tags`), `/tmp/notes/notes-front`
(branch `main`), `/tmp/notes/catalog.yaml` and `/tmp/notes/worktree.yaml`. The worktree points
at the local catalog (`catalog: ./catalog.yaml`), so your own `~/.wt/catalog.yaml` is never read
or changed. The script refuses to use a non-empty directory.

## 2. Look at the catalog

```bash
cd /tmp/notes
wt catalog           # services, repos, kind, infra, consumes, consumed by
wt catalog --check   # validates it
```

## 3. Start both branches

```bash
wt doctor --fix
wt up main
wt up feat/tags
```

Infrastructure (Traefik, Postgres) starts first, then the back, then the front, because the
front consumes the back and starts only after it is healthy.

## 4. What to look at

| URL | What you should see |
|---|---|
| `https://main.notes.localhost` | "notes on main", the notes, and a connection box with the API URL and the back's branch and database (`notes__main`) |
| `https://feat-tags.notes.localhost` | the same for `feat/tags`, with database `notes__feat_tags` |
| `https://api.main.notes.localhost/notes` | JSON notes without a `tags` key |
| `https://api.feat-tags.notes.localhost/notes` | JSON notes with `tags` |

Add a note on one front and reload the other: it only shows up on its own branch. Each front's
connection box shows the API URL of its own branch's back (injected as `API_URL`).

## 5. Stop

```bash
wt ls
wt down feat/tags
wt down main
```
