import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from wt import compose, host, proc, providers
from wt.config import Config
from wt.errors import WtError
from wt.naming import Names
from wt.providers import kafka, keycloak, postgres, redis, server
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


def test_registry_has_the_builtin_providers() -> None:
    assert set(providers.REGISTRY) == {"postgres", "redis", "kafka", "keycloak"}
    assert [p.name for p in providers.active(cfg(postgres={"databases": ["shop"]}), print)] == [
        "postgres"
    ]
    assert providers.active(cfg(), print) == []
    assert [p.name for p in providers.active(cfg(redis={}), print)] == ["redis"]
    keycloak = cfg(keycloak={"realm": "r", "client": "c"})
    assert [p.name for p in providers.active(keycloak, print)] == ["keycloak"]


def test_active_returns_only_registered_and_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(providers.REGISTRY, "postgres", lambda c, log: Dummy())
    assert providers.active(cfg(), print) == []
    monkeypatch.delitem(providers.REGISTRY, "keycloak")
    assert providers.active(cfg(keycloak={"realm": "r", "client": "c"}), print) == []
    found = providers.active(
        cfg(postgres={"databases": ["shop"]}, keycloak={"realm": "r", "client": "c"}), print
    )
    assert [p.name for p in found] == ["postgres"]


def test_dataclasses(isolated_home: Path) -> None:
    names = Names("shop", "feat/x", "shop.localhost")
    ctx = BranchContext(names, "feat/x", "main", StateStore("shop"))
    assert ctx.base_branch == "main" and ctx.names is names
    assert Resources(["a"]).names == ["a"]
    assert Check(False, "boom", ["w"]).warnings == ["w"]


def published_lines() -> dict[str, str]:
    """What `docker port` prints for each shared container when it publishes its host port."""
    return {
        postgres.container(): f"5432/tcp -> 127.0.0.1:{postgres.host_port()}\n",
        redis.container(): f"6379/tcp -> 127.0.0.1:{redis.host_port()}\n",
        kafka.container(): f"19092/tcp -> 127.0.0.1:{kafka.host_port()}\n",
        keycloak.container(): f"8080/tcp -> 127.0.0.1:{keycloak.host_port()}\n",
    }


class FakeClock:
    """Time only moves when the code sleeps or when `docker compose up` "takes" `up_seconds`."""

    def __init__(self, up_seconds: float) -> None:
        self.now = 1000.0
        self.up_seconds = up_seconds
        self.probes_ok = False
        self.published = published_lines()

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def run(self, args: Sequence[str], *, check: bool = True, **kwargs: object) -> proc.Result:
        if list(args[:2]) == ["docker", "compose"]:
            self.now += self.up_seconds
            return proc.Result(0, "", "")
        if list(args[:2]) == ["docker", "port"]:
            return proc.Result(0, self.published.get(args[2], ""), "")
        result = proc.Result(0 if self.probes_ok else 1, "", "not ready")
        if check and not result.ok:
            raise proc.CommandError(args, result)
        return result


@pytest.fixture
def clock_for(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], FakeClock]:
    def make(up_seconds: float) -> FakeClock:
        clock = FakeClock(up_seconds)
        monkeypatch.setattr(time, "monotonic", clock.monotonic)
        monkeypatch.setattr(time, "sleep", clock.sleep)
        monkeypatch.setattr(proc, "run", clock.run)
        monkeypatch.setattr(compose, "ensure_network", lambda: None)
        return clock

    return make


@dataclass(frozen=True)
class Shared:
    """One shared-server provider: how to start it and the deadline SPEC §18 gives it."""

    module: ModuleType
    start: Callable[[], None]
    budget: int
    label: str


SHARED = [
    Shared(postgres, lambda: postgres.ensure_server(["shop"]), 60, "Postgres"),
    Shared(redis, lambda: redis.ensure_server(), 60, "Redis"),
    Shared(kafka, lambda: kafka.ensure_server(), 90, "Kafka"),
    Shared(keycloak, lambda: keycloak.ensure_server(), 120, "Keycloak"),
]
ids = [shared.label.lower() for shared in SHARED]


def readiness_budget(
    shared: Shared, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[float, float | None]]:
    """Start the server and return the (timeout, total) every wait_ready call got."""
    seen: list[tuple[float, float | None]] = []
    monkeypatch.setattr(
        shared.module, "wait_ready", lambda timeout, total=None: seen.append((timeout, total))
    )
    clock.probes_ok = True  # the post-readiness steps (repair, realm) must succeed
    shared.start()
    return seen


@pytest.mark.parametrize("shared", SHARED, ids=ids)
def test_readiness_gets_only_what_the_compose_start_left(
    shared: Shared, clock_for: Callable[[float], FakeClock], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_for(shared.budget - 10)
    assert readiness_budget(shared, clock, monkeypatch) == [(10, shared.budget)]


@pytest.mark.parametrize("shared", SHARED, ids=ids)
def test_a_start_that_used_the_whole_budget_still_gets_one_second_of_readiness(
    shared: Shared, clock_for: Callable[[float], FakeClock], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_for(shared.budget + 25)
    assert readiness_budget(shared, clock, monkeypatch) == [(1.0, shared.budget)]


@pytest.mark.parametrize("shared", SHARED, ids=ids)
def test_the_whole_start_stays_inside_the_budget_and_the_error_names_the_total(
    shared: Shared, clock_for: Callable[[float], FakeClock]
) -> None:
    clock = clock_for(shared.budget - 10)
    began = clock.now
    with pytest.raises(WtError, match=rf"in {shared.budget}s"):
        shared.start()
    # The readiness loop gave up one probe after the leftover 10 s, not 10 s plus another budget.
    assert clock.now - began == shared.budget


@pytest.mark.parametrize("shared", SHARED, ids=ids)
def test_the_ports_parsed_from_the_provider_doc_are_the_ones_it_publishes(shared: Shared) -> None:
    module = shared.module
    assert server.expected_ports(module.container(), module.compose_doc()) == {
        module.container(): {module.host_port()}
    }


@pytest.mark.parametrize("shared", SHARED, ids=ids)
def test_a_server_that_runs_without_its_ports_is_recreated_once_then_reported(
    shared: Shared, clock_for: Callable[[float], FakeClock], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_for(1)
    clock.published = {}
    monkeypatch.setattr(host, "port_free", lambda port: True)
    compose_calls: list[Sequence[str]] = []
    run = proc.run

    def recording(args: Sequence[str], **kwargs: Any) -> proc.Result:
        compose_calls.append(args)
        return run(args, **kwargs)

    monkeypatch.setattr(proc, "run", recording)
    with pytest.raises(WtError, match=rf"{shared.module.container()} does not publish port"):
        shared.start()
    starts = [c for c in compose_calls if list(c[:2]) == ["docker", "compose"]]
    assert ["--force-recreate" in c for c in starts] == [False, True]
