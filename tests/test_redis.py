from collections.abc import Sequence

import pytest

from wt import compose, proc
from wt.config import Config
from wt.errors import WtError
from wt.naming import Names
from wt.projects import KnownProject
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


def ctx(branch: str = "feat/cart", project: str = "shop") -> BranchContext:
    return BranchContext(
        Names(project, branch, f"{project}.localhost"), branch, "main", StateStore(project)
    )


def record(c: BranchContext, *names: str) -> None:
    """Record provider resources for the branch, as an earlier `up` (or 0.5.0) would have."""
    with c.state.edit() as state:
        state.branches[c.branch] = BranchState(
            branch=c.branch, slug=c.names.slug, providers={"redis": list(names)}
        )


def unlinked(fake: FakeProc) -> list[str]:
    return [key for c in fake.cli() if c[0] == "unlink" for key in c[1:]]


def scanned(fake: FakeProc) -> list[str]:
    return [c[2] for c in fake.cli() if c[0] == "--scan"]


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
    assert p.prefix(ctx()) == "shop:feat-cart:"
    assert p.create(ctx()).names == ["shop:feat-cart:"]


def test_two_projects_with_the_same_branch_get_different_prefixes() -> None:
    p = provider()
    assert p.prefix(ctx("main", "shop")) == "shop:main:"
    assert p.prefix(ctx("main", "blog")) == "blog:main:"


def test_project_and_slug_never_blur_into_each_other() -> None:
    p = provider()
    assert p.prefix(ctx("c", "a-b")) != p.prefix(ctx("b-c", "a"))


def test_no_prefix_means_no_resources() -> None:
    p = provider(prefix=False)
    assert p.prefix(ctx()) == ""
    assert p.create(ctx()).names == []


def test_template_vars_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WT_INSTANCE", raising=False)
    monkeypatch.delenv("WT_REDIS_PORT", raising=False)
    p = provider()
    assert p.template_vars(ctx(), "compose") == {
        "redis": {"url": "redis://wt-redis:6379", "prefix": "shop:feat-cart:"}
    }
    assert p.template_vars(ctx(), "run") == {
        "redis": {"url": "redis://127.0.0.1:56379", "prefix": "shop:feat-cart:"}
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
    assert all(c[1].startswith("shop:feat-cart:") for c in cli[1:])


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
    fake.on("--scan", proc.Result(0, "shop:feat-cart:a\nshop:feat-cart:b\n", ""))
    provider().destroy(ctx())
    cli = fake.cli()
    assert cli[0] == ["--scan", "--pattern", "shop:feat-cart:*"]
    assert cli[1] == ["unlink", "shop:feat-cart:a", "shop:feat-cart:b"]


def test_destroy_of_feat_in_shop_never_touches_siblings_or_other_projects(fake: FakeProc) -> None:
    keys = [
        "shop:feat:a",
        "shop:feat-x:a",
        "sh:feat:a",
        "blog:feat:a",
        "feat:a",
        "shop:feat",
        "xshop:feat:a",
    ]
    fake.on("--scan", proc.Result(0, "\n".join(keys) + "\n", ""))
    provider().destroy(ctx("feat"))
    assert scanned(fake) == ["shop:feat:*"]
    assert unlinked(fake) == ["shop:feat:a"]


def test_destroy_batches_keys(fake: FakeProc) -> None:
    keys = [f"shop:feat-cart:{i}" for i in range(redis.BATCH + 1)]
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
    record(c, "shop:feat-cart:")
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
    assert fake.cli()[0] == ["--scan", "--pattern", "shop:feat-cart:*"]


def test_destroy_ignores_a_recorded_prefix_that_is_not_a_wt_prefix(fake: FakeProc) -> None:
    c = ctx()
    record(c, "*", "shop:*:", "a:b:c:", ":", "shop:feat cart:", "Shop:feat:")
    provider(prefix=False).destroy(c)
    assert fake.calls == []


def test_destroy_cleans_a_prefix_recorded_by_0_5_0(fake: FakeProc) -> None:
    c = ctx()
    record(c, "feat-cart:")
    fake.on("--scan", proc.Result(0, "feat-cart:a\nfeat-cart-x:a\nshop:feat-cart:b\n", ""))
    provider().destroy(c)
    assert scanned(fake) == ["feat-cart:*", "shop:feat-cart:*"]
    assert unlinked(fake) == ["feat-cart:a", "shop:feat-cart:b"]


def test_old_prefix_does_not_match_a_longer_sibling(fake: FakeProc) -> None:
    c = ctx("feat")
    record(c, "feat:")
    fake.on("--scan", proc.Result(0, "feat-x:a\nfeat:a\n", ""))
    provider(prefix=False).destroy(c)
    assert unlinked(fake) == ["feat:a"]


def test_destroy_cleans_old_and_new_recorded_prefixes_together(fake: FakeProc) -> None:
    c = ctx()
    record(c, "feat-cart:", "shop:feat-cart:")
    provider(prefix=False).destroy(c)
    assert scanned(fake) == ["feat-cart:*", "shop:feat-cart:*"]


def test_destroy_skips_a_malformed_name_but_cleans_the_valid_ones(fake: FakeProc) -> None:
    c = ctx()
    record(c, "shop:*:", "shop:feat-cart:")
    provider(prefix=False).destroy(c)
    assert scanned(fake) == ["shop:feat-cart:*"]


def knows_projects(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setattr(
        "wt.providers.base.known_projects", lambda: [KnownProject(n, None) for n in names]
    )


def destroy_logging(c: BranchContext, fake: FakeProc, **opts: object) -> list[str]:
    logged: list[str] = []
    RedisProvider(config(**opts), logged.append).destroy(c)
    return logged


def test_old_prefix_named_like_the_current_project_is_kept(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    knows_projects(monkeypatch)
    c = ctx("app", "app")
    record(c, "app:")
    fake.on("--scan", proc.Result(0, "app:main:a\napp:x\n", ""))
    logged = destroy_logging(c, fake, prefix=False)
    assert unlinked(fake) == []
    assert logged == ["kept app:: it could belong to project app; delete by hand if it is yours"]


def test_old_prefix_of_an_unknown_name_is_deleted_as_before(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    knows_projects(monkeypatch, "blog")
    c = ctx()
    record(c, "legacy:")
    fake.on("--scan", proc.Result(0, "legacy:a\nlegacy:b\n", ""))
    logged = destroy_logging(c, fake, prefix=False)
    assert unlinked(fake) == ["legacy:a", "legacy:b"]
    assert logged == []


def test_old_prefix_named_like_another_known_project_is_kept(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    knows_projects(monkeypatch, "blog")
    c = ctx()
    record(c, "blog:")
    fake.on("--scan", proc.Result(0, "blog:main:a\n", ""))
    logged = destroy_logging(c, fake, prefix=False)
    assert unlinked(fake) == []
    assert "blog" in logged[0] and logged[0].startswith("kept blog:")


@pytest.mark.parametrize("failure", [WtError("corrupt"), OSError("denied")])
def test_unavailable_projects_index_falls_back_to_the_current_project(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def broken() -> list[KnownProject]:
        raise failure

    monkeypatch.setattr("wt.providers.base.known_projects", broken)
    c = ctx("main", "shop")
    record(c, "shop:", "legacy:")
    fake.on("--scan", proc.Result(0, "shop:main:a\nlegacy:a\n", ""))
    logged = destroy_logging(c, fake, prefix=False)
    assert unlinked(fake) == ["legacy:a"]
    assert len(logged) == 1 and logged[0].startswith("kept shop:")


def test_the_two_segment_prefix_is_never_skipped(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    knows_projects(monkeypatch, "shop", "app")
    c = ctx("main", "app")
    record(c, "app:main:")
    fake.on("--scan", proc.Result(0, "app:main:a\napp:other:b\n", ""))
    logged = destroy_logging(c, fake)
    assert unlinked(fake) == ["app:main:a"]
    assert logged == []


@pytest.mark.parametrize("bad", ["shop:feat:\n", "shop:\n", "shop:feat:x"])
def test_a_recorded_prefix_must_match_in_full(fake: FakeProc, bad: str) -> None:
    c = ctx()
    record(c, bad)
    provider(prefix=False).destroy(c)
    assert scanned(fake) == []
