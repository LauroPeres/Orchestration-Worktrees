import multiprocessing
import os
from pathlib import Path

import pytest

from wt.errors import WtError
from wt.state import BranchState, StateStore, atomic_write


def test_edit_roundtrip(isolated_home: Path) -> None:
    store = StateStore("shop")
    assert store.load().branches == {}
    with store.edit() as state:
        state.branches["feat/x"] = BranchState(branch="feat/x", slug="feat-x")
    assert store.path == isolated_home / "shop" / "state.json"
    loaded = store.load()
    assert loaded.branches["feat/x"].status == "creating"
    assert [p.name for p in store.path.parent.iterdir()] == ["state.json"]


def test_failed_edit_does_not_save(isolated_home: Path) -> None:
    store = StateStore("shop")
    try:
        with store.edit() as state:
            state.branches["x"] = BranchState(branch="x", slug="x")
            raise RuntimeError
    except RuntimeError:
        pass
    assert store.load().branches == {}


def _write_raw(store: StateStore, text: str) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        '{"project": "shop", "branches": {',
        "[]",
        "",
        '{"project": "shop", "version": "2"}',
    ],
)
def test_corrupt_state_raises_wterror(isolated_home: Path, text: str) -> None:
    store = StateStore("shop")
    _write_raw(store, text)
    with pytest.raises(WtError) as info:
        store.load()
    assert str(store.path) in str(info.value)
    assert "delete" in str(info.value)


def test_invalid_schema_raises_wterror(isolated_home: Path) -> None:
    store = StateStore("shop")
    _write_raw(store, '{"project": "shop", "branches": {"x": {"slug": 1}}}')
    with pytest.raises(WtError, match="corrupt"):
        store.load()


def test_newer_version_refused(isolated_home: Path) -> None:
    store = StateStore("shop")
    _write_raw(store, '{"version": 2, "project": "shop", "future_field": true}')
    with pytest.raises(WtError, match="newer wt"):
        store.load()


def test_version_is_saved(isolated_home: Path) -> None:
    store = StateStore("shop")
    with store.edit():
        pass
    assert '"version": 1' in store.path.read_text()
    assert store.load().version == 1


def test_atomic_write_failure_keeps_target(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = isolated_home / "f.txt"
    atomic_write(target, "old")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in target.parent.iterdir()] == ["f.txt"]


def test_atomic_write_leaves_no_tmp(isolated_home: Path) -> None:
    target = isolated_home / "f.txt"
    atomic_write(target, "data")
    assert [p.name for p in target.parent.iterdir()] == ["f.txt"]


def _bump(home: str, count: int) -> None:
    os.environ["WT_HOME"] = home
    store = StateStore("shop")
    for _ in range(count):
        with store.edit() as state:
            state.branches.setdefault("x", BranchState(branch="x", slug="x"))
            state.branches["x"].cert_files.append("c")


def test_concurrent_edits_are_not_lost(isolated_home: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_bump, args=(str(isolated_home), 25)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    hung = [p for p in procs if p.is_alive()]
    for p in hung:
        p.terminate()
    assert not hung, "edit() deadlocked"
    assert all(p.exitcode == 0 for p in procs)
    assert len(StateStore("shop").load().branches["x"].cert_files) == 100


def test_v01_state_file_still_loads(isolated_home: Path) -> None:
    store = StateStore("shop")
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        '{"version": 1, "project": "shop", "branches": {"feat/x": '
        '{"branch": "feat/x", "slug": "feat-x", "status": "up", "repos": {}, '
        '"compose_projects": {}, "route_file": null, "cert_files": []}}}'
    )
    bs = store.load().branches["feat/x"]
    assert bs.providers == {}


def test_providers_roundtrip(isolated_home: Path) -> None:
    store = StateStore("shop")
    with store.edit() as state:
        state.branches["b"] = BranchState(branch="b", slug="b", providers={"postgres": ["shop__b"]})
    assert store.load().branches["b"].providers == {"postgres": ["shop__b"]}
