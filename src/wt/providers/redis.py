"""Shared Redis server and one key prefix per branch (docs/SPEC.md §18)."""

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wt import compose, proc
from wt.config import Config
from wt.config import Redis as RedisConfig
from wt.paths import env_port, instance, wt_home
from wt.providers import server
from wt.providers.base import (
    BranchContext,
    Check,
    Kind,
    Resources,
    branch_scope,
    keeps_one_segment_prefix,
    recorded_names,
)

DEFAULT_IMAGE = "redis:7-alpine"
DEFAULT_PORT = 56379
BATCH = 500
# What wt builds (`<project>:<slug>:`) and, for branches recorded by 0.5.0, `<slug>:`; keeps SCAN
# patterns free of globs.
PREFIX = re.compile(
    r"[a-z0-9-]+(:[a-z0-9-]+)?:"
)  # used with fullmatch: `$` accepts a final newline
SEPARATOR = ":"


def container() -> str:
    """Name of the shared Redis container (also its compose project)."""
    return f"{instance()}-redis"


def volume() -> str:
    return f"{instance()}-redis-data"


def image() -> str:
    return os.environ.get("WT_REDIS_IMAGE", DEFAULT_IMAGE)


def host_port() -> int:
    return env_port("WT_REDIS_PORT", DEFAULT_PORT)


def server_dir() -> Path:
    return wt_home() / "redis"


def compose_doc() -> dict[str, Any]:
    return {
        "name": container(),
        "services": {
            "redis": {
                "image": image(),
                "container_name": container(),
                "restart": "unless-stopped",
                "ports": [f"127.0.0.1:{host_port()}:6379"],
                "volumes": [f"{volume()}:/data"],
                "networks": [compose.NETWORK],
                "healthcheck": {
                    "test": ["CMD", "redis-cli", "ping"],
                    "interval": "2s",
                    "timeout": "2s",
                    "retries": 30,
                },
            }
        },
        "networks": {compose.NETWORK: {"external": True, "name": compose.network_name()}},
        "volumes": {volume(): {"name": volume()}},
    }


def cli(*args: str, check: bool = True) -> proc.Result:
    """Run one `redis-cli` command inside the server container."""
    return proc.run(["docker", "exec", container(), "redis-cli", *args], check=check)


def wait_ready(timeout: float = 60.0, total: float | None = None) -> None:
    server.wait_until(
        lambda: cli("ping", check=False).ok,
        timeout,
        total,
        lambda waited: f"Redis did not become ready in {waited:.0f}s ({container()})",
    )


def ensure_server(timeout: int = 60) -> None:
    server.start(container(), server_dir(), compose_doc(), timeout, wait_ready)


class RedisProvider:
    name = "redis"

    def __init__(self, cfg: Config, log: Callable[[str], None] = lambda message: None) -> None:
        # No I/O here: providers are built on every render. `cfg` may lack shared.redis
        # when `down` runs after the section was removed.
        self.cfg: RedisConfig | None = cfg.shared.redis
        self.log = log

    def prefix(self, ctx: BranchContext) -> str:
        """`feat/cart` in project `shop` -> `shop:feat-cart:`; empty when `prefix: false`."""
        if self.cfg is not None and not self.cfg.prefix:
            return ""
        return f"{branch_scope(ctx, SEPARATOR)}{SEPARATOR}"

    def ensure_server(self) -> None:
        ensure_server()

    def preflight(self, ctx: BranchContext) -> None:
        pass

    def create(self, ctx: BranchContext) -> Resources:
        prefix = self.prefix(ctx)
        return Resources([prefix] if prefix else [])

    def verify(self, ctx: BranchContext) -> Check:
        if not cli("ping", check=False).ok:
            return Check(False, f"Redis did not answer PING ({container()})")
        prefix = self.prefix(ctx)
        if not prefix:
            return Check(True, "Redis answers PING", ["no key prefix: branches share every key"])
        key, value = f"{prefix}__wt_probe__", "ok"
        try:
            cli("set", key, value)
            got = cli("get", key).stdout.strip()
        finally:
            cli("del", key, check=False)
        if got != value:
            return Check(False, f"probe key {key} did not read back")
        return Check(True, f"Redis answers; keys under {prefix} work")

    def destroy(self, ctx: BranchContext) -> None:
        wanted = recorded_names(ctx, self.name)
        current = self.prefix(ctx)
        if current:
            wanted.append(current)
        for prefix in dict.fromkeys(wanted):
            if PREFIX.fullmatch(prefix) and not keeps_one_segment_prefix(
                prefix, SEPARATOR, ctx, self.log
            ):
                self._delete_prefix(prefix)

    def _delete_prefix(self, prefix: str) -> None:
        scan = cli("--scan", "--pattern", f"{prefix}*")
        keys = [line for line in scan.stdout.splitlines() if line.startswith(prefix)]
        for start in range(0, len(keys), BATCH):
            cli("unlink", *keys[start : start + BATCH])

    def template_vars(self, ctx: BranchContext, kind: Kind) -> dict[str, Any]:
        host = f"{container()}:6379" if kind == "compose" else f"127.0.0.1:{host_port()}"
        return {"redis": {"url": f"redis://{host}", "prefix": self.prefix(ctx)}}
