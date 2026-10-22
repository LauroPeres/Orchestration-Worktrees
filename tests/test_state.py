from pathlib import Path

from wt.state import BranchState, StateStore


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
