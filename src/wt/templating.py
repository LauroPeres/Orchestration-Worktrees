"""Resolve {{ }} values in worktree.yaml, per branch."""

import shlex
from typing import Any

from jinja2 import StrictUndefined, TemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from wt.errors import WtError
from wt.providers import REGISTRY

# Names reserved for providers that later versions add (docs/SPEC.md §10).
UPCOMING = {"postgres": "v0.2", "kafka": "v0.3", "redis": "v0.3", "keycloak": "v0.4"}

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
# `run:` commands go through `sh -c`: every rendered value is shell-quoted (D15).
_shell_env = SandboxedEnvironment(
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
    finalize=lambda v: shlex.quote(str(v)),
)


def render(value: str, context: dict[str, Any], where: str, quote: bool = False) -> str:
    """`quote=True` shell-quotes each `{{ }}` value; use it only for `run:` commands."""
    env = _shell_env if quote else _env
    try:
        return env.from_string(value).render(context)
    except UndefinedError as exc:
        hint = ""
        for name, version in UPCOMING.items():
            if name not in REGISTRY and f"'{name}'" in str(exc):
                hint = f" (the {name} provider arrives in {version})"
        known = ", ".join(sorted(context))
        raise WtError(f"{where}: {exc}{hint}. Available: {known}") from exc
    except TemplateError as exc:
        raise WtError(f"{where}: bad template {value!r}: {exc}") from exc
