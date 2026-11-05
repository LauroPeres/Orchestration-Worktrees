"""Keep wt's Postgres connections in DBeaver's `data-sources.json` (SPEC §27).

wt only owns the connections it writes (id prefix + `wt.managed` property); every other key of
the file is preserved.
"""

import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wt.errors import WtError
from wt.state import atomic_write

MANAGED_PREFIX = "wt-pg-"
FOLDER = "wt"

MANAGED_PROPERTY = "wt.managed"
# Id of DBeaver's "PostgreSQL PgPass" authentication model (the postgresql plugin's plugin.xml).
PGPASS_AUTH_MODEL = "postgres_pgpass"
SHOW_ALL_DATABASES_PROPERTY = "@dbeaver-show-non-default-db@"
DEFAULT_INDENT = "\t"  # what DBeaver itself writes
WORKSPACE_RELATIVE = Path("DBeaverData/workspace6/General/.dbeaver/data-sources.json")

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class DbConnection:
    id: str
    name: str
    host: str
    port: int
    database: str
    user: str
    show_all_databases: bool = False


@dataclass(frozen=True)
class DbeaverChange:
    file: Path
    added: list[str]
    updated: list[str]
    removed: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


def default_data_sources_path() -> Path | None:
    if sys.platform == "darwin":
        return Path.home() / "Library" / WORKSPACE_RELATIVE
    if sys.platform.startswith("linux"):
        data_home = os.environ.get("XDG_DATA_HOME")
        base = Path(data_home) if data_home else Path.home() / ".local" / "share"
        return base / WORKSPACE_RELATIVE
    return None


def _written_fields(conn: DbConnection) -> JsonObject:
    """The keys wt owns in a connection; any other key DBeaver adds is left alone."""
    show_all = "true" if conn.show_all_databases else "false"
    return {
        "provider": "postgresql",
        "driver": "postgres-jdbc",
        "name": conn.name,
        "save-password": False,
        "folder": FOLDER,
        "custom-properties": {MANAGED_PROPERTY: "true"},
        "configuration": {
            "host": conn.host,
            "port": str(conn.port),
            "database": conn.database,
            "url": f"jdbc:postgresql://{conn.host}:{conn.port}/{conn.database}",
            "configurationType": "MANUAL",
            "user": conn.user,
            "auth-model": PGPASS_AUTH_MODEL,
            "provider-properties": {SHOW_ALL_DATABASES_PROPERTY: show_all},
        },
    }


def _merge(existing: JsonObject, written: JsonObject) -> JsonObject:
    """`existing` with `written` laid over it, one level into the nested objects wt writes."""
    merged = dict(existing)
    for key, value in written.items():
        current = existing.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _is_managed(connection_id: str, entry: object) -> bool:
    if not connection_id.startswith(MANAGED_PREFIX) or not isinstance(entry, dict):
        return False
    properties = entry.get("custom-properties")
    return isinstance(properties, dict) and properties.get(MANAGED_PROPERTY) == "true"


def _name_of(connection_id: str, entry: object) -> str:
    name = entry.get("name") if isinstance(entry, dict) else None
    return name if isinstance(name, str) else connection_id


def _detect_indent(text: str) -> str:
    match = re.search(r"^([ \t]+)\S", text, re.MULTILINE)
    return match.group(1) if match else DEFAULT_INDENT


def _load(path: Path) -> tuple[str, JsonObject]:
    if not path.is_file():
        raise WtError(
            f"DBeaver workspace not found at {path}; start DBeaver once, or set dbeaver: <path>"
        )
    try:
        text = path.read_text()
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        raise WtError(f"Cannot use {path}: not readable JSON ({exc.__class__.__name__})") from exc
    if not isinstance(data, dict):
        raise WtError(f"Cannot use {path}: expected a JSON object at the top level")
    for key in ("connections", "folders"):
        if not isinstance(data.get(key, {}), dict):
            raise WtError(f"Cannot use {path}: '{key}' must be a JSON object")
    return text, data


def _sync_connections(
    connections: JsonObject, desired: Sequence[DbConnection], path: Path
) -> tuple[list[str], list[str], list[str]]:
    added: list[str] = []
    updated: list[str] = []
    wanted = {conn.id for conn in desired}
    stale = [
        key for key, entry in connections.items() if _is_managed(key, entry) and key not in wanted
    ]
    removed = [_name_of(key, connections.pop(key)) for key in stale]
    for conn in desired:
        existing = connections.get(conn.id)
        if existing is None:
            connections[conn.id] = _written_fields(conn)
            added.append(conn.name)
        elif not _is_managed(conn.id, existing):
            raise WtError(f"{path} already has a connection '{conn.id}' that wt did not create")
        else:
            merged = _merge(existing, _written_fields(conn))
            if merged != existing:
                connections[conn.id] = merged
                updated.append(conn.name)
    return added, updated, removed


def apply(path: Path, desired: Sequence[DbConnection]) -> DbeaverChange:
    """Make the wt-managed connections of `path` equal `desired`; write only on a difference."""
    text, data = _load(path)
    before = json.dumps(data)
    connections: JsonObject = data.get("connections", {})
    added, updated, removed = _sync_connections(connections, desired, path)
    if connections:
        data["connections"] = connections
    if desired:
        data.setdefault("folders", {}).setdefault(FOLDER, {})
    if json.dumps(data) != before:
        rendered = json.dumps(data, indent=_detect_indent(text), ensure_ascii=False)
        atomic_write(path, rendered + ("\n" if text.endswith("\n") else ""))
    return DbeaverChange(path, added, updated, removed)
