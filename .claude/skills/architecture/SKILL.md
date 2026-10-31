---
name: architecture
description: wt's architecture — the five layers (interface, core, providers, runtime, model), the dependency rules and how import-linter and ruff enforce them, the module → owner map, the known exceptions with their migration direction, and the best real code to copy. Use before changing code in any layer of src/wt/ or reviewing a diff here.
---

# Architecture — wt

## Layers (a layer may import any layer below it, never above)
```
interface   cli, tui, __main__                      parse input, call the core, render
   ↓
core        orchestrator, doctor                    use cases: sequence, lock, record, roll back
   ↓
providers   providers/ (base = Provider protocol)   shared servers, one slice per branch
   ↓
runtime     proc, git, compose, host, router, tls,  adapters over the OS and external tools
            health, state, projects
   ↓
model       config, catalog, naming, templating,    pure rules: schema, names, ports, templates
            paths, errors
```
| Layer | Must never |
|---|---|
| interface | decide what happens (loops over branches, state reads, retries) — that's a core method |
| core | run subprocesses, write files or know a provider's internals — it calls the layer that does |
| providers | import another provider (shared code → `base.py`/`server.py`) or the core |
| runtime | make use-case decisions; call `subprocess` outside `proc`/`host` |
| model | do I/O (except reading the config file), import anything above it |

**Enforced** by import-linter (`[tool.importlinter]` in `pyproject.toml`: a layers contract + provider
independence) and ruff limits: complexity ≤ 10, ≤ 5 args, ≤ 12 branches, ≤ 50 statements per
function. Both run in CI, in the checks and in the post-edit hook. Never raise a limit or add an
`ignore_imports`/`# noqa` to pass — split the code or move it to the right layer.

## Owners — `.claude/ownership.json` is the source of truth
interface-coder · core-coder (+ `tests/conftest.py`) · providers-coder · runtime-coder · model-coder;
tests `tests/test_<module>.py` go with their module; e2e owns `tests/integration/`, `tests/test_example_*`, `examples/`.

## Best real code — copy these
- Command: `src/wt/cli.py` `down`/`add`/`stop` (one core call, print the outcome).
- Use case: `Orchestrator.add` / `Orchestrator.stop` in `src/wt/orchestrator.py` (lock → load state
  → act through the layers → `_record`; failure recorded before re-raising).
- Provider: `src/wt/providers/redis.py` (no I/O in `__init__`, verify by a real probe, destroy safe on
  missing slices, template vars per service kind).
- Adapter: `src/wt/git.py` (`proc.run` + typed results + `WtError` with the path).
- Model: `src/wt/naming.py` (pure, deterministic, documented limits as named constants).
- Tests: `tests/test_redis.py` (fake `proc`), `tests/test_orchestrator.py` with `orch` + `docker`
  fixtures from `tests/conftest.py`, `tests/test_naming.py` (pure).

## Known exceptions (migrate gradually, never add more)
1. `templating → providers` (`ignore_imports`): the "provider arrives in vX" hint reads the registry.
   Target: the core passes the hint in, or drop it (it no longer fires) — needs a `spec-decision`.
2. Functions over the limits, marked `# noqa: <code>` on the `def` line:
   `compose.up`, `config.Config._check`, `config.load_config`,
   `orchestrator.Orchestrator._start_steps`, `._up_run_service`, `._up_compose_group`, `._down_locked`,
   `._drop_services`, `proc.run`, `proc._run_streaming`. Remove the `noqa` when a task splits one.
3. `orchestrator.py` (1,087 lines) holds every use case in one class (SRP). Target: one module per
   use-case group (lifecycle up/add/stop, teardown down/remove, views ls/services/logs, configure),
   sharing a small context — a planned refactor task, never inside a feature.
4. Layers are module lists in a flat package. Target, with 3: `src/wt/{interface,core,providers,runtime,model}/`.

Changing the architecture goes through the plan, never through an agent quietly mixing styles.
