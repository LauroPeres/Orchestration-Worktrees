"""The resolved project model: services, shared servers and the loaded selection."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from wt.errors import WtError

MAX_SERVICES = 100

NAME = r"^[a-z0-9][a-z0-9_-]{0,30}$"
PROJECT = r"^[a-z][a-z0-9-]{0,30}$"
LABEL = r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)?$"  # one DNS label, or "" for the root
DOMAIN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"

# realm and client id: no "/" (recorded as "<realm>/<id>"), no leading "-" or "." (not an option
# or a "." / ".." path segment for kcadm)
KEYCLOAK_NAME = r"^[A-Za-z0-9_][A-Za-z0-9._-]*$"
# Keycloak rejects a realm of ~251+ characters (server error) and a client id of 251+
# (`unknown_error`); 64 is far below that and plenty for a name.
MAX_KEYCLOAK_NAME = 64

DEFAULT_SETUP_TIMEOUT = 600  # seconds
MAX_SETUP_TIMEOUT = 3600


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Service(Strict):
    repo: str
    compose: str | None = Field(None, description="compose file, relative to the repo root")
    compose_service: str | None = Field(None, description="name in the compose file, if different")
    run: str | None = Field(None, description="host process command (v0.2)")
    setup: str | None = Field(
        None, description="shell command run before the process starts, once per change (run only)"
    )
    setup_timeout: int = Field(
        DEFAULT_SETUP_TIMEOUT,
        ge=1,
        le=MAX_SETUP_TIMEOUT,
        description="seconds `setup` may take (run only)",
    )
    port: int | Literal["auto"] | None = None
    subdomain: str | None = Field(
        None, pattern=LABEL, description='"" = branch root; unset = no route'
    )
    health: str = "/"
    env: dict[str, str] = Field(default_factory=dict)
    needs: list[str] = Field(
        default_factory=list, description="services that must be healthy before this one starts"
    )

    @model_validator(mode="after")
    def _check_setup(self) -> Self:
        if self.run is None:
            if self.setup is not None:
                raise ValueError("setup: needs run:")
            if "setup_timeout" in self.model_fields_set:
                raise ValueError("setup_timeout: needs run:")
        return self

    @model_validator(mode="after")
    def _check(self) -> Self:
        if (self.compose is None) == (self.run is None):
            raise ValueError("set exactly one of 'compose' or 'run'")
        if self.compose is not None and self.port == "auto":
            raise ValueError(
                "'port: auto' is only for 'run' services; compose services need the "
                "container's internal port"
            )
        if self.subdomain is not None and self.port is None:
            raise ValueError("a routed service (with 'subdomain') needs a 'port'")
        if isinstance(self.port, int) and not 1 <= self.port <= 65535:
            raise ValueError("'port' must be between 1 and 65535")
        if not self.health.startswith("/"):
            raise ValueError("'health' must be a path starting with '/'")
        return self


class Postgres(Strict):
    databases: list[str]
    strategy: Literal["template", "dump-restore"] = "template"
    verify: bool = True
    base_branch: str = "main"


class Kafka(Strict):
    groups: list[str]
    topic_prefix: bool = False


class Redis(Strict):
    prefix: bool = True


class Keycloak(Strict):
    realm: str = Field(pattern=KEYCLOAK_NAME, max_length=MAX_KEYCLOAK_NAME)
    client: str = Field(pattern=KEYCLOAK_NAME, max_length=MAX_KEYCLOAK_NAME)


class Shared(Strict):
    postgres: Postgres | None = None
    kafka: Kafka | None = None
    redis: Redis | None = None
    keycloak: Keycloak | None = None

    def configured(self) -> list[str]:
        return [name for name in type(self).model_fields if getattr(self, name) is not None]


class Config(Strict):
    project: str = Field(pattern=PROJECT)
    domain: str = Field(pattern=DOMAIN)
    repos: dict[str, str] = Field(min_length=1)
    services: dict[str, Service] = Field(min_length=1)
    shared: Shared = Field(default_factory=Shared)

    @model_validator(mode="after")
    def _check(self) -> Self:  # noqa: C901, PLR0912
        for name in [*self.repos, *self.services]:
            if not re.match(NAME, name):
                raise ValueError(f"name {name!r} must match {NAME}")
        for name, svc in self.services.items():
            if svc.repo not in self.repos:
                raise ValueError(f"service {name!r} uses unknown repo {svc.repo!r}")
        compose_files: dict[str, str] = {}
        for svc in self.services.values():
            if svc.compose is None:
                continue
            seen = compose_files.setdefault(svc.repo, svc.compose)
            if seen != svc.compose:
                raise ValueError(
                    f"repo {svc.repo!r} uses two compose files ({seen}, "
                    f"{svc.compose}); v0.1 supports one compose file per repo"
                )
        hosts: dict[str, str] = {}
        for name, svc in self.services.items():
            if svc.subdomain is None:
                continue
            if svc.subdomain in hosts:
                raise ValueError(
                    f"services {hosts[svc.subdomain]!r} and {name!r} use the same "
                    f"subdomain {svc.subdomain!r}"
                )
            hosts[svc.subdomain] = name
        for name, svc in self.services.items():
            for dep in svc.needs:
                if dep not in self.services:
                    raise ValueError(f"service {name!r} needs unknown service {dep!r}")
        cycle = find_cycle({n: s.needs for n, s in self.services.items()})
        if cycle:
            raise ValueError("services need each other in a cycle: " + " -> ".join(cycle))
        return self


def find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """Return a dependency cycle as [a, b, ..., a], or None."""
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for dep in edges.get(node, []):
            if state.get(dep) == 1:
                return [*path[path.index(dep) :], dep]
            if dep not in state and (found := visit(dep)):
                return found
        path.pop()
        state[node] = 2
        return None

    for name in edges:
        if name not in state and (found := visit(name)):
            return found
    return None


@dataclass(frozen=True)
class LoadedConfig:
    config: Config
    path: Path
    catalog: Path
    uses: tuple[str, ...]  # the explicit selection (its closure is in `config.services`)
    remotes: dict[str, str] = field(default_factory=dict)
    service_order: tuple[str, ...] = ()  # catalog order: the port index
    warnings: tuple[str, ...] = ()  # non-fatal notes from loading, for the caller to show
    worktrees_dir: Path | None = None  # the catalog's `worktrees` folder, resolved; None = default

    @property
    def root(self) -> Path:
        return self.path.parent

    def repo_path(self, repo: str) -> Path:
        return (self.root / self.config.repos[repo]).resolve()

    def service_index(self, name: str) -> int:
        """Position of a service in the catalog, whatever the selection."""
        try:
            return self.service_order.index(name)
        except ValueError:
            raise WtError(f"unknown service {name!r}") from None

    def with_uses(self, uses: list[str]) -> "LoadedConfig":
        """The same project catalog, resolved for another selection."""
        if not uses:
            raise WtError("select at least one service")
        from wt.catalog import load_project  # catalog imports this module

        return load_project(self.path, uses=list(uses))


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(root)"
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"  {where}: {msg}")
    return "\n".join(lines)
