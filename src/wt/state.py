"""~/.wt/<project>/state.json: what exists for each branch, so teardown is exact."""

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from wt.errors import WtError
from wt.naming import PORT_STRIDE, host_port
from wt.paths import locks_dir, project_dir, wt_home


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RepoState(BaseModel):
    source: str  # the main repo the worktree belongs to
    path: str
    created: bool  # False = the branch was already checked out elsewhere; never removed by wt
    branch_created: bool = False


class ProcessState(BaseModel):
    """A host process started for a `run:` service."""

    pid: int
    pgid: int
    port: int | None = None
    command: str
    started_at: str = Field(default_factory=now)
    log: str
    env_hash: str | None = None  # sha256 of the rendered env; None in older state files


class BranchState(BaseModel):
    branch: str
    slug: str
    status: Literal["creating", "up", "degraded", "removing", "failed", "stopped"] = "creating"
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    repos: dict[str, RepoState] = Field(default_factory=dict)
    compose_projects: dict[str, str] = Field(default_factory=dict)  # repo -> compose project
    route_file: str | None = None
    cert_files: list[str] = Field(default_factory=list)
    providers: dict[str, list[str]] = Field(default_factory=dict)  # provider -> resource names
    ports: dict[str, int] = Field(default_factory=dict)  # service -> reserved host port
    processes: dict[str, ProcessState] = Field(default_factory=dict)  # service -> host process
    provider_base: dict[str, str] = Field(default_factory=dict)  # provider -> base branch at create
    index: int | None = None  # worktree index, unique across projects (see allocate_index)


STATE_VERSION = 1


class State(BaseModel):
    version: int = STATE_VERSION
    project: str
    config_path: str | None = None
    branches: dict[str, BranchState] = Field(default_factory=dict)


@contextmanager
def file_lock(name: str) -> Iterator[None]:
    """Cross-process lock (flock) on ~/.wt/locks/<name>.lock."""
    folder = locks_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{name}.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class StateStore:
    def __init__(self, project: str) -> None:
        self.project = project
        self.path = project_dir(project) / "state.json"

    def load(self) -> State:
        if not self.path.exists():
            return State(project=self.project)
        try:
            raw = json.loads(self.path.read_text())
            version = raw.get("version", 1) if isinstance(raw, dict) else 1
            if not isinstance(version, int) or isinstance(version, bool):
                raise ValueError(f"state version must be an integer, got {version!r}")
            if version > STATE_VERSION:
                raise WtError(
                    f"{self.path} was written by a newer wt (state version {version}); upgrade wt."
                )
            return State.model_validate(raw)
        except (ValueError, OSError) as exc:  # JSONDecodeError, ValidationError, read errors
            raise WtError(
                f"State file {self.path} is corrupt or unreadable ({exc.__class__.__name__}). "
                "Fix it by hand or delete it."
            ) from exc

    def _save(self, state: State) -> None:
        atomic_write(self.path, state.model_dump_json(indent=2) + "\n")

    @contextmanager
    def edit(self) -> Iterator[State]:
        """Load, let the caller change, save - all under the project lock."""
        with file_lock(f"state-{self.project}"):
            state = self.load()
            yield state
            self._save(state)


def ports_lock() -> AbstractContextManager[None]:
    """The global lock that guards the worktree index registry and port reservation."""
    return file_lock("ports")


def index_registry_path() -> Path:
    return wt_home() / "worktree-index.json"


def _read_registry() -> dict[int, str]:
    """{index: "<project>/<branch>"}. A missing or unreadable file is rebuilt from the state
    files; entries the state files record but the registry lacks are added."""
    registry: dict[int, str] = {}
    path = index_registry_path()
    try:
        raw = json.loads(path.read_text())
        registry = {int(key): str(owner) for key, owner in raw.items()}
    except (OSError, ValueError, AttributeError):
        registry = {}
    home = wt_home()
    folders = sorted(home.iterdir()) if home.is_dir() else []
    for folder in folders:
        if not (folder / "state.json").is_file():
            continue
        try:
            state = StateStore(folder.name).load()
        except WtError:
            continue
        for name, branch in state.branches.items():
            if branch.index is not None:
                registry.setdefault(branch.index, f"{folder.name}/{name}")
    return registry


def _write_registry(registry: dict[int, str]) -> None:
    data = {str(index): registry[index] for index in sorted(registry)}
    atomic_write(index_registry_path(), json.dumps(data, indent=2) + "\n")


def allocate_index(project: str, branch: str) -> int:
    """The smallest worktree index no branch of any project uses; the same one again for a
    branch that already has it. The caller holds `ports_lock()`."""
    owner = f"{project}/{branch}"
    registry = _read_registry()
    for index, held_by in registry.items():
        if held_by == owner:
            return index
    index = next(i for i in range(len(registry) + 1) if i not in registry)
    host_port(index, PORT_STRIDE - 1)  # WtError when the port would pass 65535
    registry[index] = owner
    _write_registry(registry)
    return index


def release_index(project: str, branch: str) -> None:
    """Free the index of a branch that was torn down (not stopped). The caller holds
    `ports_lock()`."""
    owner = f"{project}/{branch}"
    registry = _read_registry()
    kept = {index: held_by for index, held_by in registry.items() if held_by != owner}
    if kept != registry or index_registry_path().exists():
        _write_registry(kept)
