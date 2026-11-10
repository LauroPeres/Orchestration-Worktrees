import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import git as run_git
from wt import git, proc
from wt.errors import WtError


def branches(repo: Path) -> set[str]:
    return set(run_git(repo, "branch", "--format=%(refname:short)").split())


def test_creates_new_branch_from_head(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "wt" / "feat"
    res = git.ensure_worktree(repo, "feat/x", target)
    assert res == git.EnsureResult(target, created=True, branch_created=True, base="HEAD")
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


def test_move_worktree_keeps_files_and_branch(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    old = tmp_path / "old"
    new = tmp_path / "new"
    git.ensure_worktree(repo, "feat/x", old)
    (old / "wip.txt").write_text("keep me")
    git.move_worktree(repo, old, new)
    assert not old.exists()
    assert (new / "wip.txt").read_text() == "keep me"
    assert git.list_worktrees(repo)[-1] == git.Worktree(new, "feat/x")
    assert all(t.path != old for t in git.list_worktrees(repo))
    assert run_git(new, "branch", "--show-current").strip() == "feat/x"


def test_move_worktree_creates_missing_parents(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    old = tmp_path / "old"
    new = tmp_path / "deep" / "er" / "new"
    git.ensure_worktree(repo, "feat/x", old)
    git.move_worktree(repo, old, new)
    assert new.is_dir()


def test_move_worktree_refuses_existing_target(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    old = tmp_path / "old"
    new = tmp_path / "new"
    git.ensure_worktree(repo, "feat/x", old)
    new.mkdir()
    with pytest.raises(WtError, match="already exists; move or remove it first"):
        git.move_worktree(repo, old, new)
    assert old.is_dir()


def test_move_worktree_refuses_missing_source(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    old = tmp_path / "old"
    with pytest.raises(WtError, match="does not exist"):
        git.move_worktree(repo, old, tmp_path / "new")


def test_move_worktree_refuses_main_checkout(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    new = tmp_path / "moved"
    with pytest.raises(WtError) as excinfo:
        git.move_worktree(repo, repo, new)
    assert "\n" not in str(excinfo.value)
    assert repo.is_dir() and not new.exists()


def _origin_with_stale_clone(make_repo, tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bare origin, a clone (`app`) and a second clone (`other`) that pushes a newer commit."""
    run_git(tmp_path, "init", "-q", "--bare", "-b", "main", "origin.git")
    bare = tmp_path / "origin.git"
    seed = make_repo("seed")
    run_git(seed, "push", "-q", str(bare), "main")
    app, other = tmp_path / "app", tmp_path / "other"
    run_git(tmp_path, "clone", "-q", str(bare), str(app))
    run_git(tmp_path, "clone", "-q", str(bare), str(other))
    return bare, app, other


def _push_commit(other: Path, name: str) -> str:
    (other / name).write_text(name)
    run_git(other, "add", ".")
    run_git(other, "commit", "-qm", name)
    run_git(other, "push", "-q", "origin", "main")
    return run_git(other, "rev-parse", "HEAD").strip()


def test_new_branch_starts_at_latest_origin_main(make_repo, tmp_path: Path) -> None:
    _, app, other = _origin_with_stale_clone(make_repo, tmp_path)
    local_main = run_git(app, "rev-parse", "main").strip()
    tip = _push_commit(other, "newer.txt")
    target = tmp_path / "wt" / "feat"
    res = git.ensure_worktree(app, "feat/x", target)
    head = run_git(target, "rev-parse", "HEAD").strip()
    assert head == tip != local_main
    assert run_git(app, "rev-parse", "main").strip() == local_main  # never moved
    assert res.base == f"origin/main ({tip[:7]})"
    assert res.fetch_note is None and res.branch_created


def test_new_branch_has_no_upstream(make_repo, tmp_path: Path) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    git.ensure_worktree(app, "feat/x", tmp_path / "t")
    with pytest.raises(subprocess.CalledProcessError):
        run_git(app, "rev-parse", "--abbrev-ref", "feat/x@{upstream}")


def test_branch_only_on_origin_is_tracked_after_fetch(make_repo, tmp_path: Path) -> None:
    _, app, other = _origin_with_stale_clone(make_repo, tmp_path)
    run_git(other, "push", "-q", "origin", "main:feat/remote")
    assert "origin/feat/remote" not in run_git(app, "branch", "-r")
    target = tmp_path / "t"
    res = git.ensure_worktree(app, "feat/remote", target)
    assert res.created and res.base is None
    assert (
        run_git(target, "rev-parse", "--abbrev-ref", "@{upstream}").strip() == "origin/feat/remote"
    )


def test_fetch_failure_uses_last_known_origin_and_reports(make_repo, tmp_path: Path) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    known = run_git(app, "rev-parse", "origin/main").strip()
    run_git(app, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    target = tmp_path / "t"
    res = git.ensure_worktree(app, "feat/x", target)
    assert run_git(target, "rev-parse", "HEAD").strip() == known
    assert res.base == f"origin/main ({known[:7]})"
    assert res.fetch_note is not None
    assert res.fetch_note.startswith("could not fetch origin (")
    assert res.fetch_note.endswith("); using the last known origin state")


def test_fetch_failure_without_known_origin_branch_falls_back_to_head(
    make_repo, tmp_path: Path
) -> None:
    repo = make_repo()
    run_git(repo, "remote", "add", "origin", str(tmp_path / "gone.git"))
    res = git.ensure_worktree(repo, "feat/x", tmp_path / "t")
    assert res.base == "HEAD" and res.branch_created
    assert res.fetch_note is not None


def test_no_remote_branches_from_head_without_note(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    res = git.ensure_worktree(repo, "feat/x", tmp_path / "t")
    assert res.base == "HEAD" and res.fetch_note is None


def test_wt_no_fetch_skips_the_fetch(make_repo, tmp_path: Path, monkeypatch) -> None:
    _, app, other = _origin_with_stale_clone(make_repo, tmp_path)
    stale = run_git(app, "rev-parse", "origin/main").strip()
    _push_commit(other, "newer.txt")
    monkeypatch.setenv("WT_NO_FETCH", "1")
    target = tmp_path / "t"
    res = git.ensure_worktree(app, "feat/x", target)
    assert run_git(target, "rev-parse", "HEAD").strip() == stale
    assert res.base == f"origin/main ({stale[:7]})" and res.fetch_note is None


def test_existing_local_branch_is_not_fetched_or_moved(make_repo, tmp_path: Path) -> None:
    _, app, other = _origin_with_stale_clone(make_repo, tmp_path)
    local = run_git(app, "rev-parse", "main").strip()
    run_git(app, "branch", "feat/y")
    _push_commit(other, "newer.txt")
    target = tmp_path / "t"
    res = git.ensure_worktree(app, "feat/y", target)
    assert res.base is None and res.fetch_note is None and not res.branch_created
    assert run_git(target, "rev-parse", "HEAD").strip() == local
    assert run_git(app, "rev-parse", "origin/main").strip() == local  # no fetch happened


def test_origin_head_decides_the_base(make_repo, tmp_path: Path) -> None:
    _, app, other = _origin_with_stale_clone(make_repo, tmp_path)
    run_git(other, "push", "-q", "origin", "main:trunk")
    run_git(app, "fetch", "-q", "origin")
    run_git(app, "remote", "set-head", "origin", "trunk")
    res = git.ensure_worktree(app, "feat/x", tmp_path / "t")
    assert res.base is not None and res.base.startswith("origin/trunk (")


def test_uses_target_switched_to_another_branch(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "t"
    git.ensure_worktree(repo, "feat/x", target)
    run_git(target, "switch", "-q", "-c", "feat/x-step")
    res = git.ensure_worktree(repo, "feat/x", target)
    assert res.path == target and res.created and not res.branch_created
    assert res.other_branch == "feat/x-step"
    assert "feat/x" in branches(repo)


def test_uses_target_on_detached_head(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "t"
    git.ensure_worktree(repo, "feat/x", target)
    run_git(target, "switch", "-q", "--detach")
    res = git.ensure_worktree(repo, "feat/x", target)
    assert res.path == target and res.other_branch == git.DETACHED


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", name)


def _push_branch(app: Path, branch: str, *, merged: bool) -> None:
    """Push `branch` (created in `app`) to origin; `merged` also fast-forwards origin/main to it."""
    run_git(app, "switch", "-q", "-c", branch)
    _commit(app, branch.replace("/", "-"))
    run_git(app, "push", "-q", "-u", "origin", branch)
    run_git(app, "switch", "-q", "main")
    if merged:
        run_git(app, "merge", "-q", "--ff-only", branch)
        run_git(app, "push", "-q", "origin", "main")


@pytest.mark.parametrize("branch", ["feat/x", "plain"])
def test_remote_branch_merged_true_when_in_origin_default(
    make_repo, tmp_path: Path, branch: str
) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    _push_branch(app, branch, merged=True)
    assert git.remote_branch_merged(app, branch) == git.RemoteMerge(True, "origin/main")


def test_remote_branch_merged_false_when_not_in_origin_default(make_repo, tmp_path: Path) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    _push_branch(app, "feat/x", merged=False)
    assert git.remote_branch_merged(app, "feat/x") == git.RemoteMerge(False, "origin/main")


def test_remote_branch_merged_fetches_origin_first(make_repo, tmp_path: Path) -> None:
    _, app, other = _origin_with_stale_clone(make_repo, tmp_path)
    run_git(other, "switch", "-q", "-c", "feat/x")
    _commit(other, "x")
    run_git(other, "push", "-q", "origin", "feat/x")
    run_git(other, "switch", "-q", "main")
    run_git(other, "merge", "-q", "--ff-only", "feat/x")
    run_git(other, "push", "-q", "origin", "main")
    assert git.remote_branch_merged(app, "feat/x") == git.RemoteMerge(True, "origin/main")


def test_remote_branch_merged_none_without_origin(make_repo) -> None:
    assert git.remote_branch_merged(make_repo(), "feat/x") is None


def test_remote_branch_merged_none_without_that_remote_branch(make_repo, tmp_path: Path) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    assert git.remote_branch_merged(app, "feat/x") is None


def test_remote_branch_merged_raises_a_fetch_error_when_origin_is_unreachable(
    make_repo, tmp_path: Path
) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    run_git(app, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(git.FetchError) as caught:
        git.remote_branch_merged(app, "feat/x")
    assert caught.value.line  # the first line git wrote, e.g. "fatal: '...' does not appear ..."
    assert "\n" not in caught.value.line
    assert str(app) in str(caught.value)


def test_a_fetch_timeout_is_a_fetch_error(
    make_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    real = proc.run

    def run(args: list[str], **kwargs: Any) -> proc.Result:
        if "fetch" in args:
            raise proc.CommandTimeout(args, kwargs.get("timeout"))
        return real(args, **kwargs)

    monkeypatch.setattr(proc, "run", run)
    with pytest.raises(git.FetchError) as caught:
        git.remote_branch_merged(app, "feat/x")
    assert caught.value.line.startswith("command timed out after 30")


def _spy_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    real = proc.run

    def run(args: list[str], **kwargs: Any) -> proc.Result:
        calls.append((list(args), kwargs))
        return real(args, **kwargs)

    monkeypatch.setattr(proc, "run", run)
    return calls


def test_the_remote_git_calls_never_prompt_and_have_a_timeout(
    make_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    _push_branch(app, "feat/x", merged=True)
    calls = _spy_runs(monkeypatch)
    git.remote_branch_merged(app, "feat/x")
    git.delete_remote_branch(app, "feat/x")
    git.prune_remote_refs(app)
    remote = [(args, kw) for args, kw in calls if "fetch" in args or "push" in args]
    assert [args[3] for args, _ in remote] == ["fetch", "push", "fetch"]
    assert remote[1][0][3:] == ["push", "origin", "--delete", "refs/heads/feat/x"]
    for _, kwargs in remote:
        assert kwargs["env"] == {"GIT_TERMINAL_PROMPT": "0"}
        assert kwargs["timeout"] > 0


def test_branch_deletes_end_the_options_before_the_name(
    make_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    _push_branch(app, "feat/x", merged=True)
    run_git(app, "branch", "feat/y")
    calls = _spy_runs(monkeypatch)
    git.delete_branch_safe(app, "feat/x")
    git.delete_branch(app, "feat/y")
    git.drop_remote_ref(app, "feat/x")
    deletes = [args[3:] for args, _ in calls if "branch" in args]
    assert deletes == [
        ["branch", "-d", "--", "feat/x"],
        ["branch", "-D", "--", "feat/y"],
        ["branch", "-dr", "--", "origin/feat/x"],
    ]


def test_delete_remote_branch_removes_it_from_origin(make_repo, tmp_path: Path) -> None:
    bare, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    _push_branch(app, "feat/x", merged=True)
    git.delete_remote_branch(app, "feat/x")
    assert "feat/x" not in run_git(bare, "branch", "--format=%(refname:short)").split()
    assert "origin/feat/x" not in run_git(app, "branch", "-r").split()


def test_drop_remote_ref_forgets_the_local_origin_ref(make_repo, tmp_path: Path) -> None:
    bare, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    _push_branch(app, "feat/x", merged=True)
    run_git(bare, "branch", "-D", "feat/x")  # the remote branch is gone, the local ref stays
    assert "origin/feat/x" in run_git(app, "branch", "-r").split()
    git.drop_remote_ref(app, "feat/x")
    assert "origin/feat/x" not in run_git(app, "branch", "-r").split()


def test_drop_remote_ref_is_a_no_op_when_absent(make_repo, tmp_path: Path) -> None:
    _, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    git.drop_remote_ref(app, "feat/nope")


def _gone_branch(app: Path, bare: Path, branch: str, *, merged: bool) -> None:
    """A local `branch` tracking an origin branch that was then deleted on the remote."""
    _push_branch(app, branch, merged=merged)
    run_git(bare, "branch", "-D", branch)
    run_git(app, "fetch", "-q", "--prune", "origin")


def test_gone_merged_branches_lists_only_gone_and_merged(make_repo, tmp_path: Path) -> None:
    bare, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    _gone_branch(app, bare, "feat/merged", merged=True)
    _gone_branch(app, bare, "feat/unmerged", merged=False)
    _push_branch(app, "feat/alive", merged=True)  # upstream still there
    assert git.gone_merged_branches(app) == ["feat/merged"]


def test_gone_merged_branches_skips_checked_out_branches(make_repo, tmp_path: Path) -> None:
    bare, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    _gone_branch(app, bare, "feat/here", merged=True)
    _gone_branch(app, bare, "feat/there", merged=True)
    git.ensure_worktree(app, "feat/there", tmp_path / "wt-there")
    run_git(app, "switch", "-q", "feat/here")
    assert git.gone_merged_branches(app) == []


def test_gone_merged_branches_empty_without_origin(make_repo) -> None:
    assert git.gone_merged_branches(make_repo()) == []


def test_delete_branch_safe_refuses_unmerged_branch(make_repo) -> None:
    repo = make_repo()
    run_git(repo, "switch", "-q", "-c", "feat/x")
    _commit(repo, "x")
    run_git(repo, "switch", "-q", "main")
    with pytest.raises(WtError):
        git.delete_branch_safe(repo, "feat/x")
    run_git(repo, "merge", "-q", "feat/x")
    git.delete_branch_safe(repo, "feat/x")
    assert "feat/x" not in branches(repo)


def test_prune_remote_refs_reports_a_stale_ref(make_repo, tmp_path: Path) -> None:
    bare, app, _ = _origin_with_stale_clone(make_repo, tmp_path)
    _push_branch(app, "feat/x", merged=False)
    run_git(bare, "branch", "-D", "feat/x")
    assert git.prune_remote_refs(app) == ["origin/feat/x"]
    assert git.prune_remote_refs(app) == []


def test_prune_remote_refs_empty_without_origin(make_repo) -> None:
    assert git.prune_remote_refs(make_repo()) == []


def test_prune_worktrees_reports_entries_of_deleted_folders(make_repo, tmp_path: Path) -> None:
    repo = make_repo()
    target = tmp_path / "t"
    git.ensure_worktree(repo, "feat/x", target)
    shutil.rmtree(target)
    pruned = git.prune_worktrees(repo)
    assert len(pruned) == 1 and "worktrees/t" in pruned[0]
    assert git.prune_worktrees(repo) == []
