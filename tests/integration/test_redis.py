"""Redis provider against a real server: `pytest -m docker`. Isolated by WT_INSTANCE."""

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

from wt import compose, doctor
from wt.config import Config
from wt.errors import WtError
from wt.naming import Names
from wt.providers import redis
from wt.providers.base import BranchContext
from wt.providers.redis import RedisProvider
from wt.state import StateStore

pytestmark = pytest.mark.docker


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.fail("docker is not available")
    mp = pytest.MonkeyPatch()
    instance = f"wtrd{free_port()}"
    mp.setenv("WT_INSTANCE", instance)
    mp.setenv("WT_HOME", str(tmp_path_factory.mktemp("wt-home")))
    mp.setenv("WT_REDIS_PORT", str(free_port()))
    mp.setenv("WT_HTTP_PORT", str(free_port()))
    mp.setenv("WT_HTTPS_PORT", str(free_port()))
    try:
        redis.ensure_server()
        yield instance
    finally:
        docker("rm", "-f", "-v", redis.container())
        docker("volume", "rm", "-f", redis.volume())
        docker("network", "rm", compose.network_name())
        mp.undo()


def make(branch: str, prefix: bool = True) -> tuple[RedisProvider, BranchContext]:
    cfg = Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": {"redis": {"prefix": prefix}},
        }
    )
    names = Names("shop", branch, "shop.localhost")
    return RedisProvider(cfg), BranchContext(names, branch, "main", StateStore("shop"))


def keys(pattern: str = "*") -> set[str]:
    return set(redis.cli("--scan", "--pattern", pattern).stdout.split())


def test_ensure_server_is_idempotent(server: str) -> None:
    redis.cli("set", "idem:k", "1")
    redis.ensure_server()
    redis.ensure_server()
    assert redis.cli("get", "idem:k").stdout.strip() == "1"
    redis.cli("del", "idem:k")


def test_destroy_deletes_only_the_branch_prefix(server: str) -> None:
    cart, cart_ctx = make("feat/cart")
    other, other_ctx = make("feat/other")
    short, short_ctx = make("feat")
    longer, longer_ctx = make("feat-x")
    providers = [(cart, cart_ctx), (other, other_ctx), (short, short_ctx), (longer, longer_ctx)]
    for provider, ctx in providers:
        assert provider.create(ctx).names == [provider.prefix(ctx)]
        check = provider.verify(ctx)
        assert check.ok, check.detail
        for n in range(3):
            redis.cli("set", f"{provider.prefix(ctx)}k{n}", "v")
    redis.cli("set", "unrelated", "v")

    short.destroy(short_ctx)
    assert keys("feat:*") == set()
    assert len(keys("feat-x:*")) == 3  # `feat` must not touch `feat-x:`

    cart.destroy(cart_ctx)
    cart.destroy(cart_ctx)  # safe to repeat
    assert keys("feat-cart:*") == set()
    assert len(keys("feat-other:*")) == 3
    assert "unrelated" in keys()

    other.destroy(other_ctx)
    longer.destroy(longer_ctx)
    assert keys() == {"unrelated"}
    redis.cli("del", "unrelated")


def test_without_prefix_verify_warns_and_destroy_deletes_nothing(server: str) -> None:
    provider, ctx = make("feat/cart", prefix=False)
    assert provider.create(ctx).names == []
    check = provider.verify(ctx)
    assert check.ok and check.warnings
    redis.cli("set", "feat-cart:k", "v")
    redis.cli("set", "plain", "v")
    provider.destroy(ctx)
    assert keys() == {"feat-cart:k", "plain"}
    redis.cli("del", "feat-cart:k", "plain")


def test_template_vars_connect_from_the_host(server: str) -> None:
    provider, ctx = make("feat/cart")
    tv = provider.template_vars(ctx, "run")["redis"]
    assert tv["url"] == f"redis://127.0.0.1:{redis.host_port()}"
    assert tv["prefix"] == "feat-cart:"


FAIL_FAST_SECONDS = 60  # a taken port must fail clearly, not hang for the readiness wait


@pytest.fixture
def stranger_on_redis_port(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[socket.socket]:
    """A second, own instance whose Redis host port is held by a program that is not wt."""
    monkeypatch.setenv("WT_INSTANCE", f"wtrp{free_port()}")
    monkeypatch.setenv("WT_HOME", str(tmp_path_factory.mktemp("wt-home-ports")))
    monkeypatch.setenv("WT_REDIS_PORT", str(free_port()))
    stranger = socket.socket()
    stranger.bind(("127.0.0.1", redis.host_port()))
    stranger.listen()
    try:
        yield stranger
    finally:
        stranger.close()
        docker("rm", "-f", "-v", redis.container())
        docker("volume", "rm", "-f", redis.volume())
        docker("network", "rm", compose.network_name())


def failed_start() -> float:
    started = time.monotonic()
    with pytest.raises(WtError) as caught:
        redis.ensure_server()
    assert str(caught.value).strip()
    return time.monotonic() - started


def test_a_taken_port_fails_clearly_twice_then_recovers(
    stranger_on_redis_port: socket.socket,
) -> None:
    assert failed_start() < FAIL_FAST_SECONDS
    assert failed_start() < FAIL_FAST_SECONDS
    assert redis.host_port() not in compose.published_host_ports(redis.container())

    stranger_on_redis_port.close()
    redis.ensure_server()
    assert redis.host_port() in compose.published_host_ports(redis.container())
    assert redis.cli("ping").stdout.strip() == "PONG"


def test_doctor_fix_recreates_a_redis_that_runs_without_its_port(
    stranger_on_redis_port: socket.socket,
) -> None:
    failed_start()
    stranger_on_redis_port.close()
    docker("start", redis.container())
    time.sleep(3)
    # Older Docker engines (seen locally) restart the container without publishing anything;
    # newer ones (seen on the GitHub runner) publish normally. Both outcomes must be handled.
    quirk_reproduced = docker("port", redis.container()).stdout.strip() == ""
    check = doctor.check_redis(fix=False)
    if quirk_reproduced:
        assert check.level == "fail"
        assert "runs without its published ports" in check.detail
    else:
        assert check.level == "ok", check
        assert redis.host_port() in compose.published_host_ports(redis.container())

    fixed = doctor.check_redis(fix=True)
    assert fixed.level == "ok", fixed
    assert redis.host_port() in compose.published_host_ports(redis.container())
    assert redis.cli("ping").stdout.strip() == "PONG"
