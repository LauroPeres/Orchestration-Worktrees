"""Kafka provider against a real broker: `pytest -m docker`. Isolated by WT_INSTANCE."""

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

from wt import compose
from wt.config import Config
from wt.errors import WtError
from wt.naming import Names
from wt.providers import kafka
from wt.providers.base import BranchContext
from wt.providers.kafka import KafkaProvider
from wt.state import BranchState, StateStore

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
    instance = f"wtkf{free_port()}"
    mp.setenv("WT_INSTANCE", instance)
    mp.setenv("WT_HOME", str(tmp_path_factory.mktemp("wt-home")))
    mp.setenv("WT_KAFKA_PORT", str(free_port()))
    mp.setenv("WT_HTTP_PORT", str(free_port()))
    mp.setenv("WT_HTTPS_PORT", str(free_port()))
    try:
        kafka.ensure_server()
        yield instance
    finally:
        docker("rm", "-f", "-v", kafka.container())
        docker("volume", "rm", "-f", kafka.volume())
        docker("network", "rm", compose.network_name())
        mp.undo()


def make(
    branch: str, topic_prefix: bool = True, project: str = "shop"
) -> tuple[KafkaProvider, BranchContext]:
    cfg = Config.model_validate(
        {
            "project": project,
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": {"kafka": {"groups": ["orders"], "topic_prefix": topic_prefix}},
        }
    )
    names = Names(project, branch, "shop.localhost")
    return KafkaProvider(cfg), BranchContext(names, branch, "main", StateStore(project))


def produce(topic: str, message: str) -> None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            kafka.container(),
            f"{kafka.BIN}/kafka-console-producer.sh",
            "--bootstrap-server",
            kafka.BOOTSTRAP,
            "--topic",
            topic,
        ],
        input=f"{message}\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def consume(topic: str, group: str) -> str:
    result = kafka.tool(
        "kafka-console-consumer.sh",
        "--topic",
        topic,
        "--group",
        group,
        "--from-beginning",
        "--max-messages",
        "1",
        "--timeout-ms",
        "30000",
    )
    return result.stdout.strip()


def topics() -> set[str]:
    return set(kafka.tool("kafka-topics.sh", "--list").stdout.split())


def groups() -> set[str]:
    return set(kafka.tool("kafka-consumer-groups.sh", "--list").stdout.split())


def wait_topics_gone(*gone: str) -> set[str]:
    deadline = time.monotonic() + 30
    while True:
        current = topics()
        if not current & set(gone) or time.monotonic() >= deadline:
            return current
        time.sleep(1)


def test_ensure_server_is_idempotent(server: str) -> None:
    produce("idem.t", "keep")
    kafka.ensure_server()
    kafka.ensure_server()
    assert "idem.t" in topics()
    kafka.tool("kafka-topics.sh", "--delete", "--topic", "idem.t")


def test_host_bootstrap_answers(server: str) -> None:
    provider, ctx = make("feat/cart")
    host, _, port = provider.template_vars(ctx, "run")["kafka"]["bootstrap"].partition(":")
    assert (host, int(port)) == ("127.0.0.1", kafka.host_port())
    with socket.create_connection((host, int(port)), timeout=10):
        pass


def test_destroy_deletes_only_the_branch_topics_and_groups(server: str) -> None:
    feat, feat_ctx = make("feat")
    longer, longer_ctx = make("feat-x")
    other, other_ctx = make("other")
    for provider, ctx in [(feat, feat_ctx), (longer, longer_ctx), (other, other_ctx)]:
        assert provider.create(ctx).names == [
            provider.group("orders", ctx),
            provider.prefix(ctx),
        ]
        check = provider.verify(ctx)
        assert check.ok, check.detail
        topic = provider.topic("events", ctx)
        group = provider.group("orders", ctx)
        produce(topic, f"hello-{ctx.branch}")
        assert consume(topic, group) == f"hello-{ctx.branch}"
    produce("plain", "unprefixed")
    feat_topic, longer_topic, other_topic = (
        p.topic("events", c)
        for p, c in [(feat, feat_ctx), (longer, longer_ctx), (other, other_ctx)]
    )
    feat_group, longer_group, other_group = (
        p.group("orders", c)
        for p, c in [(feat, feat_ctx), (longer, longer_ctx), (other, other_ctx)]
    )
    assert {feat_group, longer_group, other_group} <= groups()
    # The one literal per provider: a rename of the scheme is caught here.
    assert (feat_topic, feat_group) == ("shop.feat.events", "orders-shop.feat")

    feat.destroy(feat_ctx)
    feat.destroy(feat_ctx)  # safe to repeat
    current = wait_topics_gone(feat_topic)
    assert feat_topic not in current
    assert {longer_topic, other_topic, "plain"} <= current
    assert feat_group not in groups()
    assert {longer_group, other_group} <= groups()

    longer.destroy(longer_ctx)
    other.destroy(other_ctx)
    current = wait_topics_gone(longer_topic, other_topic)
    assert "plain" in current
    assert not {longer_topic, other_topic} & current
    kafka.tool("kafka-topics.sh", "--delete", "--topic", "plain")


def test_same_branch_in_two_projects_keeps_separate_topics_and_groups(server: str) -> None:
    shop, shop_ctx = make("main", project="shop")
    blog, blog_ctx = make("main", project="blog")
    mine = {}
    for provider, ctx in [(shop, shop_ctx), (blog, blog_ctx)]:
        provider.create(ctx)
        topic, group = provider.topic("events", ctx), provider.group("orders", ctx)
        produce(topic, ctx.names.project)
        assert consume(topic, group) == ctx.names.project
        mine[ctx.names.project] = (topic, group)
    assert mine["shop"] != mine["blog"]

    shop.destroy(shop_ctx)
    current = wait_topics_gone(mine["shop"][0])
    assert mine["shop"][0] not in current and mine["shop"][1] not in groups()
    assert mine["blog"][0] in current and mine["blog"][1] in groups()

    blog.destroy(blog_ctx)
    assert mine["blog"][1] not in groups()
    assert mine["blog"][0] not in wait_topics_gone(mine["blog"][0])


def test_destroy_cleans_a_branch_recorded_by_0_5_0_without_the_project(server: str) -> None:
    provider, ctx = make("legacy")
    other, other_ctx = make("legacy", project="blog")
    other.create(other_ctx)
    other_topic = other.topic("events", other_ctx)
    produce(other_topic, "keep")
    produce("legacy.events", "old")  # the 0.5.0 topic: `<slug>.<name>`
    assert consume("legacy.events", "orders-legacy") == "old"  # the 0.5.0 group
    with ctx.state.edit() as state:
        state.branches[ctx.branch] = BranchState(
            branch=ctx.branch,
            slug=ctx.names.slug,
            providers={"kafka": ["orders-legacy", "legacy."]},
        )

    provider.destroy(ctx)
    current = wait_topics_gone("legacy.events")
    assert "legacy.events" not in current
    assert "orders-legacy" not in groups()
    assert other_topic in current

    other.destroy(other_ctx)
    wait_topics_gone(other_topic)


def test_without_topic_prefix_destroy_deletes_only_groups(server: str) -> None:
    provider, ctx = make("feat/cart", topic_prefix=False)
    group = provider.group("orders", ctx)
    assert provider.create(ctx).names == [group]
    check = provider.verify(ctx)
    assert check.ok, check.detail
    produce("shared-topic", "msg")
    assert consume("shared-topic", group) == "msg"
    assert group in groups()

    provider.destroy(ctx)
    assert group not in groups()
    assert "shared-topic" in topics()
    kafka.tool("kafka-topics.sh", "--delete", "--topic", "shared-topic")


def test_destroy_raises_while_a_consumer_holds_the_group_then_removes_it(server: str) -> None:
    provider, ctx = make("feat/held", topic_prefix=False)
    messages: list[str] = []
    provider.log = messages.append
    group = provider.group("orders", ctx)
    topic = "held-topic"
    kafka.tool("kafka-topics.sh", "--create", "--topic", topic, check=True)
    consumer = subprocess.Popen(
        [
            "docker",
            "exec",
            kafka.container(),
            f"{kafka.BIN}/kafka-console-consumer.sh",
            "--bootstrap-server",
            kafka.BOOTSTRAP,
            "--topic",
            topic,
            "--group",
            group,
            # Outlives the 20 s retry window; it exits by itself because `docker exec` cannot
            # kill the in-container JVM.
            "--timeout-ms",
            "60000",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while group not in groups() and time.monotonic() < deadline:
            time.sleep(1)
        assert group in groups()
        time.sleep(3)
        with pytest.raises(WtError, match="still has members"):
            provider.destroy(ctx)
        assert group in groups()
        assert any(f"waiting for consumer group {group}" in m for m in messages)
    finally:
        consumer.wait(timeout=90)
    provider.destroy(ctx)
    assert group not in groups()
    kafka.tool("kafka-topics.sh", "--delete", "--topic", topic)
