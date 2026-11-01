"""Install caches (SPEC section 24): restorable `node_modules` entries and the shared tool caches.

An entry lives in `$WT_HOME/cache/installs/<key>/` as `node_modules/` plus `meta.json`. Entries are
copied in and out, so pruning the store never breaks a worktree. The uv and npm tool caches are
plain folders that the tools themselves manage; wt only points them there and measures them.
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from wt import proc
from wt.errors import WtError
from wt.paths import installs_dir, tool_cache_dir, wt_home
from wt.state import atomic_write, file_lock, now

Preset = Literal["uv", "npm"]

META_FILE = "meta.json"
TMP_SUFFIX = ".tmp"
DEFAULT_PRUNE_DAYS = 30
TOOLS: tuple[Preset, ...] = ("uv", "npm")
_KEY_PATTERN = re.compile(r"[0-9A-Za-z_-]+")  # a key becomes a folder name: no separators, no dots


@dataclass(frozen=True)
class _PresetSpec:
    lock_files: tuple[str, ...]
    required: tuple[str, ...]
    cached_path: str | None  # what an entry stores; None = the preset shares only the tool cache
    env_var: str


_PRESETS: dict[Preset, _PresetSpec] = {
    "npm": _PresetSpec(
        lock_files=("package-lock.json", ".nvmrc", ".node-version"),
        required=("package-lock.json",),
        cached_path="node_modules",
        env_var="npm_config_cache",
    ),
    "uv": _PresetSpec(
        lock_files=("uv.lock", ".python-version", "pyproject.toml"),
        required=(),
        cached_path=None,
        env_var="UV_CACHE_DIR",
    ),
}


@dataclass(frozen=True)
class Entry:
    key: str
    preset: str
    service: str
    bytes: int
    last_used: str  # ISO 8601; empty when the metadata is unreadable


@dataclass(frozen=True)
class PruneResult:
    removed: list[str]  # entry keys, plus "uv" / "npm" when a tool cache folder was removed
    bytes_freed: int


# --- keys and presets -------------------------------------------------------------------------


def install_key(
    preset: Preset, lock_files: Mapping[str, bytes], setup_text: str, tool_version: str
) -> str:
    """sha256 hex of everything that decides what an install produces."""
    document = {
        "preset": preset,
        "lock_files": {name: hashlib.sha256(lock_files[name]).hexdigest() for name in lock_files},
        "setup": setup_text,
        "tool_version": tool_version,
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def lock_file_names(preset: Preset) -> tuple[str, ...]:
    return _PRESETS[preset].lock_files


def read_lock_files(repo_dir: Path, preset: Preset) -> dict[str, bytes]:
    """The lock files that exist in `repo_dir`; a missing required one is an error."""
    spec = _PRESETS[preset]
    found: dict[str, bytes] = {}
    for name in spec.lock_files:
        path = repo_dir / name
        if path.is_file():
            try:
                found[name] = path.read_bytes()
            except OSError as exc:
                raise WtError(f"setup_cache: cannot read {path}: {exc}") from exc
    for name in spec.required:
        if name not in found:
            raise WtError(f"setup_cache: {preset} needs {name} in {repo_dir}")
    return found


def restorable(preset: Preset) -> bool:
    """True when an install can be restored from the store (uv shares only its own cache)."""
    return _PRESETS[preset].cached_path is not None


def cached_path(preset: Preset) -> str:
    path = _PRESETS[preset].cached_path
    if path is None:
        raise WtError(
            f"setup_cache: {preset} has no install to store; it shares only its tool cache"
        )
    return path


def tool_env(preset: Preset) -> dict[str, str]:
    """The variable that points the tool at wt's shared cache folder (nothing is created)."""
    return {_PRESETS[preset].env_var: str(tool_cache_dir(preset))}


# --- the store ---------------------------------------------------------------------------------


def _entry_dir(key: str) -> Path | None:
    return installs_dir() / key if _KEY_PATTERN.fullmatch(key) else None


def _tree_bytes(path: Path) -> int:
    """Bytes under `path`; symlinks count as links, never followed."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue  # vanished while walking: a concurrent prune or install
    return total


def _read_meta(entry_dir: Path) -> dict[str, object]:
    try:
        raw = json.loads((entry_dir / META_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _text(meta: Mapping[str, object], field: str) -> str:
    value = meta.get(field)
    return value if isinstance(value, str) else ""


def _read_entry(entry_dir: Path) -> Entry:
    """An entry whose metadata is missing or corrupt still lists, as the oldest one."""
    meta = _read_meta(entry_dir)
    size = meta.get("bytes")
    return Entry(
        key=entry_dir.name,
        preset=_text(meta, "preset"),
        service=_text(meta, "service"),
        bytes=size
        if isinstance(size, int) and not isinstance(size, bool)
        else _tree_bytes(entry_dir),
        last_used=_text(meta, "last_used"),
    )


def lookup(key: str) -> Entry | None:
    folder = _entry_dir(key)
    if folder is None or not folder.is_dir():
        return None
    return _read_entry(folder)


def entries() -> list[Entry]:
    root = installs_dir()
    if not root.is_dir():
        return []
    folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.endswith(TMP_SUFFIX))
    return [_read_entry(folder) for folder in folders]


def _cp_args(source: Path, destination: Path, platform: str) -> list[str]:
    """Clone-capable copy that keeps symlinks (as links) and modes."""
    if platform == "darwin":
        return ["cp", "-Rpc", str(source), str(destination)]
    return ["cp", "-a", "--reflink=auto", str(source), str(destination)]


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a folder to a path that does not exist yet, without following symlinks.

    Tries the system `cp` (a reflink or clone copy where the filesystem allows), then falls back
    to a plain recursive copy. Raises OSError when both fail.
    """
    try:
        copied = proc.run(_cp_args(source, destination, sys.platform), check=False).ok
    except WtError:
        copied = False  # no cp binary, or it timed out: the fallback decides
    if copied:
        return
    shutil.rmtree(destination, ignore_errors=True)  # drop what a failed cp left behind
    shutil.copytree(source, destination, symlinks=True)


def _touch(key: str, entry_dir: Path) -> None:
    """Record a use; under the key lock so a concurrent prune cannot be undone by the write."""
    with file_lock(f"cache-{key}"):
        if not entry_dir.is_dir():
            return
        meta = _read_meta(entry_dir)
        meta["last_used"] = now()
        atomic_write(entry_dir / META_FILE, json.dumps(meta, indent=2))


def restore(key: str, preset: Preset, target_dir: Path) -> bool:
    """Copy the stored install into `target_dir`; False when there is nothing to restore.

    Never raises and never overwrites: an existing target or a failed copy returns False, and a
    half-copied tree is removed.
    """
    entry_dir = _entry_dir(key)
    if entry_dir is None or not restorable(preset):
        return False
    source = entry_dir / cached_path(preset)
    destination = target_dir / cached_path(preset)
    if not source.is_dir() or os.path.lexists(destination):
        return False
    staging = target_dir / f".{destination.name}{TMP_SUFFIX}"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        _copy_tree(source, staging)
        os.replace(staging, destination)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return False
    with contextlib.suppress(OSError, WtError):  # last_used only orders pruning
        _touch(key, entry_dir)
    return True


def store(key: str, preset: Preset, service: str, setup_text: str, source_dir: Path) -> bool:
    """Save `source_dir`'s install under `key` (atomic); False on failure, nothing left behind."""
    entry_dir = _entry_dir(key)
    if entry_dir is None or not restorable(preset):
        return False
    source = source_dir / cached_path(preset)
    if not source.is_dir():
        return False
    staging = entry_dir.with_name(entry_dir.name + TMP_SUFFIX)
    try:
        with file_lock(f"cache-{key}"):
            if entry_dir.is_dir():
                return True  # a concurrent store won; same key means same install
            try:
                _write_staging(staging, source, preset, service, setup_text)
                os.replace(staging, entry_dir)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    except (OSError, WtError):
        return False
    return True


def _write_staging(staging: Path, source: Path, preset: Preset, service: str, setup: str) -> None:
    shutil.rmtree(staging, ignore_errors=True)  # a crashed earlier store
    staging.mkdir(parents=True)
    copied = staging / cached_path(preset)
    _copy_tree(source, copied)
    stamp = now()
    meta = {
        "preset": preset,
        "service": service,
        "setup": setup,
        "created_at": stamp,
        "last_used": stamp,
        "bytes": _tree_bytes(copied),
    }
    atomic_write(staging / META_FILE, json.dumps(meta, indent=2))


# --- sizes, pruning and disk -------------------------------------------------------------------


def tool_sizes() -> dict[str, int]:
    return {tool: _tree_bytes(tool_cache_dir(tool)) for tool in TOOLS}


def total_bytes() -> int:
    return sum(entry.bytes for entry in entries()) + sum(tool_sizes().values())


def _last_used(entry: Entry) -> datetime:
    """Unparseable or missing counts as the oldest possible time."""
    try:
        parsed = datetime.fromisoformat(entry.last_used)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _remove(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise WtError(f"cannot remove {path}: {exc}") from exc


def prune(
    *, older_than_days: int | None = DEFAULT_PRUNE_DAYS, everything: bool = False
) -> PruneResult:
    """Remove entries not used for `older_than_days`; `everything` also clears the tool caches."""
    cutoff = (
        None if older_than_days is None else datetime.now(UTC) - timedelta(days=older_than_days)
    )
    removed: list[str] = []
    freed = 0
    for entry in entries():
        if not everything and (cutoff is None or _last_used(entry) >= cutoff):
            continue
        with file_lock(f"cache-{entry.key}"):
            _remove(installs_dir() / entry.key)
        removed.append(entry.key)
        freed += entry.bytes
    if everything:
        for tool in TOOLS:
            folder = tool_cache_dir(tool)
            if folder.is_dir():
                size = _tree_bytes(folder)
                _remove(folder)
                removed.append(tool)
                freed += size
    return PruneResult(removed=removed, bytes_freed=freed)


def free_bytes() -> int:
    """Free space on the disk that holds WT_HOME (or its nearest existing parent)."""
    folder = wt_home().absolute()
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return shutil.disk_usage(folder).free
