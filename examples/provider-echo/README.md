# wt-provider-echo

A minimal plugin provider for `wt` (docs/SPEC.md §23). It appends `create <slug>` and
`destroy <slug>` lines to a file; `verify` passes while the file exists.

## Writing a plugin
1. Make a package that depends on `wt`. Write a class with the `Provider` protocol
   (`wt.providers.base`): `ensure_server`, `preflight`, `create`, `verify`, `destroy`,
   `template_vars`, and a `name`.
2. Give it `config_model`, a pydantic model for the `infra` mapping. `wt` validates the catalog
   with it; the provider reads the same mapping from `cfg.shared.extra[name]` in `__init__`
   (which must do no I/O).
3. Register it in `pyproject.toml`; the entry-point name is the `infra` key:
   `[project.entry-points."wt.providers"]` then `echo = "wt_provider_echo:EchoProvider"`.
4. Install it into the same environment as `wt` (`pip install -e .`) and use it in `catalog.yaml`:
   `infra: {echo: {file: /tmp/echo.log}}`. `wt doctor` shows plugins that fail to load.

`create` and `destroy` must be idempotent and safe on a missing slice. `create` returns the
names it made; `wt` records them in the branch state. `{{ echo.file }}` is available to services
from `template_vars`.
