"""The user catalog: reusable repos and services that a worktree.yaml picks with `uses:`."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import Field, ValidationError, model_validator

from wt.config import MAX_SERVICES, NAME, Service, Shared, Strict, _format_errors, find_cycle
from wt.errors import WtError
from wt.naming import service_ref
from wt.paths import wt_home

CATALOG_NAME = "catalog.yaml"
ENV_VAR = r"^[A-Z_][A-Z0-9_]*$"


class CatalogService(Service):
    infra: Shared = Field(default_factory=Shared, description="what it needs; merged into shared")
    consumes: dict[str, str] = Field(
        default_factory=dict, description="service name -> env var that receives its URL"
    )

    remote: str | None = Field(
        default=None, description="URL of this service when it is not in the stack (optional)"
    )

    @model_validator(mode="after")
    def _check_consumes(self) -> Self:
        if self.remote is not None and not re.match(r"^https?://\S+$", self.remote):
            raise ValueError("remote: must be an http:// or https:// URL")
        for var in self.consumes.values():
            if not re.match(ENV_VAR, var):
                raise ValueError(f"consumes: {var!r} is not an environment variable name")
        if len(set(self.consumes.values())) < len(self.consumes):
            raise ValueError("consumes: two services use the same environment variable")
        return self


class Catalog(Strict):
    repos: dict[str, str] = Field(default_factory=dict)
    services: dict[str, CatalogService] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name in [*self.repos, *self.services]:
            if not re.match(NAME, name):
                raise ValueError(f"name {name!r} must match {NAME}")
        for name, value in self.repos.items():
            if not value.strip():
                raise ValueError(f"repo {name!r} has an empty path")
        for name, svc in self.services.items():
            if svc.repo not in self.repos:
                raise ValueError(f"service {name!r} uses unknown repo {svc.repo!r}")
            for dep in svc.consumes:
                if dep == name:
                    raise ValueError(f"service {name!r} cannot consume itself")
                if dep not in self.services:
                    raise ValueError(f"service {name!r} consumes unknown service {dep!r}")
        return self


@dataclass(frozen=True)
class LoadedCatalog:
    catalog: Catalog
    path: Path
    repos: dict[str, Path]  # absolute paths


def catalog_path(explicit: str | None, config_dir: Path | None) -> Path:
    """`catalog:` from worktree.yaml (relative to its folder), then WT_CATALOG, then the default."""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() or config_dir is None else config_dir / path
    env = os.environ.get("WT_CATALOG")
    if env:
        return Path(env).expanduser()
    return wt_home() / CATALOG_NAME


@dataclass(frozen=True)
class Row:
    service: str
    repo: str
    kind: str  # compose | run
    infra: list[str]
    consumes: list[str]
    consumed_by: list[str]
    remote: str | None


def rows(cat: Catalog) -> list[Row]:
    """One row per catalog service, sorted by name, for `wt catalog`."""
    return [
        Row(
            service=name,
            repo=svc.repo,
            kind="compose" if svc.compose is not None else "run",
            infra=svc.infra.configured(),
            consumes=list(svc.consumes),
            consumed_by=sorted(n for n, other in cat.services.items() if name in other.consumes),
            remote=svc.remote,
        )
        for name, svc in sorted(cat.services.items())
    ]


def load_catalog(path: Path) -> LoadedCatalog:
    path = path.resolve()
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise WtError(f"catalog not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise WtError(f"{path} is not valid YAML:\n{exc}") from exc
    if not isinstance(raw, dict):
        raise WtError(f"{path} must contain a YAML mapping")
    try:
        catalog = Catalog.model_validate(raw)
    except ValidationError as exc:
        raise WtError(f"invalid {path.name}:\n{_format_errors(exc)}") from exc
    repos = {}
    for name, value in catalog.repos.items():
        repo = Path(value).expanduser()
        repos[name] = repo if repo.is_absolute() else (path.parent / repo).resolve()
    return LoadedCatalog(catalog=catalog, path=path, repos=repos)


def _merge_infra(services: dict[str, CatalogService]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    owner: dict[tuple[str, str], str] = {}
    for name, svc in services.items():
        for provider in svc.infra.configured():
            conf = getattr(svc.infra, provider)
            target = merged.setdefault(provider, {})
            for key in conf.model_fields_set:
                value = getattr(conf, key)
                if isinstance(value, list):
                    target[key] = [
                        *target.get(key, []),
                        *(v for v in value if v not in target.get(key, [])),
                    ]
                    continue
                if key in target and target[key] != value:
                    raise WtError(
                        f"infra {provider}.{key} conflicts: service {owner[provider, key]!r} "
                        f"sets {target[key]!r}, service {name!r} sets {value!r}"
                    )
                target[key] = value
                owner[provider, key] = name
    return merged


def expand_uses(catalog: Catalog, uses: list[str]) -> list[str]:
    """The stack: the selection, then every service it reaches through `consumes`.

    Selection entries keep their given order; dependencies follow in discovery order.
    """
    for name in uses:
        if name not in catalog.services:
            raise WtError(f"uses: unknown service {name!r} (not in the catalog)")
    stack = list(dict.fromkeys(uses))
    for name in stack:  # grows while it is walked: breadth-first through the dependencies
        stack += [dep for dep in catalog.services[name].consumes if dep not in stack]
    cycle = find_cycle({name: list(catalog.services[name].consumes) for name in stack})
    if cycle:
        raise WtError("cycle through catalog consumes: " + " -> ".join(cycle))
    return stack


@dataclass(frozen=True)
class Resolved:
    raw: dict[str, Any]  # the dict of a normal Config
    remotes: dict[str, str]  # catalog services outside the stack -> their remote ("" if none)
    order: tuple[str, ...]  # catalog service names in catalog order (the service index)
    warnings: tuple[str, ...]


def _ignored_remotes(chosen: dict[str, CatalogService], explicit: list[str]) -> tuple[str, ...]:
    """One warning per dependency pulled in by the stack whose `remote:` no longer applies."""
    warnings = []
    for dep, svc in chosen.items():
        if dep in explicit or svc.remote is None:
            continue
        consumer = next(name for name, other in chosen.items() if dep in other.consumes)
        warnings.append(f"remote of {dep!r} is ignored: {consumer!r} needs it, so it runs locally")
    return tuple(warnings)


def _service_data(
    name: str, svc: CatalogService, chosen: dict[str, CatalogService]
) -> dict[str, Any]:
    data = svc.model_dump(exclude={"infra", "consumes", "remote"}, exclude_unset=True)
    env = dict(data.get("env", {}))
    needs = list(data.get("needs", []))
    for dep, var in svc.consumes.items():
        if var in env:
            raise WtError(f"service {name!r}: env already sets {var} (used by consumes)")
        if chosen[dep].subdomain is None:
            raise WtError(f"service {name!r} consumes {dep!r}, which has no 'subdomain' (no route)")
        env[var] = service_ref(dep, "url")
        if dep not in needs:
            needs.append(dep)
    data["env"] = env
    data["needs"] = needs
    return data


def resolve(raw: dict[str, Any], config_path: Path) -> Resolved:
    """Turn a worktree.yaml that has `uses:` into the dict of a normal Config.

    The stack is the closure of `uses` through `consumes` (SPEC section 20).
    """
    if "services" in raw:
        raise WtError("set exactly one of 'services' or 'uses', not both")
    uses = raw.get("uses")
    if not isinstance(uses, list) or not uses or not all(isinstance(u, str) for u in uses):
        raise WtError("'uses' must be a non-empty list of service names")
    if "shared" in raw:
        raise WtError("'shared' cannot be set with 'uses'; infra comes from the catalog services")
    if "repos" in raw:
        raise WtError("'repos' cannot be set with 'uses'; repos come from the catalog")
    loaded = load_catalog(catalog_path(raw.get("catalog"), config_path.parent))
    catalog = loaded.catalog
    if len(catalog.services) > MAX_SERVICES:
        raise WtError(f"the catalog has {len(catalog.services)} services; at most 100 services")
    chosen = {name: catalog.services[name] for name in expand_uses(catalog, uses)}
    services = {name: _service_data(name, svc, chosen) for name, svc in chosen.items()}
    out = {k: v for k, v in raw.items() if k not in ("uses", "catalog")}
    out["repos"] = {
        r: str(loaded.repos[r]) for r in dict.fromkeys(s["repo"] for s in services.values())
    }
    out["services"] = services
    shared = _merge_infra(chosen)
    if shared:
        out["shared"] = shared
    remotes = {n: s.remote or "" for n, s in catalog.services.items() if n not in chosen}
    return Resolved(
        raw=out,
        remotes=remotes,
        order=tuple(catalog.services),
        warnings=_ignored_remotes(chosen, uses),
    )
