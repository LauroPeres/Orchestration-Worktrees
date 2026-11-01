# Windows: what fails today

wt is **not supported on native Windows**. Use WSL2 (README, Requirements). This page is a map of the gap,
produced by a report-only CI job (`windows` in `.github/workflows/ci.yml`, windows-latest, Python 3.12, the
unit tests without Docker). It never blocks a merge.

## Result

- **First blocker:** `import wt` fails on Windows. `src/wt/state.py` imports `fcntl` (POSIX file locks), so
  the test suite cannot even be collected: `ModuleNotFoundError: No module named 'fcntl'`.
- **With `fcntl` replaced by a no-op stub** (`.github/windows-stubs/fcntl.py`, report only, so locking is *not*
  tested): 1312 passed, 153 failed, 103 errors, 1 skipped in 9.5 minutes (CI run of the v0.8.1 PR, 2026-11-01).
  The Docker tests are not part of this job.

## By area (counts are failing tests; the error text is the first line CI printed)

| Area | Failures | What fails | Where in the code |
|---|---|---|---|
| File locks | everything, before the stub | `ModuleNotFoundError: No module named 'fcntl'` | `state.py` (`file_lock`, `ports_lock`) |
| Host processes: reaping | 72 | `AttributeError: module 'os' has no attribute 'WNOHANG'` | `host.py` (`os.waitpid(…, WNOHANG)`) |
| Host processes: groups and signals | 15 in `test_host.py`, 4 in `test_proc.py`, 13 teardown errors | process groups (`start_new_session`, `os.killpg`, `os.getpgid`), SIGTERM/SIGKILL semantics, `/proc/<pid>/stat` and `ps -o lstart` for the start time, a grandchild timeout test that takes 30 s instead of 10 | `host.py`, `proc.py` |
| `sh`, `bash`, `ps` | 8 + 2 | `OSError: [WinError 193] %1 is not a valid Win32 application` (running `bootstrap.sh`); `sh -c "…; ps -o args="` fails | `examples/*/bootstrap.sh`, tests, anything run through `sh -c` (`run:`, `setup:`) |
| Paths | 50 + 6 + 4 | tests write Windows paths into YAML and `catalog.yaml is not valid YAML`; `WindowsPath('C:/abs/c.yaml') == WindowsPath('/abs/c.yaml')` (a POSIX absolute path is not absolute on Windows); the same in `~` expansion | tests (`write_global_catalog`), `catalog.py` path handling, `cli.py` |
| Permissions on files | 8 | `assert 438 == 384`: the password files cannot be `0600` (Windows reports `0666`); doctor says `file mode is 0666, expected 0600` | `providers/postgres.py` password file, `providers/server.py`, `doctor.py` |
| Read-only and open files | 4 | `PermissionError: [WinError 5]` while removing a directory | `git.remove_worktree`, temp cleanup |
| Symlinks and separators | 6 | `file:..` links in copied `node_modules` trees compare unequal; links pointing outside are not links | `cache.py` (restore keeps symlinks) |
| `multiprocessing` | 2 | `ValueError: cannot find context for 'fork'` | tests of the concurrent password and state edits |
| Messages that name the OS | 3 | regexes written for POSIX error text (`not found on PATH`, missing folder) | `proc.py` tests |
| Network probe | 1 | `TimeoutError: timed out` against a closed local port (no immediate refusal) | `health.py` |

## Not testable here, known to be POSIX-specific

- **mkcert trust:** `mkcert -install` and the trust stores differ on Windows.
- **Traefik mounts:** the compose file mounts `~/.wt` paths; Docker Desktop path translation on Windows is not
  exercised.
- **`host.docker.internal`:** the host-process route relies on the bridge gateway on Linux; Docker Desktop
  resolves it differently.
- **`bash -c` scripts** inside containers (Keycloak, Kafka) run in Linux containers and are unaffected; the
  host-side `docker exec` argument quoting through `cmd.exe`/PowerShell was not tried.
- **File locks:** the stub hides every locking problem; a real replacement (for example `msvcrt.locking`) is
  the first thing a port would need.

## If this is ever pursued

1. Replace `fcntl` with a small lock module (`msvcrt.locking` on Windows).
2. Replace process groups with Job Objects (or `CREATE_NEW_PROCESS_GROUP` + `taskkill /T`) in `host.py` and
   `proc.py`, and the `/proc`/`ps` start-time check with `psutil`-style creation time.
3. Run `run:`/`setup:` through a configurable shell (`cmd /c`, PowerShell or Git Bash) instead of `sh -c`.
4. Make paths in YAML and in tests OS-independent (use `Path` throughout, quote Windows paths).
5. Decide whether WSL2 is good enough: it already works today.
