"""Providers: the built-in registry plus plugins from entry points (docs/SPEC.md §14, §23)."""

from collections.abc import Callable

from pydantic import ValidationError

from wt.config import Config
from wt.errors import WtError
from wt.providers.base import Log, PluginFactory, Provider
from wt.providers.kafka import KafkaProvider
from wt.providers.keycloak import KeycloakProvider
from wt.providers.plugins import ENTRY_POINT_GROUP, Plugins, load_plugins, reset_plugins
from wt.providers.postgres import PostgresProvider
from wt.providers.redis import RedisProvider

__all__ = [
    "ENTRY_POINT_GROUP",
    "REGISTRY",
    "Log",
    "Plugins",
    "active",
    "load_plugins",
    "plugin_problems",
    "provider_names",
    "reset_plugins",
    "validate_infra",
]

REGISTRY: dict[str, Callable[[Config, Log], Provider]] = {
    "kafka": KafkaProvider,
    "keycloak": KeycloakProvider,
    "postgres": PostgresProvider,
    "redis": RedisProvider,
}


def active(cfg: Config, log: Log) -> list[Provider]:
    """Providers configured under `shared:`: the built-in ones, then one per plugin key.

    A plugin key whose plugin is missing or broken raises: skipping it would leave
    its slices behind.
    """
    built_in = [REGISTRY[name](cfg, log) for name in cfg.shared.configured() if name in REGISTRY]
    return built_in + [_plugin_factory(key)(cfg, log) for key in cfg.shared.extra]


def provider_names() -> list[str]:
    """Names of the providers wt can use: built-ins sorted, then installed plugins sorted."""
    return [*sorted(REGISTRY), *sorted(load_plugins().factories)]


def plugin_problems() -> dict[str, str]:
    """Why each installed plugin cannot be used, by entry-point name (for `wt doctor`)."""
    return dict(load_plugins().problems)


def validate_infra(cfg: Config) -> None:
    """Check every non-built-in `infra` key against its plugin and the plugin's `config_model`.

    Only a project that has such keys imports plugins.
    """
    for key, raw in cfg.shared.extra.items():
        try:
            _plugin_factory(key).config_model.model_validate(raw)
        except ValidationError as error:
            raise WtError(f"infra {key}: {_one_line(error)}") from error


def _plugin_factory(key: str) -> PluginFactory:
    plugins = load_plugins()
    if key in plugins.problems:
        raise WtError(plugins.problems[key])
    if key not in plugins.factories:
        installed = ", ".join(provider_names())
        raise WtError(f"unknown infra {key!r} (installed providers: {installed})")
    return plugins.factories[key]


def _one_line(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, item['loc']))}: {item['msg']}" if item["loc"] else item["msg"]
        for item in error.errors()
    )
