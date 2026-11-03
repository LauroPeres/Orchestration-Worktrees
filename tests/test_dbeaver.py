import json
import sys
from pathlib import Path
from typing import Any

import pytest

from wt import dbeaver
from wt.dbeaver import DbConnection, DbeaverChange
from wt.errors import WtError

EMPTY: dict[str, Any] = {"folders": {}, "connections": {}}


def conn(
    name: str = "music-back-testt1",
    *,
    database: str = "music__testt1",
    port: int = 5432,
    show_all: bool = False,
) -> DbConnection:
    return DbConnection(
        id=f"{dbeaver.MANAGED_PREFIX}{name}",
        name=name,
        host="127.0.0.1",
        port=port,
        database=database,
        user="wt",
        show_all_databases=show_all,
    )


def workspace(tmp_path: Path, content: dict[str, Any] | None = None, indent: str = "\t") -> Path:
    path = tmp_path / "General" / ".dbeaver" / "data-sources.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(EMPTY if content is None else content, indent=indent))
    return path


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_apply_adds_a_connection_in_the_wt_folder(tmp_path: Path) -> None:
    path = workspace(tmp_path)
    change = dbeaver.apply(path, [conn()])
    assert change == DbeaverChange(path, ["music-back-testt1"], [], [])
    assert change.changed
    data = load(path)
    assert data["folders"] == {"wt": {}}
    assert data["connections"]["wt-pg-music-back-testt1"] == {
        "provider": "postgresql",
        "driver": "postgres-jdbc",
        "name": "music-back-testt1",
        "save-password": False,
        "folder": "wt",
        "custom-properties": {"wt.managed": "true"},
        "configuration": {
            "host": "127.0.0.1",
            "port": "5432",
            "database": "music__testt1",
            "url": "jdbc:postgresql://127.0.0.1:5432/music__testt1",
            "configurationType": "MANUAL",
            "user": "wt",
            "auth-model": "postgres_pgpass",
            "provider-properties": {"@dbeaver-show-non-default-db@": "false"},
        },
    }


def test_show_all_databases_is_written_as_a_string_flag(tmp_path: Path) -> None:
    path = workspace(tmp_path)
    dbeaver.apply(path, [conn("music", database="postgres", show_all=True)])
    props = load(path)["connections"]["wt-pg-music"]["configuration"]["provider-properties"]
    assert props["@dbeaver-show-non-default-db@"] == "true"


def test_second_apply_changes_nothing_and_leaves_bytes_identical(tmp_path: Path) -> None:
    path = workspace(tmp_path)
    dbeaver.apply(path, [conn()])
    before = path.read_bytes()
    change = dbeaver.apply(path, [conn()])
    assert not change.changed
    assert path.read_bytes() == before


def test_apply_updates_a_changed_connection_and_keeps_unknown_keys(tmp_path: Path) -> None:
    path = workspace(tmp_path)
    dbeaver.apply(path, [conn()])
    data = load(path)
    entry = data["connections"]["wt-pg-music-back-testt1"]
    entry["navigator-settings"] = {"show-system-objects": True}
    entry["configuration"]["type"] = "dev"
    path.write_text(json.dumps(data, indent="\t"))

    change = dbeaver.apply(path, [conn(port=5433)])

    assert change == DbeaverChange(path, [], ["music-back-testt1"], [])
    updated = load(path)["connections"]["wt-pg-music-back-testt1"]
    assert updated["configuration"]["port"] == "5433"
    assert updated["configuration"]["url"] == "jdbc:postgresql://127.0.0.1:5433/music__testt1"
    assert updated["navigator-settings"] == {"show-system-objects": True}
    assert updated["configuration"]["type"] == "dev"


def test_apply_removes_stale_managed_connections(tmp_path: Path) -> None:
    path = workspace(tmp_path)
    dbeaver.apply(path, [conn("a"), conn("b")])
    change = dbeaver.apply(path, [conn("a")])
    assert change == DbeaverChange(path, [], [], ["b"])
    assert list(load(path)["connections"]) == ["wt-pg-a"]


def test_apply_with_nothing_desired_removes_all_managed_but_keeps_the_folder(
    tmp_path: Path,
) -> None:
    path = workspace(tmp_path)
    dbeaver.apply(path, [conn("a")])
    dbeaver.apply(path, [])
    data = load(path)
    assert data["connections"] == {}
    assert "wt" in data["folders"]


def test_apply_with_nothing_to_do_does_not_add_the_folder_or_write(tmp_path: Path) -> None:
    path = workspace(tmp_path)
    before = path.read_bytes()
    assert not dbeaver.apply(path, []).changed
    assert path.read_bytes() == before


def test_foreign_connections_folders_and_keys_are_untouched(tmp_path: Path) -> None:
    foreign = {
        "provider": "postgresql",
        "name": "postgres",
        "save-password": True,
        "configuration": {"host": "db", "auth-model": "native"},
    }
    lookalike = {"name": "mine", "custom-properties": {"wt.managed": "true"}}
    content = {
        "folders": {"work": {"description": "x"}},
        "connections": {"postgres-jdbc-1": foreign, "mine": lookalike},
        "connection-types": {"dev": {"name": "Dev"}},
        "virtual-models": {"k": 1},
    }
    path = workspace(tmp_path, content)
    dbeaver.apply(path, [conn()])
    data = load(path)
    assert data["connections"]["postgres-jdbc-1"] == foreign
    assert data["connections"]["mine"] == lookalike  # no wt- id prefix: not managed
    assert data["folders"] == {"work": {"description": "x"}, "wt": {}}
    assert data["connection-types"] == content["connection-types"]
    assert data["virtual-models"] == content["virtual-models"]
    dbeaver.apply(path, [])
    assert set(load(path)["connections"]) == {"postgres-jdbc-1", "mine"}


def test_a_wt_prefixed_id_without_the_managed_property_is_not_removed(tmp_path: Path) -> None:
    content = {"folders": {}, "connections": {"wt-pg-hand-made": {"name": "hand"}}}
    path = workspace(tmp_path, content)
    assert not dbeaver.apply(path, []).changed
    assert "wt-pg-hand-made" in load(path)["connections"]


def test_a_desired_id_that_collides_with_a_foreign_connection_is_an_error(tmp_path: Path) -> None:
    content = {"folders": {}, "connections": {"wt-pg-a": {"name": "hand"}}}
    path = workspace(tmp_path, content)
    before = path.read_bytes()
    with pytest.raises(WtError, match="wt-pg-a"):
        dbeaver.apply(path, [conn("a")])
    assert path.read_bytes() == before


@pytest.mark.parametrize("indent", ["\t", "  ", "    "])
def test_the_existing_indentation_is_kept(tmp_path: Path, indent: str) -> None:
    path = workspace(tmp_path, indent=indent)
    dbeaver.apply(path, [conn()])
    assert f'\n{indent}"connections": {{' in path.read_text()


def test_trailing_newline_presence_is_preserved(tmp_path: Path) -> None:
    path = workspace(tmp_path)
    dbeaver.apply(path, [conn()])
    assert not path.read_text().endswith("\n")
    path.write_text(path.read_text() + "\n")
    dbeaver.apply(path, [conn("other")])
    assert path.read_text().endswith("}\n")


def test_missing_file_is_an_error_naming_the_path(tmp_path: Path) -> None:
    path = tmp_path / "nope" / "data-sources.json"
    with pytest.raises(WtError) as raised:
        dbeaver.apply(path, [conn()])
    assert str(raised.value) == (
        f"DBeaver workspace not found at {path}; start DBeaver once, or set dbeaver: <path>"
    )
    assert not path.parent.exists()


def test_corrupt_json_is_an_error_and_the_file_is_not_overwritten(tmp_path: Path) -> None:
    path = workspace(tmp_path)
    path.write_text('{"connections": {')
    with pytest.raises(WtError, match=r"data-sources\.json"):
        dbeaver.apply(path, [conn()])
    assert path.read_text() == '{"connections": {'


@pytest.mark.parametrize("text", ["[]", '{"connections": []}', '{"folders": 3}'])
def test_unexpected_shapes_are_an_error(tmp_path: Path, text: str) -> None:
    path = workspace(tmp_path)
    path.write_text(text)
    with pytest.raises(WtError, match=r"data-sources\.json"):
        dbeaver.apply(path, [conn()])
    assert path.read_text() == text


def test_a_file_without_connections_or_folders_keys_gets_them(tmp_path: Path) -> None:
    path = workspace(tmp_path, {"connection-types": {}})
    dbeaver.apply(path, [conn()])
    data = load(path)
    assert "wt-pg-music-back-testt1" in data["connections"]
    assert data["folders"] == {"wt": {}}


def test_default_path_on_linux_honors_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert dbeaver.default_data_sources_path() == (
        tmp_path
        / "xdg"
        / "DBeaverData"
        / "workspace6"
        / "General"
        / ".dbeaver"
        / "data-sources.json"
    )


def test_default_path_on_linux_falls_back_to_local_share(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dbeaver.default_data_sources_path() == (
        tmp_path / ".local/share/DBeaverData/workspace6/General/.dbeaver/data-sources.json"
    )


def test_default_path_on_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dbeaver.default_data_sources_path() == (
        tmp_path / "Library/DBeaverData/workspace6/General/.dbeaver/data-sources.json"
    )


def test_default_path_is_none_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert dbeaver.default_data_sources_path() is None


def test_a_file_without_connections_key_is_not_rewritten_when_nothing_is_desired(
    tmp_path: Path,
) -> None:
    path = workspace(tmp_path, {"connection-types": {}})
    before = path.read_bytes()
    assert not dbeaver.apply(path, []).changed
    assert path.read_bytes() == before
