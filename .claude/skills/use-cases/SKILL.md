---
name: use-cases
description: How wt's use cases are written in the core (orchestrator.py, doctor.py) — branch lock, state recording, idempotency, rollback, verification, and how they call runtime adapters and providers. Use when adding or changing up/add/stop/down/remove/ls/configure/doctor behavior.
---

# Use cases — wt core

## Shape (see `Orchestrator.add`)
1. Validate cheap things first, with no side effects (`git.check_branch_name`, `self.start_plan()`
   detects cycles, providers' `preflight`).
2. `with self._branch_lock(names):` — one writer per branch.
3. Load state (`self.store.load().branches.get(branch)`); decide from it, never from guesses.
4. Act **through the layers** (`git`, `compose`, `host`, `router`, providers).
5. `self._record(branch, change)` after each meaningful step; on failure record `failed`, then re-raise.

## Rules
- **Idempotent:** running it twice is safe (`test_up_twice_is_safe`); teardown is safe on a
  half-created branch and removes only what state or deterministic names say exists.
- **Verified:** a service is up when `health` says so (`_verify`), not when `compose up` returned.
- **Multi-project:** `self._for(branch)` returns the orchestrator that owns the branch — delegate.
- **No new long functions:** limits are enforced; new steps are small private methods with one job.
- The `doctor` checks return results; they don't fix anything.

## Tests
`orch` builds an orchestrator over temp git repos, `docker` fakes compose/router/health,
`fake_provider` fakes a provider (all in `tests/conftest.py`). Test the success path, the failure
path (state says `failed`, nothing leaked) and running it twice.
