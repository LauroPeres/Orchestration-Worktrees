"""User-facing sentences shared by more than one interface (CLI and TUI)."""

from collections.abc import Sequence

CONFIRM_REMOVE = (
    "remove {branch}: removes what wt created for it (containers, volumes, databases, "
    "worktrees) and deletes the git branch in every repo of the project where it exists; "
    "unpushed commits are lost. Remove? y/N"
)
NO_SERVICES_YET = (
    "branch {branch} has no services yet; run wt add {branch} --uses {example} (services: {names})"
)
MAX_LISTED_SERVICES = 10


def no_services_message(branch: str, names: Sequence[str]) -> str:
    """`NO_SERVICES_YET` for a catalog's service names (not empty): the first one is the example."""
    listed = ", ".join(names[:MAX_LISTED_SERVICES])
    if len(names) > MAX_LISTED_SERVICES:
        listed += ", …"
    return NO_SERVICES_YET.format(branch=branch, example=names[0], names=listed)
