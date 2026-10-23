"""The user catalog: reusable repos and services that a worktree.yaml picks with `uses:`."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, ValidationError, model_validator

from wt.config import NAME, Service, Shared, Strict, _format_errors
from wt.errors import WtError
from wt.paths import wt_home

CATALOG_NAME = "catalog.yaml"
ENV_VAR = r"^[A-Z_][A-Z0-9_]*$"


class CatalogService(Service):
    infra: Shared = Field(default_factory=Shared, description="what it needs; merged into shared")
    consumes: dict[str, str] = Field(
        default_factory=dict, description="service name -> env var that receives its URL"
    )

    @model_validator(mode="after")
    def _check_consumes(self) -> Self:
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
