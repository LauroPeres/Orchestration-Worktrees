"""v0.15.0 (SPEC section 35): `remove` leaves nothing behind and `wt prune` cleans orphans.

Drives the real CLI against a real Docker daemon. Every test has its own WT_HOME, WT_INSTANCE,
catalog and free ports, so nothing here touches a real stack. `pytest -m docker`.
"""

import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from wt import compose, router
from wt.naming import Names
from wt.paths import project_dir
from wt.providers import postgres

from .isolation import reserve_free_indexes
from .test_v061 import Wt, docker, free_port, git

pytestmark = pytest.mark.docker

BOOTSTRAP = Path(__file__).parents[2] / "examples" / "notes" / "bootstrap.sh"
SAFE_PATH = "/usr/bin:/bin:/usr/local/bin"  # no mkcert: the self-signed path
UP_TIMEOUT = "180"
PROJECT_LABEL = "com.docker.compose.project"
GHOST_DOCKERFILE = "FROM scratch\nLABEL wt.test=ghost\n"
GHOST_COMPOSE = """\
services:
  ghost:
    build: .
    command: ["/nothing"]
    volumes:
      - data:/data
volumes:
  data: {}
"""


def _lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return result.stdout.split()


def _json(wt: Wt, *args: str) -> dict[str, Any]:
    """The JSON document of a command: the last stdout line (compose build output precedes it)."""
    result = wt.run(*args, "--json")
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    document: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return document


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _leftovers(project: str) -> dict[str, list[str]]:
    """Every Docker object labelled with the compose project."""
    label = f"label={PROJECT_LABEL}={project}"
    return {
        "containers": _lines(docker("ps", "-a", "-q", "--filter", label)),
        "volumes": _lines(docker("volume", "ls", "-q", "--filter", label)),
        "images": _lines(docker("image", "ls", "-q", "--filter", label)),
    }


def _git_env_run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _commit(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(message)
    _git_env_run(repo, "add", ".")
    _git_env_run(repo, "commit", "-qm", message)


def _add_origin(repo: Path, bare: Path) -> None:
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    _git_env_run(repo, "remote", "add", "origin", str(bare))
    _git_env_run(repo, "push", "-q", "origin", "main")
    _git_env_run(repo, "fetch", "-q", "origin")


def _isolate(instance: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Wt:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.skip("docker is not available")
    wt_home = Path(os.environ["WT_HOME"])
    catalog = tmp_path / "catalog.yaml"
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_CATALOG", str(catalog))
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("WT_POSTGRES_PORT", str(free_port()))
    monkeypatch.setenv("PATH", SAFE_PATH)
    reserve_free_indexes(wt_home)
    return Wt(instance, wt_home, catalog)


def _teardown(wt: Wt) -> None:
    with contextlib.suppress(Exception):
        for row in wt.ls()["branches"]:
            wt.run("down", row["branch"], "--force")
    docker("rm", "-f", "-v", router.container(), postgres.container())
    docker("volume", "rm", "-f", postgres.volume())
    docker("network", "rm", compose.network_name())
    # Every object of this run carries the unique instance in its compose project name.
    projects = compose.wt_projects(f"{wt.instance}-")
    for project in projects:
        label = f"label={PROJECT_LABEL}={project}"
        containers = _lines(docker("ps", "-a", "-q", "--filter", label))
        if containers:
            docker("rm", "-f", "-v", *containers)
        volumes = _lines(docker("volume", "ls", "-q", "--filter", label))
        if volumes:
            docker("volume", "rm", "-f", *volumes)
    images = _lines(docker("image", "ls", "-q", "--filter", f"reference=*{wt.instance}*"))
    if images:
        docker("rmi", "-f", *images)
    for project in projects:
        images = _lines(docker("image", "ls", "-q", "--filter", f"label={PROJECT_LABEL}={project}"))
        if images:
            docker("rmi", "-f", *images)


@pytest.fixture
def notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Wt]:
    """examples/notes with a unique project name; notes-back has a bare origin."""
    wt = _isolate(f"wtv015{free_port()}", tmp_path, monkeypatch)
    stack = tmp_path / "stack"
    subprocess.run([str(BOOTSTRAP), str(stack)], check=True, capture_output=True)
    config = stack / "catalog.yaml"
    config.write_text(config.read_text().replace("project: notes\n", f"project: {wt.instance}\n"))
    wt.catalog.write_text(config.read_text().replace("./notes-", f"{stack}/notes-"))
    _add_origin(stack / "notes-back", tmp_path / "origin.git")
    wt.stack = stack  # type: ignore[attr-defined]
    yield wt
    _teardown(wt)


@pytest.fixture
def plain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Wt]:
    """A one-repo project with a host-process service; nothing is brought up."""
    wt = _isolate(f"wtv015{free_port()}", tmp_path, monkeypatch)
    repo = tmp_path / "app"
    repo.mkdir()
    _git_env_run(repo, "init", "-q", "-b", "main") if False else git(
        repo, "init", "-q", "-b", "main"
    )
    _commit(repo, "README.md", "init")
    wt.write_catalog({"app": repo}, {"app": {"repo": "app", "run": "sleep 600"}})
    yield wt
    _teardown(wt)


def _repo(wt: Wt) -> Path:
    document: dict[str, Any] = yaml.safe_load(wt.catalog.read_text())
    return Path(document["repos"]["app"])


def _branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").split()


def _ghost(project: str, tmp_path: Path) -> None:
    """A compose project nobody owns: a created container, a volume and a built image.

    Built by Compose itself, then its files are deleted, like a branch whose worktree is gone.
    """
    folder = tmp_path / "ghost"
    folder.mkdir()
    (folder / "Dockerfile").write_text(GHOST_DOCKERFILE)
    (folder / "compose.yml").write_text(GHOST_COMPOSE)
    made = docker("compose", "-p", project, "-f", str(folder / "compose.yml"), "up", "--no-start")
    assert made.returncode == 0, made.stderr
    shutil.rmtree(folder)


# ---- 1. remove leaves nothing behind -------------------------------------------------------


def test_remove_leaves_no_container_volume_image_route_cert_branch_or_state(notes: Wt) -> None:
    stack: Path = notes.stack  # type: ignore[attr-defined]
    branch, slug = "feat/clean", "feat-clean"
    up = _json(notes, "up", branch, "--uses", "front", "--timeout", UP_TIMEOUT)
    assert up["status"] == "up", up
    names = Names(notes.instance, branch, f"{notes.instance}.localhost")
    projects = [names.compose_project("notes-back")]
    key = names.key
    state_dir = project_dir(notes.instance) / "branches" / slug

    # Prove the test looks at real things before it asserts they are gone.
    before = _leftovers(projects[0])
    assert before["containers"] and before["images"], before
    routes = [p for p in router.dynamic_dir().iterdir() if p.name.startswith(key)]
    certs = [p for p in router.certs_dir().iterdir() if p.name.startswith(key)]
    assert routes, list(router.dynamic_dir().iterdir())
    assert state_dir.is_dir()
    assert branch in _branches(stack / "notes-back")

    removed = _json(notes, "remove", branch, "--yes")

    assert removed["action"] == "remove"
    for project in projects:
        assert _leftovers(project) == {"containers": [], "volumes": [], "images": []}
    assert not any(p.exists() for p in [*routes, *certs])
    assert not state_dir.exists()
    assert branch not in _branches(stack / "notes-back")
    assert branch not in _branches(stack / "notes-front")
    assert not Path(up["folder"]).exists(), up["folder"]
    assert notes.ls()["branches"] == []
    # The branch had no remote copy: kept, with a note, and remove still succeeded.
    assert [r["deleted"] for r in removed["remote"]] == [False] * len(removed["remote"])
    assert compose.wt_projects(f"{notes.instance}-{notes.instance}-") == set()


# ---- 2. remote branch ----------------------------------------------------------------------


def _remote_branch(repo: Path, branch: str, *, extra_commit: bool) -> None:
    _git_env_run(repo, "branch", branch)
    if extra_commit:
        _git_env_run(repo, "checkout", "-q", branch)
        _commit(repo, "extra.txt", "ahead of main")
        _git_env_run(repo, "checkout", "-q", "main")
    _git_env_run(repo, "push", "-q", "origin", branch)


def _remote_branches(bare: Path) -> list[str]:
    return git(bare, "branch", "--format=%(refname:short)").split()


def test_remove_deletes_the_merged_remote_branch_and_its_ref(plain: Wt, tmp_path: Path) -> None:
    repo, bare = _repo(plain), tmp_path / "origin.git"
    _add_origin(repo, bare)
    _remote_branch(repo, "feat/merged", extra_commit=False)

    result = plain.run("remove", "feat/merged", "--yes")

    assert result.returncode == 0, _output(result)
    assert "deleted remote branch feat/merged in app" in _output(result)
    assert _remote_branches(bare) == ["main"]
    assert "origin/feat/merged" not in git(repo, "branch", "-r")
    assert "feat/merged" not in _branches(repo)


def test_remove_keeps_an_unmerged_remote_branch_with_a_note(plain: Wt, tmp_path: Path) -> None:
    repo, bare = _repo(plain), tmp_path / "origin.git"
    _add_origin(repo, bare)
    _remote_branch(repo, "feat/ahead", extra_commit=True)

    document = _json(plain, "remove", "feat/ahead", "--yes")

    [remote] = document["remote"]
    assert (remote["repo"], remote["branch"], remote["deleted"]) == ("app", "feat/ahead", False)
    assert remote["reason"].startswith("not merged")
    assert "feat/ahead" in _remote_branches(bare)
    assert "feat/ahead" not in _branches(repo)
    text = _output(plain.run("remove", "feat/ahead", "--yes"))
    assert "nothing to remove" in text or "feat/ahead" in text


def test_remove_text_note_for_an_unmerged_remote_branch(plain: Wt, tmp_path: Path) -> None:
    repo, bare = _repo(plain), tmp_path / "origin.git"
    _add_origin(repo, bare)
    _remote_branch(repo, "feat/ahead", extra_commit=True)

    result = plain.run("remove", "feat/ahead", "--yes")

    assert result.returncode == 0, _output(result)
    assert "kept remote branch feat/ahead in app: not merged" in _output(result)


def test_remove_without_origin_notes_no_origin_and_succeeds(plain: Wt) -> None:
    repo = _repo(plain)
    _git_env_run(repo, "branch", "feat/local")

    result = plain.run("remove", "feat/local", "--yes")

    assert result.returncode == 0, _output(result)
    assert "kept remote branch feat/local in app: no origin" in _output(result)
    assert "feat/local" not in _branches(repo)


# ---- 3. orphans ----------------------------------------------------------------------------


def test_prune_lists_then_removes_orphans_and_keeps_the_live_branch(
    notes: Wt, tmp_path: Path
) -> None:
    stack: Path = notes.stack  # type: ignore[attr-defined]
    back, bare = stack / "notes-back", tmp_path / "origin.git"
    live = _json(notes, "up", "feat/live", "--uses", "front", "--timeout", UP_TIMEOUT)
    assert live["status"] == "up", live
    live_project = Names(notes.instance, "feat/live", "x").compose_project("notes-back")
    live_containers = _leftovers(live_project)["containers"]
    assert live_containers

    # A branch whose upstream is gone and whose tip is in origin/main (fetched with prune).
    _remote_branch(back, "feat/gone", extra_commit=False)
    _git_env_run(back, "branch", "--set-upstream-to=origin/feat/gone", "feat/gone")
    _git_env_run(bare, "branch", "-D", "feat/gone")
    _git_env_run(back, "fetch", "-q", "--prune", "origin")
    # A second branch deleted on origin but not fetched yet: a stale origin ref.
    _remote_branch(back, "feat/stale", extra_commit=False)
    _git_env_run(bare, "branch", "-D", "feat/stale")
    assert "feat/gone" in _branches(back)

    ghost = f"{notes.instance}-{notes.instance}-ghost"
    _ghost(ghost, tmp_path)
    assert _leftovers(ghost)["containers"] and _leftovers(ghost)["volumes"]
    assert _leftovers(ghost)["images"]

    preview = _json(notes, "prune", "--dry-run")
    items = {(i["kind"], i["target"]): i for i in preview["items"]}
    assert preview["dry_run"] is True
    assert ("branch", "feat/gone") in items
    assert ("compose", ghost) in items
    assert not any(i["done"] for i in preview["items"]), preview
    assert not any(i["target"] == "feat/live" for i in preview["items"])
    assert "feat/gone" in _branches(back)
    assert _leftovers(ghost)["containers"]

    done = _json(notes, "prune", "--yes")
    done_items = {(i["kind"], i["target"]): i for i in done["items"]}
    assert done["dry_run"] is False
    assert done_items[("branch", "feat/gone")]["done"] is True
    assert done_items[("compose", ghost)]["done"] is True
    assert ("remote_ref", "origin/feat/stale") in done_items
    assert all(i["done"] for i in done["items"]), [i for i in done["items"] if not i["done"]]
    assert "feat/gone" not in _branches(back)
    assert _leftovers(ghost) == {"containers": [], "volumes": [], "images": []}
    assert "feat/live" in _branches(back)
    assert set(_leftovers(live_project)["containers"]) == set(live_containers)
    assert notes.branch("feat/live")["status"] == "up"


# ---- 4. adversarial ------------------------------------------------------------------------


def test_prune_with_nothing_to_do_says_so(plain: Wt) -> None:
    result = plain.run("prune", "--yes")

    assert result.returncode == 0, _output(result)
    assert "nothing to prune" in result.stdout


def test_prune_json_alone_is_an_error_document(plain: Wt) -> None:
    result = plain.run("prune", "--json")

    assert result.returncode == 1, (result.stdout, result.stderr)
    document = json.loads(result.stdout)
    assert "prune --json never asks for confirmation: pass --yes" in json.dumps(document)
    assert "Traceback" not in result.stderr


def test_prune_dry_run_json_with_nothing_to_do_has_no_items(plain: Wt) -> None:
    document = _json(plain, "prune", "--dry-run")

    assert document["action"] == "prune"
    assert document["items"] == []


def test_remove_keeps_a_wt_folder_that_holds_a_plan(notes: Wt) -> None:
    up = _json(notes, "up", "feat/plan", "--uses", "back", "--timeout", UP_TIMEOUT)
    folder = Path(up["folder"])
    (folder / "plan.md").write_text("# keep me\n")

    result = notes.run("remove", "feat/plan", "--yes")

    assert result.returncode == 0, _output(result)
    assert (folder / "plan.md").read_text() == "# keep me\n"
    assert f"kept {folder}: not empty (plan.md)" in " ".join(_output(result).split())
    shutil.rmtree(folder)
