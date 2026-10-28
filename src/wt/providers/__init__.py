"""Built-in providers. A plain registry; no entry points yet (docs/SPEC.md §14)."""

from collections.abc import Callable

from wt.config import Config
from wt.providers.base import Provider
from wt.providers.kafka import KafkaProvider
from wt.providers.keycloak import KeycloakProvider
from wt.providers.postgres import PostgresProvider
from wt.providers.redis import RedisProvider

Log = Callable[[str], None]
REGISTRY: dict[str, Callable[[Config, Log], Provider]] = {
    "kafka": KafkaProvider,
    "keycloak": KeycloakProvider,
    "postgres": PostgresProvider,
    "redis": RedisProvider,
}


def active(cfg: Config, log: Log) -> list[Provider]:
    """Registered providers that are configured under `shared:`."""
    return [REGISTRY[name](cfg, log) for name in cfg.shared.configured() if name in REGISTRY]
