"""Redis provider against a real server: `pytest -m docker`. Isolated by WT_INSTANCE."""

import shutil
import socket
import subprocess
from collections.abc import Iterator

import pytest

from wt import compose
from wt.config import Config
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
