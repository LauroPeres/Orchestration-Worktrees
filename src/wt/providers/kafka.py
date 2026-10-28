"""Shared single-node Kafka and per-branch groups and topics (docs/SPEC.md §18)."""

import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wt import compose, proc
from wt.config import Config
from wt.config import Kafka as KafkaConfig
from wt.errors import WtError
from wt.paths import env_port, instance, wt_home
from wt.providers import server
from wt.providers.base import BranchContext, Check, Kind, Resources

DEFAULT_IMAGE = "apache/kafka:3.9.1"
DEFAULT_PORT = 59092
CLUSTER_ID = "wtKafkaSingleNode0001Q"  # any fixed 22-char base64 id; one node, one cluster
BIN = "/opt/kafka/bin"
BOOTSTRAP = "localhost:9092"
NAME = re.compile(r"^[a-z0-9._-]+$")
PREFIX = re.compile(r"^[a-z0-9-]+\.$")  # what wt builds as a topic prefix


def container() -> str:
    """Name of the shared Kafka container (also its compose project)."""
    return f"{instance()}-kafka"


def volume() -> str:
    return f"{instance()}-kafka-data"


def image() -> str:
    return os.environ.get("WT_KAFKA_IMAGE", DEFAULT_IMAGE)


def host_port() -> int:
    return env_port("WT_KAFKA_PORT", DEFAULT_PORT)


def server_dir() -> Path:
    return wt_home() / "kafka"


def compose_doc() -> dict[str, Any]:
    port = host_port()
    return {
        "name": container(),
        "services": {
            "kafka": {
                "image": image(),
                "container_name": container(),
                "restart": "unless-stopped",
                "ports": [f"127.0.0.1:{port}:19092"],
                "volumes": [f"{volume()}:/var/lib/kafka/data"],
                "networks": [compose.NETWORK],
                "environment": {
                    "CLUSTER_ID": CLUSTER_ID,
                    "KAFKA_NODE_ID": "1",
                    "KAFKA_PROCESS_ROLES": "broker,controller",
                    "KAFKA_LISTENERS": "INTERNAL://:9092,EXTERNAL://:19092,CONTROLLER://:9093",
                    "KAFKA_ADVERTISED_LISTENERS": (
                        f"INTERNAL://{container()}:9092,EXTERNAL://127.0.0.1:{port}"
                    ),
                    "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": (
                        "INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT"
                    ),
                    "KAFKA_INTER_BROKER_LISTENER_NAME": "INTERNAL",
                    "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
                    "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@localhost:9093",
                    "KAFKA_LOG_DIRS": "/var/lib/kafka/data",
                    "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "true",
                    "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
                    "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
                    "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
                    "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
                },
            }
        },
        "networks": {compose.NETWORK: {"external": True, "name": compose.network_name()}},
        "volumes": {volume(): {"name": volume()}},
    }


def tool(script: str, *args: str, check: bool = True) -> proc.Result:
    """Run one Kafka CLI tool inside the server container, against the in-container listener."""
    return proc.run(
        ["docker", "exec", container(), f"{BIN}/{script}", "--bootstrap-server", BOOTSTRAP, *args],
        check=check,
    )


def wait_ready(timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if tool("kafka-broker-api-versions.sh", check=False).ok:
            return
        if time.monotonic() >= deadline:
            raise WtError(f"Kafka did not become ready in {timeout:.0f}s ({container()})")
        time.sleep(1)


def ensure_server(timeout: int = 90) -> None:
    server.up(container(), server_dir(), compose_doc(), timeout)
    wait_ready(timeout)


def _checked(name: str) -> str:
    if not NAME.match(name):
        raise WtError(f"kafka: invalid name {name!r}; use lowercase letters, digits, '.', '_', '-'")
    return name


class KafkaProvider:
    name = "kafka"

    def __init__(self, cfg: Config, log: Callable[[str], None] = lambda message: None) -> None:
        # No I/O here: providers are built on every render. `cfg` may lack shared.kafka
        # when `down` runs after the section was removed.
        self.cfg: KafkaConfig | None = cfg.shared.kafka
        self.log = log

    @property
    def groups(self) -> list[str]:
        return self.cfg.groups if self.cfg is not None else []

    @property
    def topic_prefix(self) -> bool:
        return self.cfg is not None and self.cfg.topic_prefix

    def prefix(self, ctx: BranchContext) -> str:
        """`feat/cart` -> `feat-cart.`; empty when topics are not prefixed."""
        return f"{ctx.names.slug}." if self.topic_prefix else ""

    def group(self, name: str, ctx: BranchContext) -> str:
        if name not in self.groups:
            raise WtError(
                f"kafka.group({name!r}): unknown group; configured: "
                f"{', '.join(sorted(self.groups)) or 'none'}"
            )
        return f"{_checked(name)}-{ctx.names.slug}"

    def topic(self, name: str, ctx: BranchContext) -> str:
        return f"{self.prefix(ctx)}{_checked(name)}"

    def ensure_server(self) -> None:
        ensure_server()

    def preflight(self, ctx: BranchContext) -> None:
        pass

    def create(self, ctx: BranchContext) -> Resources:
        names = [self.group(g, ctx) for g in self.groups]
        prefix = self.prefix(ctx)
        if prefix:
            names.append(prefix)
        return Resources(names)

    def verify(self, ctx: BranchContext) -> Check:
        if not tool("kafka-broker-api-versions.sh", check=False).ok:
            return Check(False, f"Kafka broker did not answer ({container()})")
        prefix = self.prefix(ctx)
        if not prefix:
            return Check(True, "Kafka broker answers")
        probe = f"{prefix}wt-probe"
        try:
            created = tool(
                "kafka-topics.sh", "--create", "--if-not-exists", "--topic", probe, check=False
            )
        finally:
            tool("kafka-topics.sh", "--delete", "--if-exists", "--topic", probe, check=False)
        if not created.ok:
            return Check(False, f"probe topic {probe} could not be created")
        return Check(True, f"Kafka broker answers; topics under {prefix} work")

    def destroy(self, ctx: BranchContext) -> None:
        groups = [self.group(g, ctx) for g in self.groups]
        prefixes = [self.prefix(ctx)] if self.prefix(ctx) else []
        bs = ctx.state.load().branches.get(ctx.branch)
        if bs is not None:
            for recorded in bs.providers.get(self.name, []):
                (prefixes if recorded.endswith(".") else groups).append(recorded)
        for group in dict.fromkeys(groups):
            if NAME.match(group):
                self._delete_group(group)
        for prefix in dict.fromkeys(prefixes):
            if PREFIX.match(prefix):
                self._delete_topics(prefix)

    def _delete_group(self, group: str) -> None:
        result = tool("kafka-consumer-groups.sh", "--delete", "--group", group, check=False)
        output = f"{result.stdout}{result.stderr}"
        if "GroupNotEmpty" in output:
            # The tool exits 0 here; a live consumer keeps the group, so warn, never abort `down`.
            self.log(
                f"warning: consumer group {group} was not deleted: a consumer is still connected"
            )
        elif not result.ok and "does not exist" not in output:
            raise proc.CommandError(
                ["kafka-consumer-groups.sh", "--delete", "--group", group], result
            )

    def _delete_topics(self, prefix: str) -> None:
        listing = tool("kafka-topics.sh", "--list").stdout.splitlines()
        for topic in listing:
            # Exact prefix match here, never a broker-side pattern: `feat.` != `feat-x.`.
            if topic.startswith(prefix) and NAME.match(topic):
                tool("kafka-topics.sh", "--delete", "--topic", topic)

    def template_vars(self, ctx: BranchContext, kind: Kind) -> dict[str, Any]:
        host = f"{container()}:9092" if kind == "compose" else f"127.0.0.1:{host_port()}"
        return {
            "kafka": {
                "bootstrap": host,
                "group": lambda name: self.group(name, ctx),
                "topic": lambda name: self.topic(name, ctx),
            }
        }
