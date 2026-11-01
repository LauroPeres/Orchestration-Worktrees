import json
import os
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wt import cache, proc
from wt.errors import WtError
from wt.paths import cache_dir, installs_dir, tool_cache_dir

LOCKS = {"package-lock.json": b'{"lockfileVersion": 3}', ".nvmrc": b"20\n"}


def make_install(repo: Path, outside: Path | None = None) -> Path:
    """A tiny node_modules: a file, a nested file, a relative symlink and (optionally) an escape."""
    modules = repo / "node_modules"
    (modules / "left-pad").mkdir(parents=True, exist_ok=True)
    (modules / "left-pad" / "index.js").write_text("module.exports = 1\n")
    (modules / "top.txt").write_text("top")
    os.symlink("left-pad/index.js", modules / "link.js")
    if outside is not None:
        os.symlink(outside, modules / "escape")
    return modules


def tree(root: Path) -> dict[str, str]:
    """Relative path -> 'file:<text>' / 'link:<target>' / 'dir'; never follows symlinks."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            result[rel] = f"link:{os.readlink(path)}"
        elif path.is_dir():
            result[rel] = "dir"
        else:
            result[rel] = f"file:{path.read_text()}"
    return result


def stored(tmp_path: Path, key: str = "k1", service: str = "web") -> Path:
    repo = tmp_path / "source"
    if not repo.exists():
        repo.mkdir()
        make_install(repo)
    assert cache.store(key, "npm", service, "npm ci", repo)
    return repo


def write_meta(key: str, **meta: object) -> None:
    (installs_dir() / key / "meta.json").write_text(json.dumps(meta))


def iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


# --- keys and presets --------------------------------------------------------------------------


def key_of(
    preset: cache.Preset = "npm",
    locks: dict[str, bytes] | None = None,
    setup: str = "npm ci",
    version: str = "v20.1.0",
) -> str:
    return cache.install_key(preset, LOCKS if locks is None else locks, setup, version)


def test_install_key_is_a_stable_sha256_hex() -> None:
    key = key_of()
    assert key == key_of()
    assert len(key) == 64
    int(key, 16)


def test_install_key_ignores_lock_file_order() -> None:
    reversed_locks = dict(reversed(list(LOCKS.items())))
    assert key_of(locks=reversed_locks) == key_of()


def test_install_key_changes_with_the_preset() -> None:
    assert key_of(preset="uv") != key_of()


def test_install_key_changes_with_a_lock_file_content() -> None:
    assert key_of(locks={**LOCKS, "package-lock.json": b"changed"}) != key_of()


def test_install_key_changes_with_the_set_of_lock_files() -> None:
    assert key_of(locks={"package-lock.json": LOCKS["package-lock.json"]}) != key_of()


def test_install_key_changes_with_the_setup_text() -> None:
    assert key_of(setup="npm install") != key_of()


def test_install_key_changes_with_the_tool_version() -> None:
    assert key_of(version="v22.0.0") != key_of()


def test_install_key_does_not_confuse_where_one_field_ends() -> None:
    first = cache.install_key("npm", {}, "ab", "c")
    second = cache.install_key("npm", {}, "a", "bc")
    assert first != second


def test_lock_file_names() -> None:
    assert cache.lock_file_names("npm") == ("package-lock.json", ".nvmrc", ".node-version")
    assert cache.lock_file_names("uv") == ("uv.lock", ".python-version", "pyproject.toml")


def test_read_lock_files_npm_reads_the_ones_present(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_bytes(b"{}")
    (tmp_path / ".node-version").write_bytes(b"20")
    (tmp_path / "package.json").write_bytes(b"ignored")
    assert cache.read_lock_files(tmp_path, "npm") == {
        "package-lock.json": b"{}",
        ".node-version": b"20",
    }


def test_read_lock_files_npm_requires_the_lock(tmp_path: Path) -> None:
    (tmp_path / ".nvmrc").write_bytes(b"20")
    with pytest.raises(WtError) as raised:
        cache.read_lock_files(tmp_path, "npm")
    assert str(raised.value) == f"setup_cache: npm needs package-lock.json in {tmp_path}"


def test_read_lock_files_uv_everything_is_optional(tmp_path: Path) -> None:
    assert cache.read_lock_files(tmp_path, "uv") == {}
    (tmp_path / "uv.lock").write_bytes(b"lock")
    assert cache.read_lock_files(tmp_path, "uv") == {"uv.lock": b"lock"}


def test_restorable_and_cached_path() -> None:
    assert cache.restorable("npm") is True
    assert cache.restorable("uv") is False
    assert cache.cached_path("npm") == "node_modules"
    with pytest.raises(WtError, match="uv"):
        cache.cached_path("uv")


def test_tool_env_points_at_the_tool_cache_and_creates_nothing() -> None:
    assert cache.tool_env("uv") == {"UV_CACHE_DIR": str(tool_cache_dir("uv"))}
    assert cache.tool_env("npm") == {"npm_config_cache": str(tool_cache_dir("npm"))}
    assert not cache_dir().exists()


# --- store, lookup, restore --------------------------------------------------------------------


def test_lookup_misses_when_nothing_is_stored() -> None:
    assert cache.lookup("nope") is None


def test_store_then_lookup_records_the_metadata(tmp_path: Path) -> None:
    stored(tmp_path, key="abc", service="web")
    entry = cache.lookup("abc")
    assert entry is not None
    assert (entry.key, entry.preset, entry.service) == ("abc", "npm", "web")
    assert entry.bytes > 0
    meta = json.loads((installs_dir() / "abc" / "meta.json").read_text())
    assert set(meta) == {"preset", "service", "setup", "created_at", "last_used", "bytes"}
    assert meta["setup"] == "npm ci"
    assert meta["created_at"] == meta["last_used"] == entry.last_used
    assert [p.name for p in installs_dir().iterdir()] == ["abc"]


def test_restore_copies_the_tree_and_keeps_symlinks(tmp_path: Path) -> None:
    source = stored(tmp_path)
    target = tmp_path / "worktree"
    target.mkdir()
    assert cache.restore("k1", "npm", target) is True
    assert tree(target / "node_modules") == tree(source / "node_modules")
    assert (target / "node_modules" / "link.js").is_symlink()
    assert sorted(p.name for p in target.iterdir()) == ["node_modules"]


def test_symlinks_pointing_outside_are_copied_as_links_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    repo = tmp_path / "source"
    repo.mkdir()
    make_install(repo, outside=outside)
    assert cache.store("k1", "npm", "web", "npm ci", repo)
    stored_escape = installs_dir() / "k1" / "node_modules" / "escape"
    assert stored_escape.is_symlink()

    target = tmp_path / "worktree"
    target.mkdir()
    assert cache.restore("k1", "npm", target) is True
    restored = target / "node_modules" / "escape"
    assert restored.is_symlink()
    assert os.readlink(restored) == str(outside)
    assert cache.lookup("k1") is not None
    assert (outside / "secret.txt").read_text() == "secret"


def test_restore_without_cp_falls_back_to_a_python_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = stored(tmp_path)

    def no_cp(args: Sequence[str], **_: object) -> proc.Result:
        return proc.Result(1, "", "cp: not supported")

    monkeypatch.setattr(proc, "run", no_cp)
    target = tmp_path / "worktree"
    target.mkdir()
    assert cache.restore("k1", "npm", target) is True
    assert tree(target / "node_modules") == tree(source / "node_modules")


def test_a_missing_cp_binary_falls_back_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = stored(tmp_path)

    def missing(args: Sequence[str], **_: object) -> proc.Result:
        raise WtError("command not found: cp")

    monkeypatch.setattr(proc, "run", missing)
    target = tmp_path / "worktree"
    target.mkdir()
    assert cache.restore("k1", "npm", target) is True
    assert tree(target / "node_modules") == tree(source / "node_modules")


def test_cp_arguments_per_platform() -> None:
    src, dst = Path("/a/node_modules"), Path("/b/node_modules")
    assert cache._cp_args(src, dst, "linux") == [
        "cp",
        "-a",
        "--reflink=auto",
        "/a/node_modules",
        "/b/node_modules",
    ]
    assert cache._cp_args(src, dst, "darwin") == [
        "cp",
        "-Rpc",
        "/a/node_modules",
        "/b/node_modules",
    ]


def test_restore_uses_the_system_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stored(tmp_path)
    calls: list[list[str]] = []
    real = proc.run

    def recording(args: Sequence[str], **kwargs: object) -> proc.Result:
        calls.append(list(args))
        return real(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(proc, "run", recording)
    target = tmp_path / "worktree"
    target.mkdir()
    assert cache.restore("k1", "npm", target)
    assert [call[0] for call in calls] == ["cp"]


def test_restore_miss_returns_false_and_creates_nothing(tmp_path: Path) -> None:
    target = tmp_path / "worktree"
    target.mkdir()
    assert cache.restore("absent", "npm", target) is False
    assert list(target.iterdir()) == []


def test_restore_never_overwrites_an_existing_node_modules(tmp_path: Path) -> None:
    stored(tmp_path)
    target = tmp_path / "worktree"
    (target / "node_modules").mkdir(parents=True)
    (target / "node_modules" / "mine").write_text("keep")
    assert cache.restore("k1", "npm", target) is False
    assert (target / "node_modules" / "mine").read_text() == "keep"


def test_restore_updates_last_used(tmp_path: Path) -> None:
    stored(tmp_path)
    write_meta("k1", preset="npm", service="web", bytes=5, last_used="2020-01-01T00:00:00+00:00")
    target = tmp_path / "worktree"
    target.mkdir()
    assert cache.restore("k1", "npm", target)
    entry = cache.lookup("k1")
    assert entry is not None
    assert entry.last_used > "2020-01-01T00:00:00+00:00"
    assert (entry.preset, entry.service, entry.bytes) == ("npm", "web", 5)


def test_failed_copy_returns_false_and_leaves_no_partial_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored(tmp_path)

    def fail_cp(args: Sequence[str], **_: object) -> proc.Result:
        return proc.Result(1, "", "boom")

    def fail_copytree(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(proc, "run", fail_cp)
    monkeypatch.setattr("wt.cache.shutil.copytree", fail_copytree)
    target = tmp_path / "worktree"
    target.mkdir()
    assert cache.restore("k1", "npm", target) is False
    assert list(target.iterdir()) == []


def test_uv_presets_are_never_stored_or_restored(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    make_install(repo)
    assert cache.store("k1", "uv", "api", "uv sync", repo) is False
    assert cache.restore("k1", "uv", tmp_path) is False
    assert not installs_dir().exists()


def test_unsafe_keys_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    make_install(repo)
    assert cache.store("../escape", "npm", "web", "npm ci", repo) is False
    assert cache.lookup("..") is None
    assert cache.restore("a/b", "npm", tmp_path) is False


def test_store_without_a_node_modules_returns_false(tmp_path: Path) -> None:
    assert cache.store("k1", "npm", "web", "npm ci", tmp_path) is False
    assert cache.lookup("k1") is None


def test_failed_store_cleans_its_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    make_install(repo)

    def fail_cp(args: Sequence[str], **_: object) -> proc.Result:
        return proc.Result(1, "", "boom")

    def fail_copytree(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(proc, "run", fail_cp)
    monkeypatch.setattr("wt.cache.shutil.copytree", fail_copytree)
    assert cache.store("k1", "npm", "web", "npm ci", repo) is False
    assert list(installs_dir().iterdir()) == []
    assert cache.lookup("k1") is None


def test_store_replaces_a_stale_temp_dir(tmp_path: Path) -> None:
    stale = installs_dir() / "k1.tmp"
    stale.mkdir(parents=True)
    (stale / "junk").write_text("left by a crash")
    stored(tmp_path)
    assert sorted(p.name for p in installs_dir().iterdir()) == ["k1"]
    assert not (installs_dir() / "k1" / "junk").exists()


def test_storing_an_existing_key_keeps_the_first_entry(tmp_path: Path) -> None:
    stored(tmp_path, service="first")
    other = tmp_path / "other"
    other.mkdir()
    make_install(other)
    assert cache.store("k1", "npm", "second", "npm ci", other) is True
    entry = cache.lookup("k1")
    assert entry is not None
    assert entry.service == "first"


def test_concurrent_stores_of_one_key_leave_one_whole_entry(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    make_install(repo)
    outcomes: list[bool] = []

    def worker() -> None:
        outcomes.append(cache.store("k1", "npm", "web", "npm ci", repo))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == [True] * 6
    assert sorted(p.name for p in installs_dir().iterdir()) == ["k1"]
    assert tree(installs_dir() / "k1" / "node_modules") == tree(repo / "node_modules")


# --- listing, sizes, pruning -------------------------------------------------------------------


def test_entries_is_empty_without_a_store() -> None:
    assert cache.entries() == []
    assert cache.total_bytes() == 0
    assert cache.tool_sizes() == {"uv": 0, "npm": 0}


def test_entries_lists_complete_entries_sorted_and_skips_temp_dirs(tmp_path: Path) -> None:
    stored(tmp_path, key="b")
    stored(tmp_path, key="a")
    (installs_dir() / "c.tmp").mkdir()
    (installs_dir() / "stray-file").write_text("x")
    assert [e.key for e in cache.entries()] == ["a", "b"]


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"text"', "", '{"bytes": "many"}'])
def test_corrupt_meta_does_not_crash_listing(tmp_path: Path, content: str) -> None:
    stored(tmp_path)
    (installs_dir() / "k1" / "meta.json").write_text(content)
    [entry] = cache.entries()
    assert entry.key == "k1"
    assert entry.last_used == ""
    assert entry.bytes > 0
    assert cache.lookup("k1") == entry
    assert cache.total_bytes() >= entry.bytes


def test_missing_meta_lists_as_the_oldest(tmp_path: Path) -> None:
    stored(tmp_path)
    (installs_dir() / "k1" / "meta.json").unlink()
    assert [e.key for e in cache.entries()] == ["k1"]
    assert cache.prune().removed == ["k1"]


def test_restore_repairs_a_corrupt_meta(tmp_path: Path) -> None:
    stored(tmp_path)
    (installs_dir() / "k1" / "meta.json").write_text("{broken")
    target = tmp_path / "worktree"
    target.mkdir()
    assert cache.restore("k1", "npm", target) is True
    entry = cache.lookup("k1")
    assert entry is not None
    assert entry.last_used != ""


def test_tool_sizes_and_total(tmp_path: Path) -> None:
    stored(tmp_path)
    for tool, size in (("uv", 100), ("npm", 40)):
        folder = tool_cache_dir(tool)
        folder.mkdir(parents=True)
        (folder / "blob").write_bytes(b"x" * size)
    assert cache.tool_sizes() == {"uv": 100, "npm": 40}
    [entry] = cache.entries()
    assert cache.total_bytes() == entry.bytes + 140


def test_prune_removes_only_entries_older_than_the_cutoff(tmp_path: Path) -> None:
    for key in ("old", "fresh", "edge"):
        stored(tmp_path, key=key)
    write_meta("old", preset="npm", service="web", bytes=700, last_used=iso_days_ago(40))
    write_meta("fresh", preset="npm", service="web", bytes=5, last_used=iso_days_ago(1))
    write_meta("edge", preset="npm", service="web", bytes=5, last_used=iso_days_ago(29))
    result = cache.prune()
    assert result == cache.PruneResult(removed=["old"], bytes_freed=700)
    assert sorted(e.key for e in cache.entries()) == ["edge", "fresh"]
    assert not (installs_dir() / "old").exists()


def test_prune_custom_days(tmp_path: Path) -> None:
    for key in ("a", "b"):
        stored(tmp_path, key=key)
    write_meta("a", preset="npm", service="web", bytes=1, last_used=iso_days_ago(3))
    write_meta("b", preset="npm", service="web", bytes=1, last_used=iso_days_ago(1))
    assert cache.prune(older_than_days=2).removed == ["a"]


def test_prune_without_a_cutoff_removes_nothing(tmp_path: Path) -> None:
    stored(tmp_path)
    write_meta("k1", preset="npm", service="web", bytes=1, last_used=iso_days_ago(900))
    assert cache.prune(older_than_days=None) == cache.PruneResult(removed=[], bytes_freed=0)
    assert [e.key for e in cache.entries()] == ["k1"]


def test_prune_treats_corrupt_or_naive_dates_sensibly(tmp_path: Path) -> None:
    for key in ("garbled", "naive-old", "naive-new"):
        stored(tmp_path, key=key)
    write_meta("garbled", bytes=1, last_used="yesterday-ish")
    write_meta("naive-old", bytes=1, last_used=(datetime.now() - timedelta(days=60)).isoformat())
    write_meta("naive-new", bytes=1, last_used=datetime.now().isoformat())
    assert sorted(cache.prune().removed) == ["garbled", "naive-old"]


def test_prune_survives_corrupt_meta(tmp_path: Path) -> None:
    stored(tmp_path, key="bad")
    stored(tmp_path, key="good")
    (installs_dir() / "bad" / "meta.json").write_text("{nope")
    result = cache.prune()
    assert result.removed == ["bad"]
    assert result.bytes_freed > 0
    assert [e.key for e in cache.entries()] == ["good"]


def test_prune_everything_clears_entries_and_tool_caches(tmp_path: Path) -> None:
    stored(tmp_path, key="a")
    stored(tmp_path, key="b")
    for tool, size in (("uv", 100), ("npm", 40)):
        folder = tool_cache_dir(tool)
        (folder / "deep").mkdir(parents=True)
        (folder / "deep" / "blob").write_bytes(b"x" * size)
    before = cache.total_bytes()
    result = cache.prune(everything=True)
    assert sorted(result.removed) == ["a", "b", "npm", "uv"]
    assert result.bytes_freed == before
    assert cache.entries() == []
    assert cache.tool_sizes() == {"uv": 0, "npm": 0}
    assert not tool_cache_dir("uv").exists()


def test_prune_on_an_empty_cache(tmp_path: Path) -> None:
    assert cache.prune(everything=True) == cache.PruneResult(removed=[], bytes_freed=0)


def test_prune_does_not_follow_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    repo = tmp_path / "source"
    repo.mkdir()
    make_install(repo, outside=outside)
    assert cache.store("k1", "npm", "web", "npm ci", repo)
    assert cache.prune(everything=True).removed == ["k1"]
    assert (outside / "keep.txt").read_text() == "keep"


def test_prune_reports_a_folder_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored(tmp_path)
    write_meta("k1", preset="npm", service="web", bytes=1, last_used=iso_days_ago(90))

    def refuse(path: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("wt.cache.shutil.rmtree", refuse)
    with pytest.raises(WtError, match=r"cannot remove .*k1"):
        cache.prune()


def test_free_bytes_uses_the_nearest_existing_parent(isolated_home: Path) -> None:
    assert not isolated_home.exists()
    assert cache.free_bytes() > 0
    isolated_home.mkdir()
    assert cache.free_bytes() > 0
