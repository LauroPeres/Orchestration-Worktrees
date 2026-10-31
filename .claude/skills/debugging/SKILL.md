---
name: debugging
description: The fixer's procedure in wt — reproduce, locate the layer, decide fix vs send back, the module → owner map, where contracts and state live, and how to prove the fix. Use when diagnosing a failing check, a validate-code finding or an e2e bug here.
---

# Debugging — wt

1. **Reproduce** with the smallest command: one test (`uv run pytest -q <path> -k <name>`), or the
   e2e command from the report. No reproduction → `blocked` with what you tried.
2. **Locate** the layer from the traceback/finding (`architecture`). Where state lives: `WT_HOME`
   (tests: temp dir) → `<project>/state.json`, route files, logs (`Orchestrator.log_path`).
3. **Decide:**
   - Inside one module → `blocked`: "send to <owner>" + root cause + suggested fix (unless the owner already failed twice).
   - Between layers (a wrong contract, a caller misusing a callee) or owner failed twice → fix it.
4. **Fix** minimally, in the layer where the cause is, following that layer's skill (load it with `Skill`).
   Add a regression test that fails before the fix.
5. **Prove:** the regression test passes and all `checks` pass.

## Owners (`.claude/ownership.json`)
cli/tui → interface-coder · orchestrator/doctor/conftest → core-coder · providers/ → providers-coder ·
proc/git/compose/host/router/tls/health/state/projects → runtime-coder ·
config/catalog/naming/templating/paths/errors → model-coder · integration tests/examples → e2e.
