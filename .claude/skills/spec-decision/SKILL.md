---
name: spec-decision
description: Record or change a design decision in docs/SPEC.md before changing code. Use when a task needs behavior the spec doesn't cover, contradicts an implementation-notes section, moves work between roadmap versions (§10), or resolves an open question (§12). Main session (planner) only — agents report the need instead.
---

# SPEC decision — wt

`docs/SPEC.md` is the source of truth. Code follows the spec, not the other way round.

1. Read `docs/SPEC.md`: §10 (roadmap), §12 (open questions) and the implementation-notes section
   of the version being built (§13 v0.1 … §19 v0.5; a new version gets a new section).
2. Find what the task needs that the spec doesn't say, or says differently.
3. Scope: work that belongs to a later roadmap version → stop and ask the user; never pull it forward.
4. Draft the change as a short bullet in the right section, in its existing style (bold topic, then
   a concrete, testable rule: paths, names, defaults, error behavior). Resolved open question → §12
   with the date (YYYY-MM-DD). Scope move → the §10 table.
5. Show the user the proposed text and wait for approval (in a `plan.md` task, record it in the
   changes log too).
6. Then make code and tests match it; mention the section in the commit message.

Keep it generic: no stack-specific services, nothing from any employer.
