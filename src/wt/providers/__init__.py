"""Built-in providers. A plain registry; no entry points yet (docs/SPEC.md §14)."""

from collections.abc import Callable

from wt.config import Config
from wt.providers.base import Provider

REGISTRY: dict[str, Callable[[Config], Provider]] = {}


def active(cfg: Config) -> list[Provider]:
    """Registered providers that are configured under `shared:`."""
    return [REGISTRY[name](cfg) for name in cfg.shared.configured() if name in REGISTRY]
