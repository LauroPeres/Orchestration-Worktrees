"""The project catalog (SPEC section 21): its schema, a selection of it, and `wt migrate`."""

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from wt.config import (
    DOMAIN,
    MAX_SERVICES,
    NAME,
    PROJECT,
    Config,
    LoadedConfig,
    Service,
    Shared,
    Strict,
    _format_errors,
    find_cycle,
)
from wt.errors import WtError
from wt.naming import service_ref
from wt.paths import wt_home

CATALOG_NAME = "catalog.yaml"
LEGACY_NAME = "worktree.yaml"  # the v0.5.x file `wt migrate` reads
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
    project: str | None = Field(
        default=None, pattern=PROJECT, description="project name; required by load_project"
    )
    domain: str | None = Field(
        default=None, pattern=DOMAIN, description="base domain; required by load_project"
    )
    repos: dict[str, str] = Field(default_factory=dict)
    worktrees: str | None = Field(
        default=None,
        description="folder for the worktrees (~ expanded, relative to this file); optional",
    )
    services: dict[str, CatalogService] = Field(min_length=1)

    @field_validator("worktrees")
    @classmethod
    def _worktrees_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be empty")
        return value

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


def _read_mapping(path: Path, missing: str) -> dict[str, Any]:
    """The YAML mapping in `path`; `missing` is the message when the file does not exist."""
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise WtError(missing) from exc
    except yaml.YAMLError as exc:
        raise WtError(f"{path} is not valid YAML:\n{exc}") from exc
    if not isinstance(raw, dict):
        raise WtError(f"{path} must contain a YAML mapping")
    return raw


def load_catalog(path: Path) -> LoadedCatalog:
    path = path.resolve()
    raw = _read_mapping(path, f"catalog not found: {path}")
    try:
        catalog = Catalog.model_validate(raw)
    except ValidationError as exc:
        raise WtError(f"invalid {path.name}:\n{_format_errors(exc)}") from exc
    repos = {}
    for name, value in catalog.repos.items():
        repo = Path(value).expanduser()
        repos[name] = repo if repo.is_absolute() else (path.parent / repo).resolve()
    return LoadedCatalog(catalog=catalog, path=path, repos=repos)


def worktrees_folder(catalog: Catalog, catalog_file: Path) -> Path | None:
    """The `worktrees` folder as wt uses it: `~` expanded, relative to the catalog's folder.

    Not created and not required to exist (wt creates it with the first worktree).
    """
    if catalog.worktrees is None:
        return None
    folder = Path(catalog.worktrees.strip()).expanduser()
    return folder if folder.is_absolute() else (catalog_file.parent / folder).resolve()


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
            known = ", ".join(catalog.services)
            raise WtError(f"uses: unknown service {name!r} (not in the catalog). Known: {known}")
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


def _uses_list(raw: dict[str, Any]) -> list[str]:
    uses = raw.get("uses")
    if not isinstance(uses, list) or not uses or not all(isinstance(u, str) for u in uses):
        raise WtError("'uses' must be a non-empty list of service names")
    return uses


def _select(loaded: LoadedCatalog, uses: list[str]) -> Resolved:
    """The `repos`, `services` and `shared` of a normal Config for a selection of the catalog."""
    catalog = loaded.catalog
    if len(catalog.services) > MAX_SERVICES:
        raise WtError(f"the catalog has {len(catalog.services)} services; at most 100 services")
    chosen = {name: catalog.services[name] for name in expand_uses(catalog, uses)}
    services = {name: _service_data(name, svc, chosen) for name, svc in chosen.items()}
    out: dict[str, Any] = {
        "repos": {
            r: str(loaded.repos[r]) for r in dict.fromkeys(s["repo"] for s in services.values())
        },
        "services": services,
    }
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


def load_project(path: Path, uses: list[str] | None = None) -> LoadedConfig:
    """Load the project catalog (SPEC section 21) for a selection of its services.

    `uses=None` is the anchor: the first service in catalog order, plus its closure.
    """
    path = path.resolve()
    if not path.is_file():
        raise WtError(f"no catalog at {path}. Run wt init, or wt migrate <worktree.yaml>")
    loaded = load_catalog(path)
    catalog = loaded.catalog
    absent = [key for key in ("project", "domain") if getattr(catalog, key) is None]
    if absent:
        raise WtError(f"{path} is missing required key(s): {', '.join(absent)}")
    if uses is None:
        uses = [next(iter(catalog.services))]
    elif not uses:
        raise WtError("choose at least one service (wt add <b> --uses a,b)")
    selected = _select(loaded, uses)
    try:
        config = Config.model_validate(
            {"project": catalog.project, "domain": catalog.domain, **selected.raw}
        )
    except ValidationError as exc:
        raise WtError(f"invalid {path.name}:\n{_format_errors(exc)}") from exc
    return LoadedConfig(
        config=config,
        path=path,
        catalog=path,
        remotes=selected.remotes,
        uses=tuple(dict.fromkeys(uses)),
        service_order=selected.order,
        warnings=selected.warnings,
        worktrees_dir=worktrees_folder(catalog, path),
    )


# --- wt migrate: fold a v0.5.x worktree.yaml into the catalog (pure: the caller writes it) ---
@dataclass(frozen=True)
class MergeResult:
    catalog: dict[str, Any]  # raw catalog, ready to dump: the existing keys, plus the merge
    added: tuple[str, ...]  # services the legacy file brought
    skipped: tuple[str, ...]  # services already in the catalog, identical
    uses: tuple[str, ...]  # the stack to record for the project's branches that have none


@dataclass(frozen=True)
class _Incoming:
    repos: dict[str, str]  # absolute paths
    services: dict[str, dict[str, Any]]
    uses: tuple[str, ...]


def read_legacy(path: Path) -> dict[str, Any]:
    """The raw mapping of a worktree.yaml."""
    return _read_mapping(path, f"{path} not found")


def _mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WtError(f"{what} must be a mapping")
    return value


def _absolute(value: object, base: Path, repo: str) -> str:
    if not isinstance(value, str):
        raise WtError(f"repo {repo!r} must be a path")
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path).resolve())


def _mentions(service: dict[str, Any], provider: str) -> bool:
    """Does the service text use `{{ <provider>.`, i.e. does it need that shared server?"""
    env = _mapping(service.get("env", {}), "env")
    texts = [*env.values(), service.get("run"), service.get("health")]
    call = re.compile(r"\{\{\s*" + re.escape(provider) + r"\.")
    return any(call.search(text) for text in texts if isinstance(text, str))


def _with_infra(
    services: dict[str, dict[str, Any]], shared: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Turn the project-level `shared` into per-service `infra`.

    A section nobody mentions goes to the first service so the server is still provisioned.
    """
    infra: dict[str, dict[str, Any]] = {name: {} for name in services}
    for provider, section in shared.items():
        if section is None:  # `redis:` with no value: not configured
            continue
        owners = [n for n, svc in services.items() if _mentions(svc, provider)]
        for owner in owners or list(services)[:1]:
            infra[owner][provider] = section
    return {n: {**svc, "infra": infra[n]} if infra[n] else svc for n, svc in services.items()}


def _incoming_services(legacy: dict[str, Any], legacy_dir: Path) -> _Incoming:
    repos = _mapping(legacy.get("repos", {}), "repos")
    services = {  # a copy: the result must not alias the caller's dict
        name: copy.deepcopy(_mapping(svc, f"service {name!r}"))
        for name, svc in _mapping(legacy["services"], "services").items()
    }
    shared = _mapping(legacy.get("shared", {}), "shared")
    return _Incoming(
        repos={name: _absolute(value, legacy_dir, name) for name, value in repos.items()},
        services=_with_infra(services, shared),
        uses=tuple(services),
    )


def _incoming_uses(legacy: dict[str, Any], legacy_dir: Path) -> _Incoming:
    loaded = load_catalog(catalog_path(legacy.get("catalog"), legacy_dir))
    stack = expand_uses(loaded.catalog, _uses_list(legacy))
    services = {  # catalog order: the position is the service index
        name: svc.model_dump(exclude_unset=True)
        for name, svc in loaded.catalog.services.items()
        if name in stack
    }
    used_repos = dict.fromkeys(svc["repo"] for svc in services.values())
    return _Incoming(
        repos={repo: str(loaded.repos[repo]) for repo in used_repos},
        services=services,
        uses=tuple(stack),
    )


def _incoming(legacy: dict[str, Any], legacy_dir: Path, legacy_name: str) -> _Incoming:
    if ("services" in legacy) == ("uses" in legacy):
        raise WtError(f"{legacy_name}: set exactly one of 'services' or 'uses'")
    if "services" in legacy:
        return _incoming_services(legacy, legacy_dir)
    return _incoming_uses(legacy, legacy_dir)


def _identity(current: dict[str, Any], legacy: dict[str, Any], legacy_name: str) -> dict[str, str]:
    """`project` and `domain` of the merged catalog: the legacy file's, which must agree."""
    identity = {}
    for key in ("project", "domain"):
        theirs = legacy.get(key)
        if not isinstance(theirs, str):
            raise WtError(f"{legacy_name} has no {key!r} (text)")
        ours = current.get(key)
        if ours is not None and ours != theirs:
            raise WtError(f"catalog is {key} {ours}, {legacy_name} is {key} {theirs}")
        identity[key] = theirs
    return identity


def _repo_path(value: object, catalog_dir: Path | None) -> Path:
    """A repo path of the catalog as wt reads it: `~` expanded, relative to the catalog folder."""
    path = Path(str(value)).expanduser()
    if path.is_absolute() or catalog_dir is None:
        return path
    return (catalog_dir / path).resolve()


def _conflicts(
    repos: dict[str, Any],
    services: dict[str, Any],
    incoming: _Incoming,
    legacy_name: str,
    catalog_dir: Path | None,
) -> list[str]:
    differing = [
        f"service {name!r} differs from the catalog's"
        for name, svc in incoming.services.items()
        if name in services and services[name] != svc
    ]
    for name, path in incoming.repos.items():
        if name in repos and _repo_path(repos[name], catalog_dir) != Path(path):
            differing.append(
                f"repo {name!r} is {repos[name]} in the catalog, {path} in {legacy_name}"
            )
    return differing


def merge_legacy(
    existing: dict[str, Any] | None,
    legacy: dict[str, Any],
    legacy_dir: Path,
    legacy_name: str = LEGACY_NAME,
    catalog_dir: Path | None = None,
) -> MergeResult:
    """Fold a v0.5.x worktree.yaml (`legacy`, read from `legacy_dir`) into the raw catalog.

    `catalog_dir` is the folder of the existing catalog file: its relative repo paths are read
    against it, so the same repo written differently is not a clash. Entries already in the
    catalog are kept exactly as written.

    Merging a file into its own result changes nothing, so `wt migrate` is safe to repeat.
    """
    current = existing or {}
    identity = _identity(current, legacy, legacy_name)
    incoming = _incoming(legacy, legacy_dir, legacy_name)
    repos = _mapping(current.get("repos") or {}, "repos")
    services = _mapping(current.get("services") or {}, "services")
    conflicts = _conflicts(repos, services, incoming, legacy_name, catalog_dir)
    if conflicts:
        raise WtError(f"{legacy_name} does not fit the catalog: " + "; ".join(conflicts))
    added = tuple(name for name in incoming.services if name not in services)
    merged = {
        **current,  # keys wt does not manage (e.g. `worktrees`) stay as written
        **identity,
        "repos": {**repos, **{n: p for n, p in incoming.repos.items() if n not in repos}},
        "services": {**services, **{name: incoming.services[name] for name in added}},
    }
    try:
        Catalog.model_validate(merged)
    except ValidationError as exc:
        raise WtError(f"the merged catalog is invalid:\n{_format_errors(exc)}") from exc
    return MergeResult(
        catalog=merged,
        added=added,
        skipped=tuple(name for name in incoming.services if name in services),
        uses=incoming.uses,
    )
