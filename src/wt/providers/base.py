"""The provider interface (docs/SPEC.md §6, §14): a shared server, one slice per branch."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from wt.config import Config
from wt.errors import WtError
from wt.naming import Names
from wt.projects import known_projects
from wt.state import StateStore

Kind = Literal["compose", "run"]
Log = Callable[[str], None]


@dataclass(frozen=True)
class BranchContext:
    names: Names
    branch: str
    base_branch: str
    state: StateStore
    urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resources:
    names: list[str]


@dataclass(frozen=True)
class Check:
    ok: bool
    detail: str
    warnings: list[str] = field(default_factory=list)


def branch_scope(ctx: BranchContext, separator: str) -> str:
    """`<project><sep><slug>`: the name every shared resource of a branch is built from.

    Neither part holds `.` or `:`, so with either as the separator two projects never produce
    the same scope.
    """
    return f"{ctx.names.project}{separator}{ctx.names.slug}"


def recorded_names(ctx: BranchContext, provider: str) -> list[str]:
    """What state recorded for this branch and provider at `create`; empty if it never got there.

    `destroy` deletes these together with today's deterministic names, because the config may
    have changed (or lost the section) since the slice was made.
    """
    recorded = ctx.state.load().branches.get(ctx.branch)
    return list(recorded.providers.get(provider, [])) if recorded is not None else []


def keeps_one_segment_prefix(
    prefix: str, separator: str, ctx: BranchContext, log: Callable[[str], None]
) -> bool:
    """True (and says so) when a prefix recorded by 0.5.0 may belong to a project's new names.

    0.5.0 recorded `<slug><sep>`; from 0.5.1 a name is `<project><sep><slug><sep>`. Deleting
    `app:` by plain prefix would also delete `app:main:...` of a project called `app`, so such a
    prefix is only deleted when its name is not a project wt knows (the current one included).
    A two-segment prefix is exact and never kept.
    """
    if prefix.count(separator) != 1:
        return False
    owner = prefix.removesuffix(separator)
    if owner != ctx.names.project and owner not in _known_project_names():
        return False
    log(f"kept {prefix}: it could belong to project {owner}; delete by hand if it is yours")
    return True


def _known_project_names() -> set[str]:
    # An index that cannot be read means "no other known projects", never a failed `down`.
    try:
        return {project.name for project in known_projects()}
    except (WtError, OSError):
        return set()


class Provider(Protocol):
    """What `wt` asks of a shared-infra provider, built-in or plugin.

    A plugin (docs/SPEC.md §23) is a class or factory registered under the entry-point group
    `wt.providers`; see `PluginFactory`. It reads its settings from `cfg.shared.extra[name]`.
    """

    name: str

    def ensure_server(self) -> None:
        """Start the shared server if needed."""

    def preflight(self, ctx: BranchContext) -> None:
        """Refuse a branch before `up` has any side effect (no state, worktree or container)."""

    def create(self, ctx: BranchContext) -> Resources:
        """Create this branch's slice (database, group, prefix...). Must be idempotent."""

    def verify(self, ctx: BranchContext) -> Check:
        """Check the slice is usable, not just that a command returned."""

    def destroy(self, ctx: BranchContext) -> None:
        """Remove the slice. Safe on a half-created or missing one."""

    def template_vars(self, ctx: BranchContext, kind: Kind) -> dict[str, Any]:
        """Names for {{ }} in the catalog; `kind` is the service kind that renders them."""


class PluginFactory(Protocol):
    """What a `wt.providers` entry point must load to: builds a provider and validates its config.

    `config_model` checks the mapping under the provider's `infra` key (`cfg.shared.extra[name]`)
    when the catalog is loaded, so a plugin can rely on a valid mapping in `__init__` (no I/O).
    """

    config_model: type[BaseModel]

    def __call__(self, cfg: Config, log: Log, /) -> Provider: ...
