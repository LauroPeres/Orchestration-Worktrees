---
name: runtime-adapters
description: How wt's runtime adapters are written — proc.run and Result/CommandError, git worktrees, docker compose, host processes, the Traefik file router, mkcert TLS, health probes, and the state store with atomic writes and file locks. Use when changing src/wt/{proc,git,compose,host,router,tls,health,state,projects}.py.
---

# Runtime adapters — wt

- **Commands:** `proc.run(args, check=True, ...) -> Result` (`returncode`, `stdout`, `stderr`, `.ok`);
  `check=True` raises `CommandError` (a `WtError` with the command and the output tail). Use
  `check=False` + `.ok` when failure is an expected answer (see `git.check_repo`).
- **Only `proc` and `host` import `subprocess`.** `host` owns long-lived processes (start, PID, logs).
- **Shape:** plain functions, typed dataclass results (`git.Worktree`), errors with the path or name
  that failed. No decisions about what the use case does next.
- **State:** `state.atomic_write(path, text)` (temp file + rename) under `state.file_lock(name)`;
  everything lives under `paths.wt_home()` (tests set `WT_HOME` to a temp dir automatically).
- **Router/TLS:** one Traefik route file per branch; certificates via mkcert when `tls.available()`.
- **Portability:** macOS, Linux and WSL2 — no Linux-only paths or flags without a fallback.

## Tests
Fake `proc.run` with `monkeypatch` (record calls, return `proc.Result`), use `tmp_path` for files;
`tests/test_git.py` uses real git in temp repos (`make_repo`). Never call Docker in unit tests.
