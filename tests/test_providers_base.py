from pathlib import Path

import pytest

from wt import providers
from wt.config import Config
from wt.naming import Names
from wt.providers.base import BranchContext, Check, Resources
from wt.state import StateStore


class Dummy:
    name = "postgres"

    def ensure_server(self) -> None: ...

    def create(self, ctx: BranchContext) -> Resources:
        return Resources([])

    def verify(self, ctx: BranchContext) -> Check:
        return Check(True, "", [])

    def destroy(self, ctx: BranchContext) -> None: ...

    def template_vars(self, ctx: BranchContext, kind: str) -> dict[str, object]:
        return {}


def cfg(**shared: object) -> Config:
    return Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "."},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": shared,
        }
    )


def test_registry_has_only_postgres_so_far() -> None:
    assert set(providers.REGISTRY) == {"postgres"}
    assert [p.name for p in providers.active(cfg(postgres={"databases": ["shop"]}), print)] == [
        "postgres"
    ]
    assert providers.active(cfg(), print) == []


def test_active_returns_only_registered_and_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(providers.REGISTRY, "postgres", lambda c, log: Dummy())
    assert providers.active(cfg(), print) == []
    assert providers.active(cfg(kafka={"groups": ["g"]}), print) == []
    found = providers.active(cfg(postgres={"databases": ["shop"]}, kafka={"groups": ["g"]}), print)
    assert [p.name for p in found] == ["postgres"]


def test_dataclasses(isolated_home: Path) -> None:
    names = Names("shop", "feat/x", "shop.localhost")
    ctx = BranchContext(names, "feat/x", "main", StateStore("shop"))
    assert ctx.base_branch == "main" and ctx.names is names
    assert Resources(["a"]).names == ["a"]
    assert Check(False, "boom", ["w"]).warnings == ["w"]
