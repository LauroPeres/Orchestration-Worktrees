"""Shared Postgres server and one set of cloned databases per branch (docs/SPEC.md §14)."""

import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wt import compose, proc
from wt.config import Config
from wt.config import Postgres as PostgresConfig
from wt.errors import WtError
from wt.naming import Names, fit, temp_name
from wt.paths import env_port, instance, wt_home
from wt.providers import server
from wt.providers.base import BranchContext, Check, Kind, Resources
from wt.state import atomic_write, file_lock

DEFAULT_IMAGE = "postgres:17-alpine"
DEFAULT_PORT = 55432
USER = "wt"
IDENT = re.compile(r"^[a-z0-9_]{1,63}$")
CLONE_ATTEMPTS = 3
CLONE_WAIT = 1.0
BUSY = "being accessed by other users"
SYSTEM_SCHEMAS = "('pg_catalog', 'information_schema')"


def container() -> str:
    """Name of the shared Postgres container (also its compose project)."""
    return f"{instance()}-postgres"


def volume() -> str:
    return f"{instance()}-postgres-data"


def image() -> str:
    return os.environ.get("WT_POSTGRES_IMAGE", DEFAULT_IMAGE)


def host_port() -> int:
    return env_port("WT_POSTGRES_PORT", DEFAULT_PORT)


def server_dir() -> Path:
    return wt_home() / "postgres"


def password_file() -> Path:
    return server_dir() / "password"


def password() -> str:
    """Generated once; the file is created with mode 0600 the first time it is needed."""
    path = password_file()
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            path.chmod(0o600)
            return existing
    # Own lock (not "postgres"): ensure_server calls this while holding that one.
    with file_lock("postgres-password"):
        existing = path.read_text().strip() if path.exists() else ""
        if not existing:
            existing = secrets.token_urlsafe(24)
            # mkstemp inside atomic_write creates the temp file with mode 0600
            atomic_write(path, existing + "\n")
        path.chmod(0o600)
        return existing


def ident(name: str) -> str:
    """Validate an identifier built by wt, then double-quote it for SQL."""
    if not IDENT.match(name):
        raise WtError(f"{name!r} is not a safe Postgres identifier (allowed: [a-z0-9_], max 63)")
    return f'"{name}"'


def literal(name: str) -> str:
    """A validated identifier as a string literal (for catalog lookups)."""
    ident(name)
    return f"'{name}'"


def db_name(database: str, names: Names) -> str:
    """`shop` on branch feat/cart -> `shop__feat_cart`, fitted to 63 chars."""
    ident(database)
    name = fit(f"{database}__{names.ident}", 63).replace("-", "_")
    ident(name)
    return name


def compose_doc() -> dict[str, Any]:
    return {
        "name": container(),
        "services": {
            "postgres": {
                "image": image(),
                "container_name": container(),
                "restart": "unless-stopped",
                "environment": {"POSTGRES_USER": USER, "POSTGRES_PASSWORD": password()},
                "ports": [f"127.0.0.1:{host_port()}:5432"],
                "volumes": [f"{volume()}:/var/lib/postgresql/data"],
                "networks": [compose.NETWORK],
                "healthcheck": {
                    # -h: the init-time server listens on the socket only; the real one also on TCP.
                    "test": ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", USER, "-d", "postgres"],
                    "interval": "2s",
                    "timeout": "2s",
                    "retries": 30,
                },
            }
        },
        "networks": {compose.NETWORK: {"external": True, "name": compose.network_name()}},
        "volumes": {volume(): {"name": volume()}},
    }


def psql(sql: str, db: str = "postgres", *, check: bool = True) -> proc.Result:
    """Run one SQL statement through `docker exec`. Output is unaligned, tuples only."""
    return proc.run(
        [
            "docker",
            "exec",
            container(),
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            USER,
            "-X",
            "-q",
            "-A",
            "-t",
            "-d",
            db,
            "-c",
            sql,
        ],
        check=check,
    )


def _rows(result: proc.Result) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.strip()]


def wait_ready(timeout: float = 60.0, total: float | None = None) -> None:
    server.wait_until(
        lambda: (
            proc.run(
                [
                    "docker",
                    "exec",
                    container(),
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    USER,
                    "-d",
                    "postgres",
                ],
                check=False,
            ).ok
        ),
        timeout,
        total,
        lambda waited: f"Postgres did not become ready in {waited:.0f}s ({container()})",
    )


def _like(database: str) -> str:
    """LIKE pattern for `<database>__*` with the underscores escaped."""
    return database.replace("_", "\\_") + "\\_\\_%"


def closed_databases(databases: list[str]) -> list[str]:
    """wt databases (`<db>__*` of the configured names) that have connections disabled.

    Only the configured database names are matched, so a user's own `a__b` is never touched.
    """
    if not databases:
        return []
    for database in databases:
        ident(database)
    like = " OR ".join(f"datname LIKE '{_like(d)}'" for d in databases)
    return _rows(
        psql(f"SELECT datname FROM pg_database WHERE ({like}) AND NOT datallowconn ORDER BY 1")
    )


def repair_connections(databases: list[str]) -> None:
    """Re-enable connections on wt databases a crashed template clone left closed."""
    for name in closed_databases(databases):
        psql(f"ALTER DATABASE {ident(name)} WITH ALLOW_CONNECTIONS true")


def ensure_server(databases: list[str], timeout: int = 60) -> None:
    server.start(container(), server_dir(), compose_doc(), timeout, wait_ready)
    repair_connections(databases)


class PostgresProvider:
    name = "postgres"

    def __init__(self, cfg: Config, log: Callable[[str], None] = lambda message: None) -> None:
        # No I/O here: providers are built on every render. `cfg` may lack shared.postgres
        # when `down` runs after the section was removed.
        self.pg: PostgresConfig | None = cfg.shared.postgres
        self.log = log
        self._cloned: list[tuple[str, str]] = []  # (source, clone) made in this run
        self._warnings: list[str] = []

    # ---- helpers ------------------------------------------------------------------------

    @property
    def databases(self) -> list[str]:
        return self.pg.databases if self.pg is not None else []

    def _names(self, ctx: BranchContext) -> list[tuple[str, str]]:
        """(base database, this branch's database) per configured database."""
        return [(self._db(d, ctx, base=True), self._db(d, ctx)) for d in self.databases]

    def _db(self, database: str, ctx: BranchContext, *, base: bool = False) -> str:
        names = ctx.names
        if base:
            names = Names(names.project, ctx.base_branch, names.domain)
        return db_name(database, names)

    def _exists(self, db: str) -> bool:
        return bool(_rows(psql(f"SELECT 1 FROM pg_database WHERE datname = {literal(db)}")))

    def _drop(self, db: str) -> None:
        psql(f"DROP DATABASE IF EXISTS {ident(db)} WITH (FORCE)")

    def _tables(self, db: str) -> list[str]:
        sql = (
            "SELECT table_schema || '.' || table_name FROM information_schema.tables "
            f"WHERE table_type = 'BASE TABLE' AND table_schema NOT IN {SYSTEM_SCHEMAS} ORDER BY 1"
        )
        return _rows(psql(sql, db))

    def _counts(self, db: str) -> dict[str, int]:
        sql = (
            "SELECT table_schema || '.' || table_name, "
            "(xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', "
            "table_schema, table_name), false, true, '')))[1]::text "
            "FROM information_schema.tables "
            f"WHERE table_type = 'BASE TABLE' AND table_schema NOT IN {SYSTEM_SCHEMAS} ORDER BY 1"
        )
        counts: dict[str, int] = {}
        for row in _rows(psql(sql, db)):
            table, _, count = row.rpartition("|")
            counts[table] = int(count)
        return counts

    # ---- Provider -----------------------------------------------------------------------

    def ensure_server(self) -> None:
        ensure_server(self.databases)

    def preflight(self, ctx: BranchContext) -> None:
        self._guard_base_alias(ctx)

    def create(self, ctx: BranchContext) -> Resources:
        self._cloned = []
        self._warnings = []
        self._guard_base_alias(ctx)
        self._record_base(ctx)
        created: list[str] = []
        for src, dst in self._names(ctx):
            created.append(dst)
            if self._exists(dst):
                continue
            if ctx.branch == ctx.base_branch or not self._exists(src):
                if ctx.branch != ctx.base_branch:
                    self._warnings.append(
                        f"base database {src} does not exist; {dst} was created empty. "
                        f"run wt up {ctx.base_branch} first to clone real data"
                    )
                psql(f"CREATE DATABASE {ident(dst)}")
                continue
            self._clone(src, dst)
            self._cloned.append((src, dst))
        return Resources(created)

    def _guard_base_alias(self, ctx: BranchContext) -> None:
        """Refuse a non-base branch whose database names equal the base branch's."""
        if ctx.branch != ctx.base_branch and any(src == dst for src, dst in self._names(ctx)):
            raise WtError(
                f"branch {ctx.branch!r} maps to the same databases as the base branch "
                f"{ctx.base_branch!r}; rename the branch"
            )

    def _record_base(self, ctx: BranchContext) -> None:
        """Remember which branch was the base when this slice was made (`destroy` relies on it)."""
        with ctx.state.edit() as state:
            bs = state.branches.get(ctx.branch)
            if bs is not None:
                bs.provider_base[self.name] = ctx.base_branch

    def _clone(self, src: str, dst: str) -> None:
        strategy = self.pg.strategy if self.pg is not None else "template"
        if strategy == "template":
            self._clone_template(src, dst)
        else:
            self._clone_dump(src, dst)

    def _clone_template(self, src: str, dst: str) -> None:
        psql(f"ALTER DATABASE {ident(src)} WITH ALLOW_CONNECTIONS false")
        try:
            self._template_steps(src, dst)
        except BaseException:
            self._reopen(src, strict=False)  # never hide the clone error
            raise
        self._reopen(src, strict=True)

    def _reopen(self, src: str, *, strict: bool) -> None:
        try:
            psql(f"ALTER DATABASE {ident(src)} WITH ALLOW_CONNECTIONS true")
        except WtError as exc:
            if strict:
                raise
            self.log(f"warning: could not reopen {src} (run wt doctor --fix): {exc}")

    def _template_steps(self, src: str, dst: str) -> None:
        psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = {literal(src)} AND pid <> pg_backend_pid()"
        )
        for attempt in range(1, CLONE_ATTEMPTS + 1):
            result = psql(f"CREATE DATABASE {ident(dst)} TEMPLATE {ident(src)}", check=False)
            if result.ok:
                return
            if BUSY not in result.stderr or attempt == CLONE_ATTEMPTS:
                raise proc.CommandError(["psql", "CREATE DATABASE"], result)
            time.sleep(CLONE_WAIT)

    def _clone_dump(self, src: str, dst: str) -> None:
        ident(src)
        ident(dst)
        tmp = temp_name(dst)
        self._drop(tmp)  # a leftover from a run that was killed hard
        psql(f"CREATE DATABASE {ident(tmp)}")
        script = (
            f'set -o pipefail; pg_dump -U {USER} -Fc "$1" | pg_restore -U {USER} --no-owner -d "$2"'
        )
        try:
            proc.run(["docker", "exec", container(), "sh", "-c", script, "sh", src, tmp])
            psql(f"ALTER DATABASE {ident(tmp)} RENAME TO {ident(dst)}")
        except BaseException:  # Ctrl-C and SIGTERM too: never leave a half-restored database
            self._drop(tmp)
            raise

    def verify(self, ctx: BranchContext) -> Check:
        warnings = list(self._warnings)
        if self.pg is None or not self.pg.verify:
            return Check(True, "verification disabled", warnings)
        strategy = self.pg.strategy
        for src, dst in self._cloned:
            if self._tables(src) != self._tables(dst):
                self._drop(dst)
                return Check(False, f"table list of {dst} differs from {src}", warnings)
            before, after = self._counts(src), self._counts(dst)
            if before == after:
                continue
            diff = sorted(t for t in before if before[t] != after.get(t))
            detail = f"row counts of {dst} differ from {src} in: {', '.join(diff)}"
            if strategy == "template":
                self._drop(dst)
                return Check(False, detail, warnings)
            warnings.append(f"{detail} (the source is live)")
        return Check(True, f"{len(self._cloned)} database(s) cloned and verified", warnings)

    def destroy(self, ctx: BranchContext) -> None:
        recorded: list[str] = []
        base: str | None = ctx.base_branch if self.pg is not None else None
        bs = ctx.state.load().branches.get(ctx.branch)
        if bs is not None:
            recorded = bs.providers.get(self.name, [])
            base = bs.provider_base.get(self.name, base)  # state wins over today's config
        wanted = list(dict.fromkeys([*recorded, *(dst for _, dst in self._names(ctx))]))
        if base is None or ctx.branch == base:
            self.log(f"keeping the base branch databases: {', '.join(wanted) or 'none'}")
            return
        protected = {
            db_name(d, Names(ctx.names.project, base, ctx.names.domain)) for d in self.databases
        }
        for db in wanted:
            if db in protected:
                self.log(f"keeping {db}: it belongs to the base branch {base}")
                continue
            self._drop(db)

    def template_vars(self, ctx: BranchContext, kind: Kind) -> dict[str, Any]:
        known = {d: self._db(d, ctx) for d in self.databases}
        host = f"{container()}:5432" if kind == "compose" else f"127.0.0.1:{host_port()}"

        def lookup(database: str) -> str:
            if database not in known:
                raise WtError(
                    f"postgres.{database!r}: unknown database; configured: "
                    f"{', '.join(sorted(known)) or 'none'}"
                )
            return known[database]

        def url(database: str) -> str:
            return f"postgresql://{USER}:{password()}@{host}/{lookup(database)}"

        return {"postgres": {"url": url, "name": lookup}}
