"""Load and validate worktree.yaml."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from wt.errors import WtError

CONFIG_NAME = "worktree.yaml"
MAX_SERVICES = 100

NAME = r"^[a-z0-9][a-z0-9_-]{0,30}$"
PROJECT = r"^[a-z][a-z0-9-]{0,30}$"
LABEL = r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)?$"  # one DNS label, or "" for the root
DOMAIN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"

# realm and client id: no "/" (recorded as "<realm>/<id>"), no leading "-" or "." (not an option
# or a "." / ".." path segment for kcadm)
KEYCLOAK_NAME = r"^[A-Za-z0-9_][A-Za-z0-9._-]*$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Service(Strict):
    repo: str
    compose: str | None = Field(None, description="compose file, relative to the repo root")
    compose_service: str | None = Field(None, description="name in the compose file, if different")
    run: str | None = Field(None, description="host process command (v0.2)")
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
    realm: str = Field(pattern=KEYCLOAK_NAME)
    client: str = Field(pattern=KEYCLOAK_NAME)


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
    def _check(self) -> Self:
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
    catalog: Path | None = None
    remotes: dict[str, str] = field(default_factory=dict)
    uses: tuple[str, ...] | None = None  # the selection; None for a `services:` project
    service_order: tuple[str, ...] = ()  # catalog order (or `services:` order): the port index

    @property
    def root(self) -> Path:
        return self.path.parent

    def repo_path(self, repo: str) -> Path:
        return (self.root / self.config.repos[repo]).resolve()

    def service_index(self, name: str) -> int:
        """Position of a service in the catalog (whatever the selection) or in `services:`."""
        try:
            return self.service_order.index(name)
        except ValueError:
            raise WtError(f"unknown service {name!r}") from None

    def with_uses(self, uses: list[str]) -> "LoadedConfig":
        """The same worktree.yaml and catalog, resolved for another selection."""
        if self.uses is None:
            raise WtError("configure needs a catalog project (uses: in worktree.yaml)")
        if not uses:
            raise WtError("select at least one service")
        return load_config(self.path, uses=list(uses))


def find_config(start: Path) -> Path | None:
    for folder in [start, *start.parents]:
        candidate = folder / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(root)"
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"  {where}: {msg}")
    return "\n".join(lines)


def load_config(path: Path, uses: list[str] | None = None) -> LoadedConfig:
    """`uses` replaces the `uses:` of the file (a per-worktree selection)."""
    path = path.resolve()
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise WtError(f"config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise WtError(f"{path} is not valid YAML:\n{exc}") from exc
    if not isinstance(raw, dict):
        raise WtError(f"{path} must contain a YAML mapping")
    catalog: Path | None = None
    remotes: dict[str, str] = {}
    selection: tuple[str, ...] | None = None
    order: tuple[str, ...] = ()
    if ("services" in raw) == ("uses" in raw):
        raise WtError(f"invalid {path.name}: set exactly one of 'services' or 'uses'")
    if uses is not None and "uses" not in raw:
        raise WtError(f"{path.name} has no 'uses:'; a selection needs a catalog project")
    if "uses" in raw:
        from wt.catalog import catalog_path, resolve  # catalog imports this module

        if uses is not None:
            raw = {**raw, "uses": uses}
        catalog = catalog_path(raw.get("catalog"), path.parent)
        selected = raw["uses"]
        raw, remotes, order = resolve(raw, path)
        selection = tuple(dict.fromkeys(selected))
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise WtError(f"invalid {path.name}:\n{_format_errors(exc)}") from exc
    if catalog is None:
        order = tuple(config.services)
        if len(order) > MAX_SERVICES:
            raise WtError(f"{path.name} has {len(order)} services; at most 100 services")
    return LoadedConfig(
        config=config,
        path=path,
        catalog=catalog,
        remotes=remotes,
        uses=selection,
        service_order=order,
    )


STARTER = """\
# worktree.yaml - describe your stack once; `wt up <branch>` runs a copy of it per branch.
project: {project}
domain: {project}.localhost      # *.localhost resolves to 127.0.0.1 in browsers

repos:                           # one or many repos; each gets a git worktree per branch
  app: .

services:
  app:
    repo: app
    compose: docker-compose.yml  # compose file inside the repo
    port: 8000                   # port the container listens on (routed by Traefik)
    subdomain: ""                # "" = https://<branch>.{project}.localhost
    health: /                    # path probed after `up`
    env:
      PUBLIC_URL: "{{{{ url }}}}"
# --- examples: uncomment to try (host process and shared Postgres) ---
#   web:
#     repo: app
#     run: npm run dev -- --host 0.0.0.0 --port {{{{ port }}}}  # 0.0.0.0 so Traefik reaches it
#     port: auto                  # a free port per branch, kept for that branch
#     env:
#       DATABASE_URL: "{{{{ postgres.url('app') }}}}"
# shared:
#   postgres:
#     databases: [app]
#     strategy: template          # template | dump-restore
#     verify: true                # compare row counts after clone
#     base_branch: main
"""
