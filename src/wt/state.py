"""~/.wt/<project>/state.json: what exists for each branch, so teardown is exact."""

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from wt.errors import WtError
from wt.paths import locks_dir, project_dir


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RepoState(BaseModel):
    source: str  # the main repo the worktree belongs to
    path: str
    created: bool  # False = the branch was already checked out elsewhere; never removed by wt
    branch_created: bool = False


class BranchState(BaseModel):
    branch: str
    slug: str
    status: Literal["creating", "up", "degraded", "removing", "failed"] = "creating"
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    repos: dict[str, RepoState] = Field(default_factory=dict)
    compose_projects: dict[str, str] = Field(default_factory=dict)  # repo -> compose project
    route_file: str | None = None
    cert_files: list[str] = Field(default_factory=list)
    providers: dict[str, list[str]] = Field(default_factory=dict)  # provider -> resource names
    provider_base: dict[str, str] = Field(default_factory=dict)  # provider -> base branch at create


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
