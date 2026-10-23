"""git worktree add/remove, one repo at a time."""

from dataclasses import dataclass
from pathlib import Path

from wt import proc
from wt.errors import WtError


def _git(repo: Path, *args: str, check: bool = True) -> proc.Result:
    return proc.run(["git", "-C", str(repo), *args], check=check)


def check_repo(repo: Path) -> None:
    if not repo.is_dir():
        raise WtError(f"repo folder does not exist: {repo}")
    if not _git(repo, "rev-parse", "--git-dir", check=False).ok:
        raise WtError(f"not a git repository: {repo}")


def check_branch_name(branch: str) -> None:
    result = proc.run(["git", "check-ref-format", "--branch", branch], check=False)
    if not result.ok:
        raise WtError(f"{branch!r} is not a valid git branch name")


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str | None  # None when detached


def list_worktrees(repo: Path) -> list[Worktree]:
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    trees: list[Worktree] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path, branch = Path(line.removeprefix("worktree ")), None
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ").removeprefix("refs/heads/")
        elif not line and path is not None:
            trees.append(Worktree(path, branch))
            path = None
    return trees


def _has_ref(repo: Path, ref: str) -> bool:
    return _git(repo, "show-ref", "--verify", "--quiet", ref, check=False).ok


@dataclass(frozen=True)
class EnsureResult:
    path: Path
    created: bool
    branch_created: bool


def ensure_worktree(repo: Path, branch: str, target: Path) -> EnsureResult:
    """Make sure `branch` of `repo` is checked out somewhere, preferring `target`.

    - already checked out (main repo or another worktree): reuse it, wt does not own it
    - local branch exists: add a worktree for it
    - only origin/<branch> exists: add a worktree with a tracking branch
    - neither: create the branch from the repo's HEAD
    """
    check_repo(repo)
    _git(repo, "worktree", "prune")
    for tree in list_worktrees(repo):
        if tree.branch == branch:
            owned = tree.path.resolve() == target.resolve()
            return EnsureResult(tree.path, created=owned, branch_created=False)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise WtError(f"{target} exists and is not a worktree of {branch!r}; remove it first")
    target.parent.mkdir(parents=True, exist_ok=True)
    if _has_ref(repo, f"refs/heads/{branch}"):
        _git(repo, "worktree", "add", str(target), branch)
        return EnsureResult(target, created=True, branch_created=False)
    if _has_ref(repo, f"refs/remotes/origin/{branch}"):
        _git(repo, "worktree", "add", "--track", "-b", branch, str(target), f"origin/{branch}")
        return EnsureResult(target, created=True, branch_created=True)
    _git(repo, "worktree", "add", "-b", branch, str(target), "HEAD")
    return EnsureResult(target, created=True, branch_created=True)


def is_dirty(path: Path) -> bool:
    """Uncommitted or untracked files (both block `git worktree remove`)."""
    return bool(_git(path, "status", "--porcelain").stdout.strip())


def remove_worktree(repo: Path, path: Path, *, force: bool) -> None:
    """Remove the worktree folder. The branch and its commits are kept."""
    if path.exists():
        args = ["worktree", "remove", *(["--force"] if force else []), str(path)]
        _git(repo, *args)
    _git(repo, "worktree", "prune")


def branch_exists(repo: Path, branch: str) -> bool:
    return _has_ref(repo, f"refs/heads/{branch}")


def delete_branch(repo: Path, branch: str) -> None:
    """`git branch -D`: drops the branch even when it is not merged."""
    _git(repo, "branch", "-D", branch)
