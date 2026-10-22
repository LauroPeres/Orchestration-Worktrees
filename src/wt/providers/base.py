"""The provider interface (docs/SPEC.md §6, §14): a shared server, one slice per branch."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from wt.naming import Names
from wt.state import StateStore

Kind = Literal["compose", "run"]


@dataclass(frozen=True)
class BranchContext:
    names: Names
    branch: str
    base_branch: str
    state: StateStore


@dataclass(frozen=True)
class Resources:
    names: list[str]


@dataclass(frozen=True)
class Check:
    ok: bool
    detail: str
    warnings: list[str] = field(default_factory=list)


class Provider(Protocol):
    name: str

    def ensure_server(self) -> None:
        """Start the shared server if needed."""

    def create(self, ctx: BranchContext) -> Resources:
        """Create this branch's slice (database, group, prefix...). Must be idempotent."""

    def verify(self, ctx: BranchContext) -> Check:
        """Check the slice is usable, not just that a command returned."""

    def destroy(self, ctx: BranchContext) -> None:
        """Remove the slice. Safe on a half-created or missing one."""

    def template_vars(self, ctx: BranchContext, kind: Kind) -> dict[str, Any]:
        """Names for {{ }} in worktree.yaml; `kind` is the service kind that renders them."""
