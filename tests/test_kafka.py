from collections.abc import Callable, Sequence

import pytest

from wt import compose, proc
from wt.config import Config
from wt.errors import WtError
from wt.naming import Names
from wt.providers import kafka
from wt.providers.base import BranchContext
from wt.providers.kafka import KafkaProvider
from wt.state import BranchState, StateStore


def config(**opts: object) -> Config:
    opts.setdefault("groups", ["worker"])
    return Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": {"kafka": opts},
        }
    )


class FakeProc:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.rules: list[tuple[str, Callable[[], proc.Result]]] = []

    def on(self, needle: str, result: proc.Result) -> None:
        self.rules.append((needle, lambda: result))

    def on_each(self, needle: str, next_result: Callable[[], proc.Result]) -> None:
        """Answer every matching call with the next result (a sequence of answers)."""
        self.rules.append((needle, next_result))

    def tools(self) -> list[list[str]]:
        """`[script, args...]` for every exec of a /opt/kafka/bin tool, in order."""
        out = []
        for c in self.calls:
            for i, a in enumerate(c):
                if a.startswith("/opt/kafka/bin/"):
                    out.append([a.rsplit("/", 1)[1], *c[i + 1 :]])
        return out

    def __call__(self, args: Sequence[str], *, check: bool = True, **kwargs: object) -> proc.Result:
        self.calls.append(list(args))
        line = " ".join(args)
        result = proc.Result(0, "", "")
        for needle, rule in self.rules:
            if needle in line:
                result = rule()
                break
        if check and not result.ok:
            raise proc.CommandError(args, result)
        return result


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProc:
    f = FakeProc()
    monkeypatch.setattr(proc, "run", f)
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr("wt.providers.kafka.time.sleep", lambda s: None)
    return f


def ctx(branch: str = "feat/cart") -> BranchContext:
    return BranchContext(
        Names("shop", branch, "shop.localhost"), branch, "main", StateStore("shop")
    )


def provider(**opts: object) -> KafkaProvider:
    return KafkaProvider(config(**opts), lambda message: None)


def test_names_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("WT_INSTANCE", "WT_KAFKA_IMAGE", "WT_KAFKA_PORT"):
        monkeypatch.delenv(var, raising=False)
    assert kafka.container() == "wt-kafka"
    assert kafka.volume() == "wt-kafka-data"
    assert kafka.image() == kafka.DEFAULT_IMAGE
    assert kafka.DEFAULT_IMAGE.startswith("apache/kafka:") and "latest" not in kafka.DEFAULT_IMAGE
    assert kafka.host_port() == 59092
    monkeypatch.setenv("WT_INSTANCE", "t1")
    monkeypatch.setenv("WT_KAFKA_IMAGE", "apache/kafka:4.0.0")
    monkeypatch.setenv("WT_KAFKA_PORT", "59100")
    assert kafka.container() == "t1-kafka"
    assert kafka.volume() == "t1-kafka-data"
    assert kafka.image() == "apache/kafka:4.0.0"
    assert kafka.host_port() == 59100


def test_compose_doc_is_single_node_kraft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WT_INSTANCE", raising=False)
    monkeypatch.setenv("WT_KAFKA_PORT", "59100")
    doc = kafka.compose_doc()
    svc = doc["services"]["kafka"]
    env = svc["environment"]
    assert svc["ports"] == ["127.0.0.1:59100:19092"]
    assert svc["networks"] == [compose.NETWORK]
    assert svc["volumes"] == [f"{kafka.volume()}:/var/lib/kafka/data"]
    assert env["KAFKA_NODE_ID"] == "1"
    assert env["KAFKA_PROCESS_ROLES"] == "broker,controller"
    assert env["CLUSTER_ID"] == kafka.CLUSTER_ID
    assert env["KAFKA_AUTO_CREATE_TOPICS_ENABLE"] == "true"
    assert "INTERNAL://:9092" in env["KAFKA_LISTENERS"]
    assert "EXTERNAL://:19092" in env["KAFKA_LISTENERS"]
    assert "CONTROLLER://:9093" in env["KAFKA_LISTENERS"]
    assert (
        env["KAFKA_ADVERTISED_LISTENERS"] == "INTERNAL://wt-kafka:9092,EXTERNAL://127.0.0.1:59100"
    )
    assert env["KAFKA_CONTROLLER_QUORUM_VOTERS"] == "1@localhost:9093"
    for key in (
        "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR",
        "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR",
        "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR",
    ):
        assert env[key] == "1"
    assert doc["volumes"] == {kafka.volume(): {"name": kafka.volume()}}


def test_ensure_server_starts_then_asks_the_broker(fake: FakeProc) -> None:
    provider().ensure_server()
    cmds = [" ".join(c) for c in fake.calls]
    up = next(i for i, c in enumerate(cmds) if "compose" in c and " up " in c)
    api = next(i for i, c in enumerate(cmds) if "kafka-broker-api-versions.sh" in c)
    assert up < api
    assert "--bootstrap-server localhost:9092" in cmds[api]
    assert (kafka.server_dir() / "compose.yml").exists()


def test_wait_ready_times_out_after_90s_by_default(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.on("kafka-broker-api-versions.sh", proc.Result(1, "", "refused"))
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr("wt.providers.kafka.time.monotonic", lambda: next(ticks))
    with pytest.raises(WtError, match="did not become ready in 90s"):
        kafka.wait_ready()


def test_group_and_topic_names() -> None:
    p = provider(groups=["worker", "audit"])
    assert p.group("worker", ctx()) == "worker-feat-cart"
    assert p.topic("cart", ctx()) == "cart"
    assert provider(topic_prefix=True).topic("cart", ctx()) == "feat-cart.cart"


def test_resources_are_groups_plus_prefix() -> None:
    assert provider(groups=["worker", "audit"]).create(ctx()).names == [
        "worker-feat-cart",
        "audit-feat-cart",
    ]
    assert provider(topic_prefix=True).create(ctx()).names == ["worker-feat-cart", "feat-cart."]


def test_template_vars_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WT_INSTANCE", raising=False)
    monkeypatch.delenv("WT_KAFKA_PORT", raising=False)
    p = provider(topic_prefix=True)
    compose_vars = p.template_vars(ctx(), "compose")["kafka"]
    run_vars = p.template_vars(ctx(), "run")["kafka"]
    assert compose_vars["bootstrap"] == "wt-kafka:9092"
    assert run_vars["bootstrap"] == "127.0.0.1:59092"
    assert run_vars["group"]("worker") == "worker-feat-cart"
    assert run_vars["topic"]("cart") == "feat-cart.cart"
    assert provider().template_vars(ctx(), "run")["kafka"]["topic"]("cart") == "cart"


def test_unknown_group_is_an_error() -> None:
    group = provider().template_vars(ctx(), "run")["kafka"]["group"]
    with pytest.raises(WtError, match="nope"):
        group("nope")


@pytest.mark.parametrize("bad", ["Bad", "a b", "a/b", "", "a*"])
def test_invalid_names_are_rejected(bad: str) -> None:
    with pytest.raises(WtError, match="invalid"):
        provider(groups=[bad]).create(ctx())
    with pytest.raises(WtError, match="invalid"):
        provider().topic(bad, ctx())


def test_verify_without_prefix_only_asks_the_broker(fake: FakeProc) -> None:
    check = provider().verify(ctx())
    assert check.ok
    assert [t[0] for t in fake.tools()] == ["kafka-broker-api-versions.sh"]


def test_verify_with_prefix_creates_and_deletes_probe_topic(fake: FakeProc) -> None:
    check = provider(topic_prefix=True).verify(ctx())
    assert check.ok
    tools = fake.tools()
    assert [t[0] for t in tools] == [
        "kafka-broker-api-versions.sh",
        "kafka-topics.sh",
        "kafka-topics.sh",
    ]
    assert "--create" in tools[1] and "feat-cart.wt-probe" in tools[1]
    assert "--delete" in tools[2] and "feat-cart.wt-probe" in tools[2]


def test_verify_fails_when_broker_does_not_answer(fake: FakeProc) -> None:
    fake.on("kafka-broker-api-versions.sh", proc.Result(1, "", "down"))
    check = provider().verify(ctx())
    assert not check.ok and "broker" in check.detail


def test_verify_fails_when_probe_topic_cannot_be_created(fake: FakeProc) -> None:
    fake.on("--create", proc.Result(1, "", "boom"))
    check = provider(topic_prefix=True).verify(ctx())
    assert not check.ok and "probe" in check.detail
    assert "--delete" in fake.tools()[-1]  # still tries to clean up


def test_destroy_deletes_groups_and_exact_prefix_topics(fake: FakeProc) -> None:
    fake.on(
        "--list",
        proc.Result(0, "feat-cart.a\nfeat-cart.b\nfeat-cart-x.a\nother\nfeat.c\n", ""),
    )
    provider(topic_prefix=True).destroy(ctx())
    tools = fake.tools()
    groups = [t for t in tools if t[0] == "kafka-consumer-groups.sh"]
    assert len(groups) == 1 and "--delete" in groups[0] and "worker-feat-cart" in groups[0]
    deleted = [t[t.index("--topic") + 1] for t in tools if "--delete" in t and "--topic" in t]
    assert deleted == ["feat-cart.a", "feat-cart.b"]


def test_destroy_of_feat_never_touches_feat_x_topics(fake: FakeProc) -> None:
    fake.on("--list", proc.Result(0, "feat-x.a\nfeat.a\n", ""))
    provider(topic_prefix=True).destroy(ctx("feat"))
    deleted = [
        t[t.index("--topic") + 1] for t in fake.tools() if "--topic" in t and "--delete" in t
    ]
    assert deleted == ["feat.a"]


def test_destroy_without_topic_prefix_deletes_only_groups(fake: FakeProc) -> None:
    provider().destroy(ctx())
    assert [t[0] for t in fake.tools()] == ["kafka-consumer-groups.sh"]


def test_destroy_tolerates_missing_group(fake: FakeProc) -> None:
    fake.on("kafka-consumer-groups.sh", proc.Result(1, "", "Group worker-feat-cart does not exist"))
    provider().destroy(ctx())


NOT_EMPTY = proc.Result(
    0,
    "Error: Deletion of some consumer groups failed:\n"
    "* Group 'worker-feat-cart' could not be deleted due to: "
    "java.util.concurrent.ExecutionException: "
    "org.apache.kafka.common.errors.GroupNotEmptyException: The group is not empty.\n",
    "",
)


class Clock:
    """A monotonic clock that only moves when the code sleeps, so retries cost no real time."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        monkeypatch.setattr("wt.providers.kafka.time.monotonic", lambda: self.now)
        monkeypatch.setattr("wt.providers.kafka.time.sleep", self.sleep)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


def group_deletes(fake: FakeProc) -> list[list[str]]:
    return [t for t in fake.tools() if t[0] == "kafka-consumer-groups.sh"]


def test_destroy_retries_while_a_consumer_is_still_a_member_then_succeeds(
    fake: FakeProc, clock: Clock
) -> None:
    results = iter(
        [NOT_EMPTY, NOT_EMPTY, proc.Result(0, "Deletion of requested consumer groups", "")]
    )
    fake.on_each("kafka-consumer-groups.sh", lambda: next(results))
    provider().destroy(ctx())
    assert len(group_deletes(fake)) == 3
    assert clock.sleeps == [kafka.GROUP_RETRY_INTERVAL] * 2


def test_destroy_accepts_a_group_that_disappears_while_retrying(
    fake: FakeProc, clock: Clock
) -> None:
    results = iter([NOT_EMPTY, proc.Result(1, "", "Group worker-feat-cart does not exist")])
    fake.on_each("kafka-consumer-groups.sh", lambda: next(results))
    provider().destroy(ctx())
    assert len(group_deletes(fake)) == 2


def test_destroy_raises_when_members_remain_after_the_retry_window(
    fake: FakeProc, clock: Clock
) -> None:
    fake.on("kafka-consumer-groups.sh", NOT_EMPTY)
    with pytest.raises(WtError) as err:
        provider().destroy(ctx())
    assert str(err.value) == "consumer group worker-feat-cart still has members"
    assert sum(clock.sleeps) == kafka.GROUP_RETRY_WINDOW
    assert kafka.GROUP_RETRY_WINDOW == 20 and kafka.GROUP_RETRY_INTERVAL == 2


def test_a_group_with_members_stops_destroy_before_it_touches_the_topics(
    fake: FakeProc, clock: Clock
) -> None:
    fake.on("kafka-consumer-groups.sh", NOT_EMPTY)
    with pytest.raises(WtError):
        provider(topic_prefix=True).destroy(ctx())
    assert not [t for t in fake.tools() if t[0] == "kafka-topics.sh"]


def test_destroy_does_not_retry_when_the_first_delete_works(fake: FakeProc, clock: Clock) -> None:
    provider().destroy(ctx())
    assert len(group_deletes(fake)) == 1 and clock.sleeps == []


def test_destroy_tells_once_that_it_is_waiting_for_the_consumer(
    fake: FakeProc, clock: Clock
) -> None:
    results = iter([NOT_EMPTY, NOT_EMPTY, proc.Result(0, "", "")])
    fake.on_each("kafka-consumer-groups.sh", lambda: next(results))
    messages: list[str] = []
    KafkaProvider(config(), messages.append).destroy(ctx())
    assert len(messages) == 1 and "worker-feat-cart" in messages[0]


def test_destroy_raises_on_other_group_errors(fake: FakeProc) -> None:
    fake.on("kafka-consumer-groups.sh", proc.Result(1, "", "connection refused"))
    with pytest.raises(proc.CommandError):
        provider().destroy(ctx())


def test_destroy_is_safe_when_no_topics(fake: FakeProc) -> None:
    provider(topic_prefix=True).destroy(ctx())
    assert not [t for t in fake.tools() if t[0] == "kafka-topics.sh" and "--delete" in t]


def test_destroy_uses_recorded_resources_when_config_is_gone(fake: FakeProc) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart",
            slug="feat-cart",
            providers={"kafka": ["old-feat-cart", "feat-cart.", "*", "bad name"]},
        )
    fake.on("--list", proc.Result(0, "feat-cart.a\n", ""))
    bare = KafkaProvider(
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
    bare.destroy(c)
    tools = fake.tools()
    groups = [t[t.index("--group") + 1] for t in tools if t[0] == "kafka-consumer-groups.sh"]
    assert groups == ["old-feat-cart"]
    assert any("feat-cart.a" in t for t in tools)
