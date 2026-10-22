import stat
from collections.abc import Sequence

import pytest

from wt import compose, proc
from wt.config import Config
from wt.errors import WtError
from wt.naming import Names
from wt.providers import postgres
from wt.providers.base import BranchContext
from wt.providers.postgres import PostgresProvider
from wt.state import BranchState, StateStore


def config(**pg: object) -> Config:
    return Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": {"postgres": {"databases": ["shop"], **pg}},
        }
    )


class FakeProc:
    """Stands in for wt.proc.run. `rules` maps a substring of the command line to a Result."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.rules: list[tuple[str, list[proc.Result]]] = []

    def on(self, needle: str, *results: proc.Result) -> None:
        self.rules.append((needle, list(results)))

    def sql(self) -> list[str]:
        """The -c argument of every psql call, in order."""
        return [c[c.index("-c") + 1] for c in self.calls if "psql" in c]

    def __call__(self, args: Sequence[str], *, check: bool = True, **kwargs: object) -> proc.Result:
        self.calls.append(list(args))
        line = " ".join(args)
        result = proc.Result(0, "", "")
        for needle, queue in self.rules:
            if needle in line:
                result = queue.pop(0) if len(queue) > 1 else queue[0]
                break
        if check and not result.ok:
            raise proc.CommandError(args, result)
        return result


def out(text: str) -> proc.Result:
    return proc.Result(0, text, "")


def err(text: str) -> proc.Result:
    return proc.Result(1, "", text)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProc:
    f = FakeProc()
    monkeypatch.setattr(proc, "run", f)
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr("wt.providers.postgres.time.sleep", lambda s: None)
    return f


def ctx(branch: str = "feat/cart", base: str = "main") -> BranchContext:
    return BranchContext(Names("shop", branch, "shop.localhost"), branch, base, StateStore("shop"))


# ---- server ---------------------------------------------------------------------------


def test_names_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WT_INSTANCE", raising=False)
    monkeypatch.delenv("WT_POSTGRES_IMAGE", raising=False)
    monkeypatch.delenv("WT_POSTGRES_PORT", raising=False)
    assert postgres.container() == "wt-postgres"
    assert postgres.volume() == "wt-postgres-data"
    assert postgres.image() == "postgres:17-alpine"
    assert postgres.host_port() == 55432
    monkeypatch.setenv("WT_INSTANCE", "t1")
    monkeypatch.setenv("WT_POSTGRES_IMAGE", "postgres:16")
    monkeypatch.setenv("WT_POSTGRES_PORT", "55499")
    assert postgres.container() == "t1-postgres"
    assert postgres.volume() == "t1-postgres-data"
    assert postgres.image() == "postgres:16"
    assert postgres.host_port() == 55499


def test_password_created_once_with_mode_0600() -> None:
    first = postgres.password()
    path = postgres.password_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(first) >= 20
    assert postgres.password() == first
    assert path.read_text().strip() == first


def test_compose_doc_publishes_on_loopback_and_joins_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WT_POSTGRES_PORT", "55499")
    doc = postgres.compose_doc()
    svc = doc["services"]["postgres"]
    assert svc["ports"] == ["127.0.0.1:55499:5432"]
    assert svc["environment"]["POSTGRES_USER"] == "wt"
    assert svc["networks"] == [compose.NETWORK]
    assert doc["volumes"] == {postgres.volume(): {"name": postgres.volume()}}


def test_ensure_server_starts_waits_and_repairs(fake: FakeProc) -> None:
    fake.on("FROM pg_database", out("shop__feat_cart\n"))
    postgres.ensure_server(["shop"])
    up = next(c for c in fake.calls if c[:2] == ["docker", "compose"])
    assert "up" in up and "--wait" in up
    compose_file = postgres.server_dir() / "compose.yml"
    assert stat.S_IMODE(compose_file.stat().st_mode) == 0o600  # holds the password
    cmds = [" ".join(c) for c in fake.calls]
    ready = next(i for i, c in enumerate(cmds) if "pg_isready" in c and "exec" in c)
    assert ready > cmds.index(" ".join(up))
    assert fake.sql()[-1] == 'ALTER DATABASE "shop__feat_cart" WITH ALLOW_CONNECTIONS true'
    assert "NOT datallowconn" in fake.sql()[0]


def test_repair_only_matches_configured_databases(fake: FakeProc) -> None:
    postgres.repair_connections(["shop", "my_app"])
    sql = fake.sql()[0]
    assert "datname LIKE 'shop\\_\\_%'" in sql
    assert "datname LIKE 'my\\_app\\_\\_%'" in sql
    assert "position(" not in sql
    fake.calls.clear()
    postgres.repair_connections([])
    assert fake.calls == []


def test_password_fixes_mode_and_regenerates_empty_file() -> None:
    path = postgres.password_file()
    path.parent.mkdir(parents=True)
    path.write_text("secret\n")
    path.chmod(0o644)
    assert postgres.password() == "secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("\n")
    fresh = postgres.password()
    assert fresh and fresh != "secret"
    assert path.read_text().strip() == fresh


def test_wait_ready_gives_up(fake: FakeProc, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.on("pg_isready", err("no response"))
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr("wt.providers.postgres.time.monotonic", lambda: float(next(clock)))
    with pytest.raises(WtError, match="did not become ready"):
        postgres.wait_ready(60)


def test_psql_command_shape(fake: FakeProc) -> None:
    postgres.psql("SELECT 1", "shop__main")
    cmd = fake.calls[0]
    assert cmd[:3] == ["docker", "exec", postgres.container()]
    assert cmd[3:8] == ["psql", "-v", "ON_ERROR_STOP=1", "-U", "wt"]
    assert cmd[cmd.index("-d") + 1] == "shop__main"
    assert cmd[-2:] == ["-c", "SELECT 1"]


@pytest.mark.parametrize("bad", ["", "Shop", 'a"b', "a-b", "a;drop", "x" * 64, "a b"])
def test_rejected_identifiers(bad: str) -> None:
    with pytest.raises(WtError, match="safe Postgres identifier"):
        postgres.ident(bad)


def test_hostile_database_name_in_config_is_rejected(fake: FakeProc) -> None:
    provider = PostgresProvider(config(databases=['shop"; DROP DATABASE postgres; --']))
    with pytest.raises(WtError, match="safe Postgres identifier"):
        provider.create(ctx())
    with pytest.raises(WtError, match="safe Postgres identifier"):
        provider.template_vars(ctx(), "compose")
    assert fake.calls == []


def test_long_names_are_fitted_to_valid_identifiers() -> None:
    long_branch = "feature/" + "very-long-name-" * 6
    name = postgres.db_name("shop", Names("shop", long_branch, "shop.localhost"))
    assert len(name) <= 63
    postgres.ident(name)


# ---- create ---------------------------------------------------------------------------


def test_base_branch_creates_empty_if_missing(fake: FakeProc) -> None:
    provider = PostgresProvider(config())
    res = provider.create(ctx("main"))
    assert res.names == ["shop__main"]
    assert fake.sql()[-1] == 'CREATE DATABASE "shop__main"'


def test_existing_branch_database_is_kept(fake: FakeProc) -> None:
    fake.on("datname = 'shop__feat_cart'", out("1\n"))
    provider = PostgresProvider(config())
    assert provider.create(ctx()).names == ["shop__feat_cart"]
    assert not any("CREATE DATABASE" in s for s in fake.sql())
    assert provider.verify(ctx()).ok


def test_missing_base_creates_empty_and_warns(fake: FakeProc) -> None:
    provider = PostgresProvider(config())
    provider.create(ctx())
    assert fake.sql()[-1] == 'CREATE DATABASE "shop__feat_cart"'
    check = provider.verify(ctx())
    assert check.ok
    assert any("run wt up main first to clone real data" in w for w in check.warnings)


def test_template_clone_sql_order(fake: FakeProc) -> None:
    fake.on("datname = 'shop__main'", out("1\n"))
    provider = PostgresProvider(config())
    provider.create(ctx())
    sql = [s for s in fake.sql() if "FROM pg_database" not in s]
    assert sql == [
        'ALTER DATABASE "shop__main" WITH ALLOW_CONNECTIONS false',
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = 'shop__main' AND pid <> pg_backend_pid()",
        'CREATE DATABASE "shop__feat_cart" TEMPLATE "shop__main"',
        'ALTER DATABASE "shop__main" WITH ALLOW_CONNECTIONS true',
    ]


def test_template_clone_retries_when_source_is_busy(fake: FakeProc) -> None:
    fake.on("datname = 'shop__main'", out("1\n"))
    busy = err("source database is being accessed by other users")
    fake.on("TEMPLATE", busy, busy, out(""))
    PostgresProvider(config()).create(ctx())
    creates = [s for s in fake.sql() if s.startswith("CREATE DATABASE")]
    assert len(creates) == 3
    assert fake.sql()[-1].endswith("ALLOW_CONNECTIONS true")


def test_template_clone_reopens_source_when_it_fails(fake: FakeProc) -> None:
    fake.on("datname = 'shop__main'", out("1\n"))
    busy = err("source database is being accessed by other users")
    fake.on("TEMPLATE", busy)
    with pytest.raises(WtError, match="accessed by other users"):
        PostgresProvider(config()).create(ctx())
    creates = [s for s in fake.sql() if s.startswith("CREATE DATABASE")]
    assert len(creates) == 3  # gave up after three tries
    assert fake.sql()[-1] == 'ALTER DATABASE "shop__main" WITH ALLOW_CONNECTIONS true'


def test_failed_reopen_does_not_hide_the_clone_error(fake: FakeProc) -> None:
    fake.on("datname = 'shop__main'", out("1\n"))
    fake.on("TEMPLATE", err("permission denied"))
    fake.on("ALLOW_CONNECTIONS true", err("connection lost"))
    logs: list[str] = []
    with pytest.raises(WtError, match="permission denied"):
        PostgresProvider(config(), log=logs.append).create(ctx())
    assert any("could not reopen shop__main" in m for m in logs)


def test_failed_reopen_after_a_good_clone_is_an_error(fake: FakeProc) -> None:
    fake.on("datname = 'shop__main'", out("1\n"))
    fake.on("ALLOW_CONNECTIONS true", err("connection lost"))
    with pytest.raises(WtError, match="connection lost"):
        PostgresProvider(config()).create(ctx())


def test_template_clone_other_error_is_not_retried(fake: FakeProc) -> None:
    fake.on("datname = 'shop__main'", out("1\n"))
    fake.on("TEMPLATE", err("permission denied"))
    with pytest.raises(WtError, match="permission denied"):
        PostgresProvider(config()).create(ctx())
    assert len([s for s in fake.sql() if s.startswith("CREATE DATABASE")]) == 1
    assert fake.sql()[-1].endswith("ALLOW_CONNECTIONS true")


def test_dump_restore_clone(fake: FakeProc) -> None:
    fake.on("datname = 'shop__main'", out("1\n"))
    provider = PostgresProvider(config(strategy="dump-restore"))
    provider.create(ctx())
    sql = fake.sql()
    assert 'CREATE DATABASE "shop__feat_cart"' in sql
    assert not any("ALLOW_CONNECTIONS false" in s for s in sql)  # base is not blocked
    dump = next(c for c in fake.calls if "pg_dump" in " ".join(c))
    assert dump[-2:] == ["shop__main", "shop__feat_cart"]
    assert "pg_restore" in dump[dump.index("-c") + 1]


def test_dump_restore_failure_drops_the_half_clone(fake: FakeProc) -> None:
    fake.on("datname = 'shop__main'", out("1\n"))
    fake.on("pg_dump", err("pg_restore: error"))
    with pytest.raises(WtError, match="pg_restore"):
        PostgresProvider(config(strategy="dump-restore")).create(ctx())
    assert fake.sql()[-1] == 'DROP DATABASE IF EXISTS "shop__feat_cart" WITH (FORCE)'


# ---- verify ---------------------------------------------------------------------------


def cloned(provider: PostgresProvider) -> None:
    provider._cloned = [("shop__main", "shop__feat_cart")]


def stub_tables_and_counts(
    provider: PostgresProvider,
    monkeypatch: pytest.MonkeyPatch,
    tables: dict[str, list[str]],
    counts: dict[str, dict[str, int]],
) -> None:
    monkeypatch.setattr(provider, "_tables", lambda db: tables[db])
    monkeypatch.setattr(provider, "_counts", lambda db: counts[db])


def test_verify_ok(fake: FakeProc, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = PostgresProvider(config())
    cloned(provider)
    same = {"shop__main": ["public.items"], "shop__feat_cart": ["public.items"]}
    n = {"shop__main": {"public.items": 3}, "shop__feat_cart": {"public.items": 3}}
    stub_tables_and_counts(provider, monkeypatch, same, n)
    assert provider.verify(ctx()).ok
    assert fake.calls == []


def test_verify_template_count_mismatch_drops_clone(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = PostgresProvider(config())
    cloned(provider)
    same = {"shop__main": ["public.items"], "shop__feat_cart": ["public.items"]}
    n = {"shop__main": {"public.items": 3}, "shop__feat_cart": {"public.items": 2}}
    stub_tables_and_counts(provider, monkeypatch, same, n)
    check = provider.verify(ctx())
    assert not check.ok and "public.items" in check.detail
    assert fake.sql() == ['DROP DATABASE IF EXISTS "shop__feat_cart" WITH (FORCE)']


def test_verify_dump_restore_count_mismatch_only_warns(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = PostgresProvider(config(strategy="dump-restore"))
    cloned(provider)
    same = {"shop__main": ["public.items"], "shop__feat_cart": ["public.items"]}
    n = {"shop__main": {"public.items": 4}, "shop__feat_cart": {"public.items": 3}}
    stub_tables_and_counts(provider, monkeypatch, same, n)
    check = provider.verify(ctx())
    assert check.ok and any("public.items" in w for w in check.warnings)
    assert fake.calls == []


@pytest.mark.parametrize("strategy", ["template", "dump-restore"])
def test_verify_table_list_mismatch_fails(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch, strategy: str
) -> None:
    provider = PostgresProvider(config(strategy=strategy))
    cloned(provider)
    tables = {"shop__main": ["public.a", "public.b"], "shop__feat_cart": ["public.a"]}
    stub_tables_and_counts(provider, monkeypatch, tables, {})
    check = provider.verify(ctx())
    assert not check.ok and "table list" in check.detail


def test_verify_disabled(fake: FakeProc) -> None:
    provider = PostgresProvider(config(verify=False))
    cloned(provider)
    assert provider.verify(ctx()).ok
    assert fake.calls == []


def test_counts_parses_psql_rows(fake: FakeProc) -> None:
    fake.on("query_to_xml", out("public.items|3\npublic.users|0\n"))
    provider = PostgresProvider(config())
    assert provider._counts("shop__main") == {"public.items": 3, "public.users": 0}


# ---- destroy --------------------------------------------------------------------------


def test_destroy_drops_branch_databases_with_force(fake: FakeProc) -> None:
    PostgresProvider(config()).destroy(ctx())
    assert fake.sql() == ['DROP DATABASE IF EXISTS "shop__feat_cart" WITH (FORCE)']


def test_destroy_never_drops_base_databases() -> None:
    logs: list[str] = []
    provider = PostgresProvider(config(), log=logs.append)
    provider.destroy(ctx("main"))
    assert logs and "keeping" in logs[0] and "shop__main" in logs[0]


def test_destroy_works_without_postgres_config_using_recorded_names(fake: FakeProc) -> None:
    cfg = config()
    cfg.shared.postgres = None
    with StateStore("shop").edit() as state:
        bs = BranchState(branch="feat/cart", slug="feat-cart")
        bs.providers["postgres"] = ["recorded_db"]
        state.branches["feat/cart"] = bs
        bs.provider_base["postgres"] = "main"
    PostgresProvider(cfg).destroy(ctx())
    assert fake.sql() == ['DROP DATABASE IF EXISTS "recorded_db" WITH (FORCE)']


def record(branch: str, base: str | None, dbs: list[str]) -> None:
    with StateStore("shop").edit() as state:
        bs = BranchState(branch=branch, slug=branch)
        bs.providers["postgres"] = dbs
        if base is not None:
            bs.provider_base["postgres"] = base
        state.branches[branch] = bs


def test_destroy_refuses_when_nothing_says_which_branch_is_base(fake: FakeProc) -> None:
    cfg = config()
    cfg.shared.postgres = None  # removed after `up`, and the state has no base recorded
    logs: list[str] = []
    record("develop", None, ["shop__develop"])
    PostgresProvider(cfg, log=logs.append).destroy(ctx("develop"))
    assert fake.calls == []
    assert logs and "keeping" in logs[0]


def test_destroy_keeps_recorded_base_after_config_removed(fake: FakeProc) -> None:
    cfg = config()
    cfg.shared.postgres = None
    record("develop", "develop", ["shop__develop"])
    PostgresProvider(cfg).destroy(ctx("develop"))  # ctx falls back to base "main"
    assert fake.calls == []


def test_destroy_keeps_recorded_base_after_base_branch_changed(fake: FakeProc) -> None:
    record("develop", "develop", ["shop__develop"])
    # config now says base_branch is main, so ctx says so too
    PostgresProvider(config(base_branch="main")).destroy(ctx("develop", base="main"))
    assert fake.calls == []


def test_create_records_the_base_branch_in_state(fake: FakeProc) -> None:
    record("feat/cart", None, [])
    PostgresProvider(config()).create(ctx())
    bs = StateStore("shop").load().branches["feat/cart"]
    assert bs.provider_base == {"postgres": "main"}


# ---- template vars --------------------------------------------------------------------


def test_template_vars_per_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_POSTGRES_PORT", "55499")
    provider = PostgresProvider(config())
    pw = postgres.password()
    compose_vars = provider.template_vars(ctx(), "compose")["postgres"]
    run_vars = provider.template_vars(ctx(), "run")["postgres"]
    assert compose_vars["url"]("shop") == f"postgresql://wt:{pw}@wt-postgres:5432/shop__feat_cart"
    assert run_vars["url"]("shop") == f"postgresql://wt:{pw}@127.0.0.1:55499/shop__feat_cart"
    assert compose_vars["name"]("shop") == "shop__feat_cart"


def test_unknown_database_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    vars_ = PostgresProvider(config()).template_vars(ctx(), "run")["postgres"]
    with pytest.raises(WtError, match="unknown database"):
        vars_["url"]("nope")


def test_templates_render_through_jinja() -> None:
    from wt.templating import render

    provider = PostgresProvider(config())
    context = provider.template_vars(ctx(), "compose")
    assert render("{{ postgres.name('shop') }}", context, "env.A") == "shop__feat_cart"
