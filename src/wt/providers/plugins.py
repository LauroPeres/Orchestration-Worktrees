"""Plugin providers found through entry points (docs/SPEC.md §23).

Nothing is imported until `load_plugins` runs, and callers only run it for a project that has a
non-built-in `infra` key (or for `wt doctor`), so a project without plugins pays nothing.
"""

from dataclasses import dataclass, field
from functools import cache
from importlib import metadata
from typing import cast

from pydantic import BaseModel

from wt.config import BUILTIN_INFRA
from wt.providers.base import PluginFactory

ENTRY_POINT_GROUP = "wt.providers"
RESERVED_NAME = "extra"  # the `Shared` field that holds the plugin sections


@dataclass(frozen=True)
class Plugins:
    factories: dict[str, PluginFactory] = field(default_factory=dict)
    problems: dict[str, str] = field(default_factory=dict)  # name -> short reason, shown as is


class _LoadError(Exception):
    """Why one entry point cannot be used; becomes its `problems` entry."""


@cache
def load_plugins() -> Plugins:
    """Import every installed `wt.providers` entry point, once per process."""
    factories: dict[str, PluginFactory] = {}
    problems: dict[str, str] = {}
    for entry_point in sorted(metadata.entry_points(group=ENTRY_POINT_GROUP), key=_name):
        name = entry_point.name
        if name in BUILTIN_INFRA:
            problems[name] = f"provider {name!r} is built in"
        elif name == RESERVED_NAME:
            problems[name] = f"provider {name!r} is reserved"
        else:
            try:
                factories[name] = _load(entry_point)
            except _LoadError as error:
                problems[name] = f"provider plugin {name!r} failed to load: {error}"
    return Plugins(factories, problems)


def reset_plugins() -> None:
    """Forget what was loaded (tests)."""
    load_plugins.cache_clear()


def _name(entry_point: metadata.EntryPoint) -> str:
    return entry_point.name


def _load(entry_point: metadata.EntryPoint) -> PluginFactory:
    try:
        loaded = entry_point.load()
    except Exception as error:  # third-party code: whatever it raises is its problem, not a crash
        raise _LoadError(f"{type(error).__name__}: {error}") from error
    model = getattr(loaded, "config_model", None)
    if not callable(loaded) or not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise _LoadError("it is not callable or has no `config_model`")
    return cast(PluginFactory, loaded)
