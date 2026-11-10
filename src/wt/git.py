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
    # the target was already a worktree on another branch, used as is (SPEC section 33);
    # DETACHED for a detached HEAD
    other_branch: str | None = None


DETACHED = "a detached HEAD"


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
    - `target` is a worktree of `repo` on another branch: use it as is, wt owns it
    - local branch exists: add a worktree for it
    - only origin/<branch> exists: add a worktree with a tracking branch
    - neither: create the branch from origin's default branch (no upstream), else from HEAD

    Origin is fetched once, only when the branch is not local (SPEC section 28); a failed fetch
    is reported in `fetch_note`, never raised.
    """
    check_repo(repo)
    _git(repo, "worktree", "prune")
    trees = list_worktrees(repo)
    for tree in trees:
        if tree.branch == branch:
            owned = tree.path.resolve() == target.resolve()
            return EnsureResult(tree.path, created=owned, branch_created=False)
    for tree in trees:
        if target.exists() and tree.path.resolve() == target.resolve():
            other = tree.branch or DETACHED
            return EnsureResult(target, created=True, branch_created=False, other_branch=other)
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
    _git(repo, "branch", "-D", "--", branch)


def delete_branch_safe(repo: Path, branch: str) -> None:
    """`git branch -d`: refuses a branch that is not merged."""
    _git(repo, "branch", "-d", "--", branch)


PUSH_TIMEOUT_SECONDS = 30
# git must fail rather than ask for credentials: `remove` and `prune` run unattended
NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


class FetchError(WtError):
    """`git fetch origin` failed or timed out; `line` is the first line git (or wt) reported."""

    def __init__(self, repo: Path, line: str) -> None:
        self.line = line
        super().__init__(f"git fetch origin failed in {repo}: {line}")


def _first_error_line(exc: WtError) -> str:
    text = exc.result.stderr if isinstance(exc, proc.CommandError) else ""
    lines = [line.strip() for line in [*text.splitlines(), *str(exc).splitlines()]]
    return next((line for line in lines if line), type(exc).__name__)


def _fetch_checked(repo: Path, *args: str) -> None:
    try:
        proc.run(
            ["git", "-C", str(repo), "fetch", *args, "origin"],
            timeout=FETCH_TIMEOUT_SECONDS,
            env=NO_PROMPT_ENV,
        )
    except WtError as exc:
        raise FetchError(repo, _first_error_line(exc)) from exc


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """`git merge-base --is-ancestor`: exit 0 yes, 1 no, anything else is a real failure."""
    result = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    if result.returncode not in (0, 1):
        reason = (result.stderr.strip().splitlines() or ["git failed"])[0]
        raise WtError(f"git merge-base --is-ancestor failed in {repo}: {reason}")
    return result.ok


@dataclass(frozen=True)
class RemoteMerge:
    merged: bool
    base: str  # origin's default branch it was checked against, e.g. "origin/main"


def remote_branch_merged(repo: Path, branch: str) -> RemoteMerge | None:
    """Fetch origin (no prune); is `origin/<branch>` merged into origin's default branch?

    None when it cannot be told: no origin, no `origin/<branch>` or no default branch on origin.
    Raises `FetchError` when the fetch fails, `WtError` when git cannot answer.
    """
    if not _has_origin(repo):
        return None
    _fetch_checked(repo)
    base = _origin_base(repo)
    if base is None or not _has_ref(repo, f"refs/remotes/origin/{branch}"):
        return None
    merged = _is_ancestor(repo, f"refs/remotes/origin/{branch}", f"refs/remotes/{base}")
    return RemoteMerge(merged, base)


def delete_remote_branch(repo: Path, branch: str) -> None:
    """`git push origin --delete refs/heads/<branch>`, never prompting, with a timeout."""
    proc.run(
        ["git", "-C", str(repo), "push", "origin", "--delete", f"refs/heads/{branch}"],
        timeout=PUSH_TIMEOUT_SECONDS,
        env=NO_PROMPT_ENV,
    )


def drop_remote_ref(repo: Path, branch: str) -> None:
    """Forget the local `origin/<branch>` ref; no-op when it is already gone."""
    if _has_ref(repo, f"refs/remotes/origin/{branch}"):
        _git(repo, "branch", "-dr", "--", f"origin/{branch}")


GONE_UPSTREAM = "[gone]"


def gone_merged_branches(repo: Path) -> list[str]:
    """Local branches whose upstream is gone and whose tip is in origin's default branch.

    Branches checked out in any worktree of the repo are never listed.
    """
    base = _origin_base(repo)
    if base is None:
        return []
    rows = _git(
        repo, "for-each-ref", "--format=%(refname:short)\t%(upstream:track)", "refs/heads"
    ).stdout
    checked_out = {tree.branch for tree in list_worktrees(repo)}
    gone = [
        name
        for name, _, track in (row.partition("\t") for row in rows.splitlines())
        if track.strip() == GONE_UPSTREAM and name not in checked_out
    ]
    return [
        name for name in gone if _is_ancestor(repo, f"refs/heads/{name}", f"refs/remotes/{base}")
    ]


def _output_lines(result: proc.Result) -> list[str]:
    """Non-empty stripped lines of stdout then stderr (git reports progress on stderr)."""
    return [line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()]


def prune_worktrees(repo: Path) -> list[str]:
    """`git worktree prune --verbose`: the lines git printed for the entries it removed."""
    return _output_lines(_git(repo, "worktree", "prune", "--verbose"))


def _origin_refs(repo: Path) -> set[str]:
    out = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").stdout
    return set(out.split())


def prune_remote_refs(repo: Path) -> list[str]:
    """`git fetch --prune origin`: the `origin/*` refs it removed, e.g. `origin/feat/x`, sorted.

    Empty when there is no origin or nothing was stale. Compares the refs before and after
    because git's own report is translated to the user's language.
    """
    if not _has_origin(repo):
        return []
    before = _origin_refs(repo)
    _fetch_checked(repo, "--prune")
    return sorted(before - _origin_refs(repo))
