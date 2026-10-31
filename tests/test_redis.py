from collections.abc import Sequence

import pytest

from wt import compose, proc
from wt.config import Config
from wt.errors import WtError
from wt.naming import Names
from wt.providers import redis
from wt.providers.base import BranchContext
from wt.providers.redis import RedisProvider
from wt.state import BranchState, StateStore


def config(**opts: object) -> Config:
    return Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": {"redis": opts},
        }
    )


class FakeProc:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.rules: list[tuple[str, proc.Result]] = []

    def on(self, needle: str, result: proc.Result) -> None:
        self.rules.append((needle, result))

    def cli(self) -> list[list[str]]:
        """Arguments after `redis-cli` for every exec call, in order."""
        return [c[c.index("redis-cli") + 1 :] for c in self.calls if "redis-cli" in c]

    def __call__(self, args: Sequence[str], *, check: bool = True, **kwargs: object) -> proc.Result:
        self.calls.append(list(args))
        line = " ".join(args)
        result = proc.Result(0, "", "")
        for needle, rule in self.rules:
            if needle in line:
                result = rule
                break
        if check and not result.ok:
            raise proc.CommandError(args, result)
        return result


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProc:
    f = FakeProc()
    monkeypatch.setattr(proc, "run", f)
    f.on("docker port", proc.Result(0, f"6379/tcp -> 127.0.0.1:{redis.host_port()}\n", ""))
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr("wt.providers.redis.time.sleep", lambda s: None)
    return f


def ctx(branch: str = "feat/cart") -> BranchContext:
    return BranchContext(
        Names("shop", branch, "shop.localhost"), branch, "main", StateStore("shop")
    )


def provider(**opts: object) -> RedisProvider:
    return RedisProvider(config(**opts), lambda message: None)


def test_names_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("WT_INSTANCE", "WT_REDIS_IMAGE", "WT_REDIS_PORT"):
        monkeypatch.delenv(var, raising=False)
    assert redis.container() == "wt-redis"
    assert redis.volume() == "wt-redis-data"
    assert redis.image() == "redis:7-alpine"
    assert redis.host_port() == 56379
    monkeypatch.setenv("WT_INSTANCE", "t1")
    monkeypatch.setenv("WT_REDIS_IMAGE", "redis:6")
    monkeypatch.setenv("WT_REDIS_PORT", "56400")
    assert redis.container() == "t1-redis"
    assert redis.volume() == "t1-redis-data"
    assert redis.image() == "redis:6"
    assert redis.host_port() == 56400


def test_compose_doc_publishes_on_loopback_and_joins_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WT_REDIS_PORT", "56400")
    doc = redis.compose_doc()
    svc = doc["services"]["redis"]
    assert svc["ports"] == ["127.0.0.1:56400:6379"]
    assert svc["networks"] == [compose.NETWORK]
    assert svc["volumes"] == [f"{redis.volume()}:/data"]
    assert doc["volumes"] == {redis.volume(): {"name": redis.volume()}}


def test_ensure_server_starts_then_pings(fake: FakeProc) -> None:
    fake.on("redis-cli ping", proc.Result(0, "PONG\n", ""))
    provider().ensure_server()
    cmds = [" ".join(c) for c in fake.calls]
    up = next(i for i, c in enumerate(cmds) if "compose" in c and " up " in c)
    ping = next(i for i, c in enumerate(cmds) if "redis-cli ping" in c)
    assert up < ping
    assert (redis.server_dir() / "compose.yml").exists()


def test_wait_ready_times_out(fake: FakeProc, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.on("redis-cli ping", proc.Result(1, "", "refused"))
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr("wt.providers.redis.time.monotonic", lambda: next(ticks))
    with pytest.raises(WtError, match="did not become ready"):
        redis.wait_ready(timeout=25)


def test_prefix_and_resources() -> None:
    p = provider()
    assert p.prefix(ctx()) == "feat-cart:"
    assert p.create(ctx()).names == ["feat-cart:"]


def test_no_prefix_means_no_resources() -> None:
    p = provider(prefix=False)
    assert p.prefix(ctx()) == ""
    assert p.create(ctx()).names == []


def test_template_vars_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WT_INSTANCE", raising=False)
    monkeypatch.delenv("WT_REDIS_PORT", raising=False)
    p = provider()
    assert p.template_vars(ctx(), "compose") == {
        "redis": {"url": "redis://wt-redis:6379", "prefix": "feat-cart:"}
    }
    assert p.template_vars(ctx(), "run") == {
        "redis": {"url": "redis://127.0.0.1:56379", "prefix": "feat-cart:"}
    }
    assert provider(prefix=False).template_vars(ctx(), "run")["redis"]["prefix"] == ""


def test_verify_probes_key_under_prefix(fake: FakeProc) -> None:
    fake.on("redis-cli ping", proc.Result(0, "PONG\n", ""))
    fake.on("redis-cli get", proc.Result(0, "ok\n", ""))
    check = provider().verify(ctx())
    assert check.ok and not check.warnings
    cli = fake.cli()
    assert cli[0] == ["ping"]
    assert [c[0] for c in cli[1:]] == ["set", "get", "del"]
    assert all(c[1].startswith("feat-cart:") for c in cli[1:])


def test_verify_fails_when_ping_fails(fake: FakeProc) -> None:
    fake.on("redis-cli ping", proc.Result(1, "", "down"))
    check = provider().verify(ctx())
    assert not check.ok and "PING" in check.detail


def test_verify_fails_when_probe_reads_back_wrong_value(fake: FakeProc) -> None:
    fake.on("redis-cli ping", proc.Result(0, "PONG\n", ""))
    fake.on("redis-cli get", proc.Result(0, "\n", ""))
    check = provider().verify(ctx())
    assert not check.ok and "probe" in check.detail
    assert fake.cli()[-1][0] == "del"  # cleans up even on failure


def test_verify_without_prefix_pings_and_warns(fake: FakeProc) -> None:
    fake.on("redis-cli ping", proc.Result(0, "PONG\n", ""))
    check = provider(prefix=False).verify(ctx())
    assert check.ok and check.warnings
    assert fake.cli() == [["ping"]]


def test_destroy_unlinks_only_keys_under_the_prefix(fake: FakeProc) -> None:
    fake.on("--scan", proc.Result(0, "feat-cart:a\nfeat-cart:b\n", ""))
    provider().destroy(ctx())
    cli = fake.cli()
    assert cli[0] == ["--scan", "--pattern", "feat-cart:*"]
    assert cli[1] == ["unlink", "feat-cart:a", "feat-cart:b"]


def test_destroy_batches_keys(fake: FakeProc) -> None:
    keys = [f"feat-cart:{i}" for i in range(redis.BATCH + 1)]
    fake.on("--scan", proc.Result(0, "\n".join(keys) + "\n", ""))
    provider().destroy(ctx())
    unlinks = [c for c in fake.cli() if c[0] == "unlink"]
    assert [len(c) - 1 for c in unlinks] == [redis.BATCH, 1]


def test_destroy_is_safe_when_no_keys(fake: FakeProc) -> None:
    provider().destroy(ctx())
    assert all(c[0] == "--scan" for c in fake.cli())


def test_destroy_without_prefix_touches_nothing(fake: FakeProc) -> None:
    provider(prefix=False).destroy(ctx())
    assert fake.calls == []


def test_destroy_uses_recorded_prefix_when_config_is_gone(fake: FakeProc) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart", slug="feat-cart", providers={"redis": ["feat-cart:"]}
        )
    provider_without_section = RedisProvider(
        Config.model_validate(
            {
                "project": "shop",
                "domain": "shop.localhost",
                "repos": {"api": "./api"},
                "services": {"api": {"repo": "api", "compose": "c.yml"}},
            }
        ),
        lambda message: None,
    )
    provider_without_section.destroy(c)
    assert fake.cli()[0] == ["--scan", "--pattern", "feat-cart:*"]


def test_destroy_ignores_a_recorded_prefix_that_is_not_a_wt_prefix(fake: FakeProc) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart", slug="feat-cart", providers={"redis": ["*"]}
        )
    provider(prefix=False).destroy(c)
    assert fake.calls == []
