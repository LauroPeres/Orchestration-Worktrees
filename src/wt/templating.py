"""Resolve {{ }} values in worktree.yaml, per branch."""

import re
import shlex
from typing import Any

from jinja2 import StrictUndefined, TemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from wt.errors import WtError
from wt.providers import REGISTRY

# Names reserved for providers that later versions add (docs/SPEC.md §10).
UPCOMING = {"postgres": "v0.2"}

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
# `run:` commands go through `sh -c`: every rendered value is shell-quoted (D15).
_shell_env = SandboxedEnvironment(
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
    finalize=lambda v: shlex.quote(str(v)),
)


def _hyphen_hint(value: str, context: dict[str, Any]) -> str:
    """Point at the subscript form when a template reads `services.<hyphenated name>`."""
    names = context.get("services")
    if not isinstance(names, dict):
        return ""
    written = {m.group(1) for m in re.finditer(r"services\.([A-Za-z0-9_-]+)", value)}
    hyphenated = sorted(n for n in names if "-" in n and n in written)
    return "".join(
        f" (a hyphenated service name needs brackets: use services['{n}'].url)" for n in hyphenated
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
        hint += _hyphen_hint(value, context)
        known = ", ".join(sorted(context))
        raise WtError(f"{where}: {exc}{hint}. Available: {known}") from exc
    except TemplateError as exc:
        raise WtError(f"{where}: bad template {value!r}: {exc}") from exc
