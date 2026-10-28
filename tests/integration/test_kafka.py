"""Kafka provider against a real broker: `pytest -m docker`. Isolated by WT_INSTANCE."""

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

from wt import compose
from wt.config import Config
from wt.naming import Names
from wt.providers import kafka
from wt.providers.base import BranchContext
from wt.providers.kafka import KafkaProvider
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


def make(branch: str, topic_prefix: bool = True) -> tuple[KafkaProvider, BranchContext]:
    cfg = Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": {"kafka": {"groups": ["orders"], "topic_prefix": topic_prefix}},
        }
    )
    names = Names("shop", branch, "shop.localhost")
    return KafkaProvider(cfg), BranchContext(names, branch, "main", StateStore("shop"))


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
    assert {"orders-feat", "orders-feat-x", "orders-other"} <= groups()

    feat.destroy(feat_ctx)
    feat.destroy(feat_ctx)  # safe to repeat
    current = wait_topics_gone("feat.events")
    assert "feat.events" not in current
    assert {"feat-x.events", "other.events", "plain"} <= current
    assert "orders-feat" not in groups()
    assert {"orders-feat-x", "orders-other"} <= groups()

    longer.destroy(longer_ctx)
    other.destroy(other_ctx)
    current = wait_topics_gone("feat-x.events", "other.events")
    assert "plain" in current
    assert not {"feat-x.events", "other.events"} & current
    kafka.tool("kafka-topics.sh", "--delete", "--topic", "plain")


def test_without_topic_prefix_destroy_deletes_only_groups(server: str) -> None:
    provider, ctx = make("feat/cart", topic_prefix=False)
    assert provider.create(ctx).names == ["orders-feat-cart"]
    check = provider.verify(ctx)
    assert check.ok, check.detail
    produce("shared-topic", "msg")
    assert consume("shared-topic", provider.group("orders", ctx)) == "msg"
    assert "orders-feat-cart" in groups()

    provider.destroy(ctx)
    assert "orders-feat-cart" not in groups()
    assert "shared-topic" in topics()
    kafka.tool("kafka-topics.sh", "--delete", "--topic", "shared-topic")
