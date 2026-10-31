---
name: review-wt
description: validate-code's checklist for a wt diff — the modularization, correctness and repo-rules lenses, what to look for and how to report findings. Use when reviewing a diff in this repo.
---

# Review checklist — wt

Review only the diff (`git diff <base>...HEAD`, plus the files it touches). Use the
`code-framework:clean-code` checklist, then the lens the planner named:

## Lens: modularization
- Each changed file is in the right layer (`architecture`); no logic in cli/tui, no subprocess or
  file writes in core, no I/O in model, no provider importing another.
- `git diff --name-only` matches the step's owner (`.claude/ownership.json`).
- New functions within the limits; no new `# noqa`, `ignore_imports` or raised thresholds.
- `orchestrator.py` didn't grow a new long method; abstractions only at boundaries.

## Lens: correctness
- Idempotency: running twice, `down` on a half-created branch, a crash between steps (is state recorded?).
- Locks: every state mutation under `_branch_lock`/`file_lock`; atomic writes.
- Deletion safety: only names from state or deterministic naming, validated before deleting.
- Verification: "done" means probed healthy, not "command returned".
- Shell safety: `run:` values quoted; no user string interpolated into a shell.
- Errors: `WtError` with an actionable message; nothing swallowed. Tests cover the failure path.

## Lens: repo rules
- Behavior matches `docs/SPEC.md` (or the diff updates it via `spec-decision`); within the roadmap version (§10).
- Clean room: nothing from any employer (names, hosts, domains, metrics). Generic: no user stacks
  outside `examples/`.
