import shutil
from pathlib import Path

import pytest

from tests.conftest import git as run_git
from wt import git
from wt.errors import WtError


def branches(repo: Path) -> set[str]:
    return set(run_git(repo, "branch", "--format=%(refname:short)").split())


def test_creates_new_branch_from_head(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "wt" / "feat"
    res = git.ensure_worktree(repo, "feat/x", target)
    assert res == git.EnsureResult(target, created=True, branch_created=True)
    assert (target / "README.md").exists()
    assert "feat/x" in branches(repo)


def test_existing_local_branch(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    run_git(repo, "branch", "feat/y")
    res = git.ensure_worktree(repo, "feat/y", tmp_path / "t")
    assert res.created and not res.branch_created


def test_rerun_is_idempotent(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "t"
    git.ensure_worktree(repo, "feat/x", target)
    again = git.ensure_worktree(repo, "feat/x", target)
    assert again.path == target and again.created and not again.branch_created


def test_branch_checked_out_elsewhere_is_reused_not_owned(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    res = git.ensure_worktree(repo, "main", tmp_path / "t")
    assert res.path == repo and not res.created
    assert not (tmp_path / "t").exists()


def test_remote_branch_gets_tracking_branch(make_repo, tmp_path: Path) -> None:
    origin = make_repo("origin")
    run_git(origin, "branch", "feat/remote")
    clone = tmp_path / "clone"
    run_git(tmp_path, "clone", "-q", str(origin), str(clone))
    res = git.ensure_worktree(clone, "feat/remote", tmp_path / "t")
    assert res.created
    upstream = run_git(tmp_path / "t", "rev-parse", "--abbrev-ref", "@{upstream}").strip()
    assert upstream == "origin/feat/remote"


def test_refuses_non_empty_foreign_folder(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "t"
    target.mkdir()
    (target / "file").write_text("x")
    with pytest.raises(WtError, match="not a worktree"):
        git.ensure_worktree(repo, "feat/x", target)


def test_dirty_and_remove_keeps_branch(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "t"
    git.ensure_worktree(repo, "feat/x", target)
    assert not git.is_dirty(target)
    (target / "new.txt").write_text("x")
    assert git.is_dirty(target)
    with pytest.raises(WtError):
        git.remove_worktree(repo, target, force=False)
    git.remove_worktree(repo, target, force=True)
    assert not target.exists()
    assert "feat/x" in branches(repo)
    git.remove_worktree(repo, target, force=False)  # already gone: no error


def test_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(WtError, match="not a git repository"):
        git.check_repo(tmp_path)


def test_branch_name_validation() -> None:
    git.check_branch_name("feat/ok")
    with pytest.raises(WtError):
        git.check_branch_name("bad..name")


def test_recreates_worktree_deleted_by_hand(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "t"
    git.ensure_worktree(repo, "feat/x", target)
    shutil.rmtree(target)
    res = git.ensure_worktree(repo, "feat/x", target)
    assert res.created and target.is_dir()
    assert any(t.path == target for t in git.list_worktrees(repo))


def test_target_that_is_a_file_is_refused(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "t"
    target.write_text("x")
    with pytest.raises(WtError, match="not a worktree"):
        git.ensure_worktree(repo, "feat/x", target)
