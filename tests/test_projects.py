import json
from pathlib import Path

from wt.paths import project_dir
from wt.projects import known_projects
from wt.state import BranchState, StateStore


def write_state(project: str, config: Path | None, branches: list[str]) -> None:
    store = StateStore(project)
    with store.edit() as state:
        state.config_path = str(config) if config else None
        for b in branches:
            state.branches[b] = BranchState(branch=b, slug=b)


def test_lists_projects_with_branches_sorted(tmp_path: Path) -> None:
    cfg = tmp_path / "worktree.yaml"
    cfg.write_text("x")
    write_state("zeta", cfg, ["main"])
    write_state("alpha", cfg, ["a", "b"])
    found = known_projects()
    assert [p.name for p in found] == ["alpha", "zeta"]
    assert found[0].config_path == cfg
    assert found[0].config_exists


def test_skips_empty_corrupt_and_non_project_dirs(tmp_path: Path) -> None:
    write_state("empty", tmp_path / "w.yaml", [])
    project_dir("broken").mkdir(parents=True)
    (project_dir("broken") / "state.json").write_text("{nope")
    (project_dir("postgres")).mkdir(parents=True)  # shared infra: no state.json
    (project_dir("future")).mkdir(parents=True)
    (project_dir("future") / "state.json").write_text(json.dumps({"version": 99}))
    assert known_projects() == []


def test_missing_config_is_flagged(tmp_path: Path) -> None:
    write_state("gone", tmp_path / "nope" / "worktree.yaml", ["main"])
    write_state("noconfig", None, ["main"])
    found = {p.name: p for p in known_projects()}
    assert not found["gone"].config_exists
    assert not found["noconfig"].config_exists
    assert found["noconfig"].config_path is None
