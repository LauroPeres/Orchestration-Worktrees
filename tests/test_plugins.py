"""Plugin providers (docs/SPEC.md §23): entry points are faked, nothing is installed."""

from collections.abc import Callable, Iterator
from importlib import metadata
from typing import Any

import pytest
from pydantic import BaseModel

from wt import providers
from wt.config import Config
from wt.errors import WtError
from wt.providers.base import BranchContext, Check, Resources


class EchoConfig(BaseModel):
    file: str


class EchoProvider:
    name = "echo"
    config_model = EchoConfig

    def __init__(self, cfg: Config, log: Callable[[str], None]) -> None:
        self.file = cfg.shared.extra["echo"]["file"]

    def ensure_server(self) -> None: ...

    def preflight(self, ctx: BranchContext) -> None: ...

    def create(self, ctx: BranchContext) -> Resources:
        return Resources(["echo-slice"])

    def verify(self, ctx: BranchContext) -> Check:
        return Check(True, "", [])

    def destroy(self, ctx: BranchContext) -> None: ...

    def template_vars(self, ctx: BranchContext, kind: str) -> dict[str, Any]:
        return {"file": self.file}


class FakeEntryPoint:
    def __init__(self, name: str, target: object = None, error: Exception | None = None) -> None:
        self.name = name
        self._target = target
        self._error = error

    def load(self) -> object:
        if self._error is not None:
            raise self._error
        return self._target


class NoConfigModel:
    def __init__(self, cfg: Config, log: Callable[[str], None]) -> None: ...


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., list[str]]]:
    """Fake the installed entry points; returns the names asked for, to see what was imported."""
    asked: list[str] = []

    def install(*entry_points: FakeEntryPoint) -> list[str]:
        def fake(*, group: str) -> list[FakeEntryPoint]:
            asked.append(group)
            return list(entry_points)

        monkeypatch.setattr(metadata, "entry_points", fake)
        providers.reset_plugins()
        return asked

    yield install
    providers.reset_plugins()


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


def test_a_good_plugin_is_loaded_and_active(install: Callable[..., list[str]]) -> None:
    install(FakeEntryPoint("echo", EchoProvider))
    loaded = providers.load_plugins()
    assert loaded.factories == {"echo": EchoProvider} and loaded.problems == {}
    config = cfg(redis={}, echo={"file": "/tmp/x"})
    assert [p.name for p in providers.active(config, print)] == ["redis", "echo"]
    providers.validate_infra(config)


def test_plugins_are_loaded_once_per_process(install: Callable[..., list[str]]) -> None:
    asked = install(FakeEntryPoint("echo", EchoProvider))
    assert providers.load_plugins() is providers.load_plugins()
    assert asked == [providers.ENTRY_POINT_GROUP] == ["wt.providers"]


def test_a_plugin_named_like_a_builtin_is_not_loaded(install: Callable[..., list[str]]) -> None:
    install(FakeEntryPoint("redis", EchoProvider))
    loaded = providers.load_plugins()
    assert loaded.factories == {}
    assert loaded.problems == {"redis": "provider 'redis' is built in"}
    assert providers.plugin_problems() == loaded.problems


def test_the_name_extra_is_reserved(install: Callable[..., list[str]]) -> None:
    install(FakeEntryPoint("extra", EchoProvider))
    assert providers.load_plugins().problems == {"extra": "provider 'extra' is reserved"}


def test_a_plugin_that_fails_to_import_is_a_problem_not_a_crash(
    install: Callable[..., list[str]],
) -> None:
    install(FakeEntryPoint("broken", error=ImportError("no module named foo")))
    loaded = providers.load_plugins()
    assert loaded.factories == {}
    assert loaded.problems == {
        "broken": "provider plugin 'broken' failed to load: ImportError: no module named foo"
    }
    with pytest.raises(WtError, match="provider plugin 'broken' failed to load"):
        providers.validate_infra(cfg(broken={"a": 1}))


def test_a_plugin_without_config_model_is_a_problem(install: Callable[..., list[str]]) -> None:
    install(FakeEntryPoint("bare", NoConfigModel))
    assert providers.plugin_problems() == {
        "bare": "provider plugin 'bare' failed to load: it is not callable or has no `config_model`"
    }


def test_an_unknown_key_lists_the_installed_providers(install: Callable[..., list[str]]) -> None:
    install(FakeEntryPoint("echo", EchoProvider), FakeEntryPoint("alpha", EchoProvider))
    with pytest.raises(WtError) as raised:
        providers.validate_infra(cfg(mongo={"url": "x"}))
    assert str(raised.value) == (
        "unknown infra 'mongo' (installed providers: kafka, keycloak, postgres, redis, alpha, echo)"
    )


def test_provider_names_are_builtins_then_plugins_sorted(
    install: Callable[..., list[str]],
) -> None:
    install(FakeEntryPoint("zeta", EchoProvider), FakeEntryPoint("echo", EchoProvider))
    assert providers.provider_names() == ["kafka", "keycloak", "postgres", "redis", "echo", "zeta"]


def test_a_mapping_the_config_model_rejects_is_a_one_line_error(
    install: Callable[..., list[str]],
) -> None:
    install(FakeEntryPoint("echo", EchoProvider))
    with pytest.raises(WtError) as raised:
        providers.validate_infra(cfg(echo={"file": 3}))
    message = str(raised.value)
    assert message.startswith("infra echo: file: ") and "\n" not in message
    with pytest.raises(WtError, match=r"infra echo: file: Field required"):
        providers.validate_infra(cfg(echo={}))


def test_a_project_without_plugin_keys_never_loads_plugins(
    install: Callable[..., list[str]],
) -> None:
    asked = install(FakeEntryPoint("echo", EchoProvider))
    config = cfg(redis={})
    providers.validate_infra(config)
    providers.active(config, print)
    assert asked == []


def test_active_fails_loudly_for_a_plugin_key_that_is_not_installed(
    install: Callable[..., list[str]],
) -> None:
    install()
    with pytest.raises(WtError, match="unknown infra 'echo'"):
        providers.active(cfg(echo={"file": "x"}), print)
