"""Projects wt knows about: every `$WT_HOME/<project>/state.json` that has branches."""

from dataclasses import dataclass
from pathlib import Path

from wt.errors import WtError
from wt.paths import wt_home
from wt.state import StateStore


@dataclass(frozen=True)
class KnownProject:
    name: str
    config_path: Path | None

    @property
    def config_exists(self) -> bool:
        return self.config_path is not None and self.config_path.is_file()


@dataclass(frozen=True)
class UnreadableProject:
    """A project folder whose state.json exists but cannot be loaded (corrupt or newer)."""

    name: str
    reason: str  # the load error, which names the state file


@dataclass(frozen=True)
class ProjectScan:
    known: list[KnownProject]
    unreadable: list[UnreadableProject]


def known_projects() -> list[KnownProject]:
    """Projects with at least one branch in their state, sorted by name.

    Folders without a readable state.json (shared infra, corrupt or newer files) are skipped.
    """
    return scan_projects().known


def scan_projects() -> ProjectScan:
    """`known_projects`, plus the projects whose state.json exists but cannot be loaded.

    For callers that must not mistake an unreadable project for one without branches.
    """
    home = wt_home()
    if not home.is_dir():
        return ProjectScan([], [])
    found: list[KnownProject] = []
    unreadable: list[UnreadableProject] = []
    for folder in sorted(home.iterdir()):
        if not (folder / "state.json").is_file():
            continue
        try:
            state = StateStore(folder.name).load()
        except WtError as exc:
            unreadable.append(UnreadableProject(folder.name, str(exc)))
            continue
        if state.branches:
            path = Path(state.config_path) if state.config_path else None
            found.append(KnownProject(folder.name, path))
    return ProjectScan(found, unreadable)
