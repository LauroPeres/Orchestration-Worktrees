"""git worktree add/remove, one repo at a time."""

import os
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
    base: str | None = None  # where a created branch started: "origin/main (a1b2c3d)" or "HEAD"
    fetch_note: str | None = None  # why `git fetch origin` did not happen or failed


FETCH_TIMEOUT_SECONDS = 30
NO_FETCH_ENV = "WT_NO_FETCH"
FALLBACK_BASES = ("origin/main", "origin/master")


def _has_origin(repo: Path) -> bool:
    return "origin" in _git(repo, "remote").stdout.split()


def _fetch_origin(repo: Path) -> str | None:
    """`git fetch origin` when there is an origin and fetching is not disabled.

    Returns a note when the fetch failed (the caller carries on with the last known state).
    """
    if os.environ.get(NO_FETCH_ENV) == "1" or not _has_origin(repo):
        return None
    try:
        result = _git_with_timeout(repo, "fetch", "origin")
    except WtError as err:
        reason = str(err).splitlines()[0]
    else:
        if result.ok:
            return None
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        reason = lines[0].removeprefix("fatal: ") if lines else "git fetch failed"
    return f"could not fetch origin ({reason}); using the last known origin state"


def _git_with_timeout(repo: Path, *args: str) -> proc.Result:
    return proc.run(["git", "-C", str(repo), *args], check=False, timeout=FETCH_TIMEOUT_SECONDS)


def _origin_base(repo: Path) -> str | None:
    """The remote's default branch (`origin/HEAD`), else the first of origin/main, origin/master."""
    head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    candidates = [head.stdout.strip()] if head.ok and head.stdout.strip() else []
    for base in [*candidates, *FALLBACK_BASES]:
        if _has_ref(repo, f"refs/remotes/{base}"):
            return base
    return None


def _create_branch(repo: Path, branch: str, target: Path) -> str:
    """Create `branch` in a new worktree from the origin default branch, else HEAD; no upstream."""
    base = _origin_base(repo)
    start = base or "HEAD"
    _git(repo, "worktree", "add", "--no-track", "-b", branch, str(target), start)
    if base is None:
        return "HEAD"
    return f"{base} ({_git(repo, 'rev-parse', '--short', base).stdout.strip()})"


def ensure_worktree(repo: Path, branch: str, target: Path) -> EnsureResult:
    """Make sure `branch` of `repo` is checked out somewhere, preferring `target`.

    - already checked out (main repo or another worktree): reuse it, wt does not own it
    - local branch exists: add a worktree for it
    - only origin/<branch> exists: add a worktree with a tracking branch
    - neither: create the branch from origin's default branch (no upstream), else from HEAD

    Origin is fetched once, only when the branch is not local (SPEC section 28); a failed fetch
    is reported in `fetch_note`, never raised.
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
    fetch_note = _fetch_origin(repo)
    if _has_ref(repo, f"refs/remotes/origin/{branch}"):
        _git(repo, "worktree", "add", "--track", "-b", branch, str(target), f"origin/{branch}")
        return EnsureResult(target, created=True, branch_created=True, fetch_note=fetch_note)
    base = _create_branch(repo, branch, target)
    return EnsureResult(target, created=True, branch_created=True, base=base, fetch_note=fetch_note)


def is_dirty(path: Path) -> bool:
    """Uncommitted or untracked files (both block `git worktree remove`)."""
    return bool(_git(path, "status", "--porcelain").stdout.strip())


def remove_worktree(repo: Path, path: Path, *, force: bool) -> None:
    """Remove the worktree folder. The branch and its commits are kept."""
    if path.exists():
        args = ["worktree", "remove", *(["--force"] if force else []), str(path)]
        _git(repo, *args)
    _git(repo, "worktree", "prune")


def move_worktree(repo: Path, old: Path, new: Path) -> None:
    """`git worktree move`: keeps files, branch and git's own bookkeeping."""
    if new.exists():
        raise WtError(f"{new} already exists; move or remove it first")
    if not old.exists():
        raise WtError(f"{old} does not exist")
    new.parent.mkdir(parents=True, exist_ok=True)
    result = _git(repo, "worktree", "move", str(old), str(new), check=False)
    if not result.ok:
        reason = (result.stderr or result.stdout).strip().splitlines()
        raise WtError(f"cannot move {old} to {new}: {reason[0] if reason else 'git failed'}")
    _git(repo, "worktree", "prune")


def branch_exists(repo: Path, branch: str) -> bool:
    return _has_ref(repo, f"refs/heads/{branch}")


def delete_branch(repo: Path, branch: str) -> None:
    """`git branch -D`: drops the branch even when it is not merged."""
    _git(repo, "branch", "-D", branch)
