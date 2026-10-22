"""Resolve {{ }} values in worktree.yaml, per branch."""

from typing import Any

from jinja2 import StrictUndefined, TemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from wt.errors import WtError

# Names reserved for providers that later versions add (docs/SPEC.md §10).
UPCOMING = {"postgres": "v0.2", "kafka": "v0.3", "redis": "v0.3", "keycloak": "v0.4"}

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


def render(value: str, context: dict[str, Any], where: str) -> str:
    try:
        return _env.from_string(value).render(context)
    except UndefinedError as exc:
        hint = ""
        for name, version in UPCOMING.items():
            if f"'{name}'" in str(exc):
                hint = f" (the {name} provider arrives in {version})"
        known = ", ".join(sorted(context))
        raise WtError(f"{where}: {exc}{hint}. Available: {known}") from exc
    except TemplateError as exc:
        raise WtError(f"{where}: bad template {value!r}: {exc}") from exc
