import stat
from pathlib import Path

import pytest

from wt import pgpass
from wt.errors import WtError

COMMENT = "# wt: shared Postgres (managed by wt)"


def ensure(path: Path, *, port: int = 5432, password: str = "secret") -> bool:
    return pgpass.ensure(host="127.0.0.1", port=port, user="wt", password=password, path=path)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_default_path_is_home_pgpass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PGPASSFILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert pgpass.default_path() == tmp_path / ".pgpass"


def test_default_path_honors_pgpassfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "custom"))
    assert pgpass.default_path() == tmp_path / "custom"


def test_default_path_ignores_an_empty_pgpassfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PGPASSFILE", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert pgpass.default_path() == tmp_path / ".pgpass"


def test_ensure_creates_the_file_with_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / ".pgpass"
    assert ensure(path) is True
    assert path.read_text() == f"{COMMENT}\n127.0.0.1:5432:*:wt:secret\n"
    assert mode(path) == 0o600


def test_ensure_uses_pgpassfile_when_no_path_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "from-env"
    monkeypatch.setenv("PGPASSFILE", str(target))
    assert pgpass.ensure(host="127.0.0.1", port=5432, user="wt", password="x") is True
    assert target.read_text().endswith("127.0.0.1:5432:*:wt:x\n")


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / ".pgpass"
    ensure(path)
    before = path.read_bytes()
    assert ensure(path) is False
    assert path.read_bytes() == before


def test_ensure_updates_the_managed_entry_in_place(tmp_path: Path) -> None:
    path = tmp_path / ".pgpass"
    path.write_text(f"db1:5432:*:me:one\n{COMMENT}\n127.0.0.1:5432:*:wt:old\ndb2:5432:*:me:two\n")
    assert ensure(path, port=5440, password="new") is True
    assert path.read_text() == (
        f"db1:5432:*:me:one\n{COMMENT}\n127.0.0.1:5440:*:wt:new\ndb2:5432:*:me:two\n"
    )


def test_ensure_keeps_other_lines_and_appends_when_missing(tmp_path: Path) -> None:
    path = tmp_path / ".pgpass"
    path.write_text("# mine\ndb1:5432:*:me:one")  # no trailing newline
    assert ensure(path) is True
    assert path.read_text() == f"# mine\ndb1:5432:*:me:one\n{COMMENT}\n127.0.0.1:5432:*:wt:secret\n"


def test_ensure_fixes_the_mode_of_a_touched_file(tmp_path: Path) -> None:
    path = tmp_path / ".pgpass"
    path.write_text("db1:5432:*:me:one\n")
    path.chmod(0o644)
    ensure(path)
    assert mode(path) == 0o600


def test_ensure_escapes_colon_and_backslash_in_the_password(tmp_path: Path) -> None:
    path = tmp_path / ".pgpass"
    ensure(path, password=r"a:b\c")
    assert path.read_text().splitlines()[1] == r"127.0.0.1:5432:*:wt:a\:b\\c"


def test_ensure_rejects_a_newline_without_leaking_the_password(tmp_path: Path) -> None:
    path = tmp_path / ".pgpass"
    with pytest.raises(WtError) as raised:
        ensure(path, password="top\nsecret")
    assert "top" not in str(raised.value)
    assert not path.exists()
