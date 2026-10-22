"""Postgres provider against a real server: `pytest -m docker`. Isolated by WT_INSTANCE."""

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from wt import compose
from wt.config import Config
from wt.naming import Names
from wt.providers import postgres
from wt.providers.base import BranchContext
from wt.providers.postgres import PostgresProvider
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
    instance = f"wtpg{free_port()}"
    mp.setenv("WT_INSTANCE", instance)
    mp.setenv("WT_HOME", str(tmp_path_factory.mktemp("wt-home")))
    mp.setenv("WT_POSTGRES_PORT", str(free_port()))
    mp.setenv("WT_HTTP_PORT", str(free_port()))
    mp.setenv("WT_HTTPS_PORT", str(free_port()))
    try:
        postgres.ensure_server(["shop"])
        yield instance
    finally:
        docker("rm", "-f", "-v", postgres.container())
        docker("volume", "rm", "-f", postgres.volume())
        docker("network", "rm", compose.network_name())
        mp.undo()


def make(
    server: str, tag: str, branch: str, **pg: object
) -> tuple[PostgresProvider, BranchContext]:
    """A provider whose base branch is unique to the test, so tests never share data."""
    base = f"base-{tag}"
    cfg = Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": {"postgres": {"databases": ["shop"], "base_branch": base, **pg}},
        }
    )
    names = Names("shop", branch, "shop.localhost")
    return PostgresProvider(cfg), BranchContext(names, branch, base, StateStore("shop"))


def base_of(ctx: BranchContext) -> BranchContext:
    names = Names(ctx.names.project, ctx.base_branch, ctx.names.domain)
    return BranchContext(names, ctx.base_branch, ctx.base_branch, ctx.state)


def db_of(ctx: BranchContext) -> str:
    return postgres.db_name("shop", ctx.names)


def scalar(db: str, sql: str) -> str:
    return postgres.psql(sql, db).stdout.strip()


def seed(provider: PostgresProvider, ctx: BranchContext) -> str:
    """Create the base database with two rows; return its name."""
    base = base_of(ctx)
    provider.create(base)
    name = db_of(base)
    postgres.psql("CREATE TABLE items (id serial PRIMARY KEY, name text)", name)
    postgres.psql("INSERT INTO items (name) VALUES ('a'), ('b')", name)
    return name


@pytest.mark.parametrize("strategy", ["template", "dump-restore"])
def test_clone_keeps_data_and_is_independent(server: str, strategy: str) -> None:
    provider, ctx = make(server, f"clone-{strategy}"[:20], "feat/cart", strategy=strategy)
    src = seed(provider, ctx)
    provider.create(ctx)
    dst = db_of(ctx)
    check = provider.verify(ctx)
    assert check.ok, check.detail
    assert scalar(dst, "SELECT count(*) FROM items") == "2"
    postgres.psql("INSERT INTO items (name) VALUES ('only-in-clone')", dst)
    assert scalar(src, "SELECT count(*) FROM items") == "2"
    postgres.psql("INSERT INTO items (name) VALUES ('only-in-base')", src)
    assert scalar(dst, "SELECT count(*) FROM items") == "3"
    # a second create keeps the clone, it never re-clones
    provider.create(ctx)
    assert scalar(dst, "SELECT count(*) FROM items") == "3"
    provider.destroy(ctx)
    provider.destroy(base_of(ctx))


def test_template_clone_while_a_client_is_connected(server: str) -> None:
    provider, ctx = make(server, "busy", "feat/cart")
    src = seed(provider, ctx)
    holder_cmd = ["docker", "exec", postgres.container(), "psql", "-U", "wt", "-d", src]
    holder = subprocess.Popen(
        [*holder_cmd, "-c", "SELECT pg_sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            sql = (
                "SELECT count(*) FROM pg_stat_activity "
                f"WHERE datname = '{src}' AND state = 'active'"
            )
            if scalar("postgres", sql) == "1":
                break
            time.sleep(0.2)
        provider.create(ctx)
        assert provider.verify(ctx).ok
        assert scalar(db_of(ctx), "SELECT count(*) FROM items") == "2"
        # the source accepts connections again after the clone
        assert scalar(src, "SELECT count(*) FROM items") == "2"
    finally:
        holder.kill()
        holder.wait()
    provider.destroy(ctx)
    provider.destroy(base_of(ctx))


def test_verify_catches_a_mismatch_and_drops_the_clone(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, ctx = make(server, "mismatch", "feat/cart")
    seed(provider, ctx)
    provider.create(ctx)
    dst = db_of(ctx)
    real = provider._counts
    monkeypatch.setattr(
        provider, "_counts", lambda db: {t: n + (db == dst) for t, n in real(db).items()}
    )
    check = provider.verify(ctx)
    assert not check.ok and "public.items" in check.detail
    assert scalar("postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{dst}'") == "0"
    provider.destroy(base_of(ctx))


def test_destroy_drops_the_branch_and_keeps_the_base(server: str, tmp_path: Path) -> None:
    provider, ctx = make(server, "destroy", "feat/cart")
    src = seed(provider, ctx)
    provider.create(ctx)
    dst = db_of(ctx)
    logs: list[str] = []
    provider.log = logs.append

    provider.destroy(ctx)
    exists = "SELECT count(*) FROM pg_database WHERE datname = '{}'"
    assert scalar("postgres", exists.format(dst)) == "0"
    provider.destroy(ctx)  # safe to repeat

    provider.destroy(base_of(ctx))
    assert scalar("postgres", exists.format(src)) == "1"
    assert any("keeping" in m for m in logs)
    postgres.psql(f'DROP DATABASE "{src}" WITH (FORCE)')


def test_template_vars_connect_from_the_host(server: str) -> None:
    provider, ctx = make(server, "vars", "feat/cart")
    provider.create(ctx)
    url = provider.template_vars(ctx, "run")["postgres"]["url"]("shop")
    assert f"127.0.0.1:{postgres.host_port()}/{db_of(ctx)}" in url
    provider.destroy(ctx)
