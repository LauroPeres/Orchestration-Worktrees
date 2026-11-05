"""The one `~/.pgpass` entry wt manages for the shared Postgres server (SPEC §27)."""

import os
from pathlib import Path

from wt.errors import WtError
from wt.state import atomic_write

MANAGED_COMMENT = "# wt: shared Postgres (managed by wt)"
PGPASSFILE_ENV = "PGPASSFILE"
DEFAULT_FILE_NAME = ".pgpass"


def default_path() -> Path:
    custom = os.environ.get(PGPASSFILE_ENV)
    return Path(custom) if custom else Path.home() / DEFAULT_FILE_NAME


def _escape(field: str) -> str:
    """pgpass separates fields with ':' and escapes ':' and '\\' with a backslash."""
    return field.replace("\\", "\\\\").replace(":", "\\:")


def _entry(host: str, port: int, user: str, password: str) -> str:
    if "\n" in password or "\r" in password:
        raise WtError(
            "The shared Postgres password cannot contain a line break (pgpass is line-based)."
        )
    return ":".join([_escape(host), str(port), "*", _escape(user), _escape(password)])


def _with_managed_entry(lines: list[str], entry: str) -> list[str]:
    """`lines` with the managed comment+entry pair updated in place, or appended when absent."""
    result = list(lines)
    for index, line in enumerate(result):
        if line.rstrip() != MANAGED_COMMENT:
            continue
        following = result[index + 1].strip() if index + 1 < len(result) else ""
        if following and not following.startswith("#"):
            result[index + 1] = entry
        else:
            result.insert(index + 1, entry)
        return result
    return [*result, MANAGED_COMMENT, entry]


def ensure(*, host: str, port: int, user: str, password: str, path: Path | None = None) -> bool:
    """Keep exactly one managed entry in the pgpass file; True when the file changed.

    The file is written with mode 0600 (postgres clients ignore a pgpass that others can read).
    """
    target = path if path is not None else default_path()
    entry = _entry(host, port, user, password)
    try:
        lines = target.read_text().splitlines() if target.exists() else []
    except OSError as exc:
        raise WtError(f"Cannot read {target}: {exc.strerror or exc.__class__.__name__}") from exc
    updated = _with_managed_entry(lines, entry)
    if updated == lines:
        return False
    try:
        atomic_write(target, "\n".join(updated) + "\n")  # mkstemp creates it 0600
    except OSError as exc:
        raise WtError(f"Cannot write {target}: {exc.strerror or exc.__class__.__name__}") from exc
    return True
