import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from wt import claudegen
from wt.errors import WtError

AGENT = """---
name: checker
description: Runs the checks
skills: [lint]
---
Run the checks, then ask the `reviewer` agent.
"""
SKILL = """---
name: lint
description: Lints
---
Lint it.
"""
HEADER = "You work in ./{repo}/. Every path in your instructions is relative to that folder."
SKILL_HEADER = "This skill belongs to ./{repo}/. Every path in it is relative to that folder."
MANIFEST = ".claude/.wt-generated.json"
SKILLS = ".claude/skills/music-front-lint"


def make_repo(
    root: Path,
    name: str,
    *,
    agents: Mapping[str, str] | None = None,
    skills: Mapping[str, str] | None = None,
    settings: Mapping[str, Any] | None = None,
    claude_md: bool = True,
) -> Path:
    repo = root / name
    claude = repo / ".claude"
    claude.mkdir(parents=True)
    for stem, text in (agents or {}).items():
        (claude / "agents").mkdir(exist_ok=True)
        (claude / "agents" / f"{stem}.md").write_text(text)
    for skill, text in (skills or {}).items():
        (claude / "skills" / skill).mkdir(parents=True)
        (claude / "skills" / skill / "SKILL.md").write_text(text)
    if settings is not None:
        (claude / "settings.json").write_text(json.dumps(settings))
    if claude_md:
        (repo / "CLAUDE.md").write_text(f"# {name}\n")
    return repo


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, Path]:
    source = tmp_path / "src"
    front = make_repo(
        source,
        "music-front",
        agents={"checker": AGENT},
        skills={"lint": SKILL},
        settings={
            "permissions": {"deny": ["Read(./.env)"], "allow": ["Bash(npm test:*)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Edit", "hooks": [{"type": "command", "command": "./a.sh"}]}
                ]
            },
        },
    )
    back = make_repo(
        source,
        "music-back",
        agents={"builder": AGENT.replace("checker", "builder")},
        skills={"deploy": SKILL.replace("lint", "deploy")},
        settings={"permissions": {"deny": ["Read(./secret.txt)"]}},
        claude_md=False,
    )
    return {"music-front": front, "music-back": back}


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "wt" / "feat"
    path.mkdir(parents=True)
    return path


def manifest(folder: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((folder / MANIFEST).read_text())
    return loaded


def snapshot(folder: Path) -> dict[str, tuple[str, int]]:
    """{relative path: (content or link target, mtime_ns)} of every generated entry."""
    state: dict[str, tuple[str, int]] = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if path.is_symlink():
            state[rel] = (os.readlink(path), path.lstat().st_mtime_ns)
        elif path.is_file():
            state[rel] = (path.read_text(), path.stat().st_mtime_ns)
    return state


def test_sync_generates_prefixed_agents(folder: Path, repos: dict[str, Path]) -> None:
    result = claudegen.sync(folder, "feat", repos)

    assert result.changed is True
    assert result.repos == ["music-front", "music-back"]
    assert result.agents == ["music-front-checker", "music-back-builder"]
    assert result.skills == ["music-front-lint", "music-back-deploy"]
    text = (folder / ".claude/agents/music-front-checker.md").read_text()
    assert "name: music-front-checker" in text
    assert HEADER.format(repo="music-front") in text
    assert "skills: [music-front-lint]" in text
    assert (folder / ".claude/agents/music-back-builder.md").is_file()


def test_sync_writes_skill_folders_with_a_renamed_skill_md(
    folder: Path, repos: dict[str, Path]
) -> None:
    claudegen.sync(folder, "feat", repos)

    skill = folder / ".claude/skills/music-front-lint"
    assert skill.is_dir() and not skill.is_symlink()
    text = (skill / "SKILL.md").read_text()
    assert text.startswith("---\nname: music-front-lint\n")
    assert SKILL_HEADER.format(repo="music-front") in text
    assert "name: lint" in (repos["music-front"] / ".claude/skills/lint/SKILL.md").read_text()


def test_sync_links_a_skills_other_files_to_the_absolute_repo_paths(
    folder: Path, repos: dict[str, Path]
) -> None:
    source = repos["music-front"] / ".claude/skills/lint"
    (source / "reference.md").write_text("ref")
    (source / "scripts").mkdir()

    claudegen.sync(folder, "feat", repos)

    skill = folder / ".claude/skills/music-front-lint"
    for name in ("reference.md", "scripts"):
        assert (skill / name).is_symlink()
        assert os.readlink(skill / name) == str((source / name).absolute())


def test_sync_merges_settings_and_wraps_hooks(folder: Path, repos: dict[str, Path]) -> None:
    claudegen.sync(folder, "feat", repos)

    settings = json.loads((folder / ".claude/settings.json").read_text())
    assert settings["permissions"]["deny"] == [
        "Read(./music-front/.env)",
        "Read(./music-back/secret.txt)",
    ]
    assert settings["permissions"]["allow"] == ["Bash(npm test:*)"]
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command.startswith('bash "$CLAUDE_PROJECT_DIR/.claude/wt-hook.sh" music-front ')
    assert command.endswith("./a.sh")
    assert (folder / ".claude/settings.json").read_text().endswith("}\n")


def test_sync_writes_an_executable_hook_script(folder: Path, repos: dict[str, Path]) -> None:
    claudegen.sync(folder, "feat", repos)

    script = folder / ".claude/wt-hook.sh"
    assert script.read_text().startswith("#!")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


def test_sync_writes_claude_md_importing_only_repos_that_have_one(
    folder: Path, repos: dict[str, Path]
) -> None:
    claudegen.sync(folder, "feat", repos)

    lines = (folder / "CLAUDE.md").read_text().splitlines()
    assert lines[0] == "<!-- Generated by wt. Do not edit; run wt claude sync feat. -->"
    assert "@music-front/CLAUDE.md" in lines
    assert "@music-back/CLAUDE.md" not in lines
    assert "music-front/.claude/plans/<task>/" in "\n".join(lines)


def test_sync_honors_plan_repo(folder: Path, repos: dict[str, Path]) -> None:
    claudegen.sync(folder, "feat", repos, plan_repo="music-back")

    assert "music-back/.claude/plans/<task>/" in (folder / "CLAUDE.md").read_text()


def test_sync_rejects_an_unknown_plan_repo(folder: Path, repos: dict[str, Path]) -> None:
    with pytest.raises(WtError, match="nope"):
        claudegen.sync(folder, "feat", repos, plan_repo="nope")
    assert not (folder / ".claude").exists()


def test_sync_writes_the_manifest(folder: Path, repos: dict[str, Path]) -> None:
    (repos["music-front"] / ".claude/skills/lint/extra.txt").write_text("extra")
    claudegen.sync(folder, "feat", repos)

    data = manifest(folder)
    assert data["version"] == 1
    assert data["wt"] == "feat"
    assert data["repos"] == ["music-front", "music-back"]
    assert data["conflicts"] == []
    assert data["notes"] == []
    files = data["files"]
    assert MANIFEST not in files
    assert set(files) == {
        ".claude/agents/music-front-checker.md",
        ".claude/agents/music-back-builder.md",
        ".claude/skills/music-front-lint/SKILL.md",
        ".claude/skills/music-back-deploy/SKILL.md",
        ".claude/skills/music-front-lint/extra.txt",
        ".claude/settings.json",
        ".claude/wt-hook.sh",
        "CLAUDE.md",
    }
    agent = files[".claude/agents/music-front-checker.md"]
    assert agent["kind"] == "copy"
    assert agent["source"] == "music-front/.claude/agents/checker.md"
    agent_bytes = (folder / ".claude/agents/music-front-checker.md").read_bytes()
    assert agent["sha256"] == hashlib.sha256(agent_bytes).hexdigest()
    skill_md = files[".claude/skills/music-front-lint/SKILL.md"]
    assert skill_md["kind"] == "copy"
    assert skill_md["source"] == "music-front/.claude/skills/lint/SKILL.md"
    link = files[".claude/skills/music-front-lint/extra.txt"]
    target = str((repos["music-front"] / ".claude/skills/lint/extra.txt").absolute())
    assert link == {
        "kind": "link",
        "sha256": hashlib.sha256(target.encode()).hexdigest(),
        "source": "music-front/.claude/skills/lint/extra.txt",
    }
    assert files["CLAUDE.md"]["kind"] == "file"


def test_second_sync_changes_nothing(folder: Path, repos: dict[str, Path]) -> None:
    claudegen.sync(folder, "feat", repos)
    before = snapshot(folder)

    result = claudegen.sync(folder, "feat", repos)

    assert result.changed is False
    assert snapshot(folder) == before


def test_sync_updates_a_changed_source_agent(folder: Path, repos: dict[str, Path]) -> None:
    claudegen.sync(folder, "feat", repos)
    agent = repos["music-front"] / ".claude/agents/checker.md"
    agent.write_text(AGENT + "More.\n")

    result = claudegen.sync(folder, "feat", repos)

    assert result.changed is True
    assert "More." in (folder / ".claude/agents/music-front-checker.md").read_text()


def test_sync_removes_what_a_repo_that_left_the_wt_produced(
    folder: Path, tmp_path: Path, repos: dict[str, Path]
) -> None:
    third = make_repo(tmp_path / "src", "music-api", agents={"checker": AGENT}, skills={"s": SKILL})
    claudegen.sync(folder, "feat", {**repos, "music-api": third})
    assert (folder / ".claude/agents/music-api-checker.md").is_file()

    result = claudegen.sync(folder, "feat", repos)

    assert result.changed is True
    assert not (folder / ".claude/agents/music-api-checker.md").exists()
    assert not (folder / ".claude/skills/music-api-s").is_symlink()
    assert "music-api" not in manifest(folder)["repos"]
    assert "@music-api" not in (folder / "CLAUDE.md").read_text()


def test_sync_replaces_a_hand_edited_file_and_says_so(folder: Path, repos: dict[str, Path]) -> None:
    claudegen.sync(folder, "feat", repos)
    generated = folder / ".claude/agents/music-front-checker.md"
    original = generated.read_text()
    generated.write_text("my own notes\n")

    result = claudegen.sync(folder, "feat", repos)

    assert result.changed is True
    assert generated.read_text() == original
    assert ".claude/agents/music-front-checker.md was edited by hand; replaced" in result.notes
    assert claudegen.sync(folder, "feat", repos).changed is False


def test_sync_recreates_a_deleted_generated_file(folder: Path, repos: dict[str, Path]) -> None:
    claudegen.sync(folder, "feat", repos)
    (folder / "CLAUDE.md").unlink()

    result = claudegen.sync(folder, "feat", repos)

    assert result.changed is True
    assert (folder / "CLAUDE.md").is_file()
    assert result.notes == []


def test_sync_points_a_moved_skill_link_at_the_new_folder(
    folder: Path, tmp_path: Path, repos: dict[str, Path]
) -> None:
    (repos["music-front"] / ".claude/skills/lint/extra.txt").write_text("extra")
    claudegen.sync(folder, "feat", repos)
    moved = make_repo(
        tmp_path / "moved", "music-front", agents={"checker": AGENT}, skills={"lint": SKILL}
    )
    (moved / ".claude/skills/lint/extra.txt").write_text("extra")

    result = claudegen.sync(folder, "feat", {**repos, "music-front": moved})

    assert result.changed is True
    link = folder / ".claude/skills/music-front-lint/extra.txt"
    assert os.readlink(link) == str((moved / ".claude/skills/lint/extra.txt").absolute())


def test_sync_skips_a_skill_folder_without_skill_md(folder: Path, repos: dict[str, Path]) -> None:
    (repos["music-back"] / ".claude/skills/empty").mkdir()

    result = claudegen.sync(folder, "feat", repos)

    assert not (folder / ".claude/skills/music-back-empty").exists()
    assert result.notes == ["music-back: skill empty has no SKILL.md; skipped"]
    assert manifest(folder)["notes"] == result.notes


def test_sync_reports_settings_conflicts(folder: Path, repos: dict[str, Path]) -> None:
    (repos["music-front"] / ".claude/settings.json").write_text(json.dumps({"model": "opus"}))
    (repos["music-back"] / ".claude/settings.json").write_text(json.dumps({"model": "haiku"}))

    result = claudegen.sync(folder, "feat", repos)

    assert [conflict.key for conflict in result.conflicts] == ["model"]
    assert manifest(folder)["conflicts"] == [
        {"key": "model", "repos": ["music-front", "music-back"]}
    ]
    assert "model" not in json.loads((folder / ".claude/settings.json").read_text())


def test_sync_needs_two_repos(folder: Path, repos: dict[str, Path]) -> None:
    with pytest.raises(WtError, match="feat has fewer than two repos; start Claude inside"):
        claudegen.sync(folder, "feat", {"music-front": repos["music-front"]})
    assert list(folder.iterdir()) == []


def test_sync_refuses_a_claude_folder_it_did_not_generate(
    folder: Path, repos: dict[str, Path]
) -> None:
    (folder / ".claude").mkdir()
    (folder / ".claude/mine.md").write_text("keep")

    with pytest.raises(WtError, match="was not generated by wt; move it away or set claude: false"):
        claudegen.sync(folder, "feat", repos)

    assert [path.name for path in (folder / ".claude").iterdir()] == ["mine.md"]
    assert not (folder / "CLAUDE.md").exists()


def test_sync_refuses_a_claude_md_it_did_not_generate(folder: Path, repos: dict[str, Path]) -> None:
    (folder / "CLAUDE.md").write_text("mine")

    with pytest.raises(WtError, match=r"CLAUDE\.md exists and was not generated by wt"):
        claudegen.sync(folder, "feat", repos)

    assert (folder / "CLAUDE.md").read_text() == "mine"
    assert not (folder / ".claude").exists()


def test_sync_refuses_the_folder_of_another_wt(
    folder: Path, tmp_path: Path, repos: dict[str, Path]
) -> None:
    claudegen.sync(folder, "other", repos)
    before = snapshot(folder)

    with pytest.raises(WtError, match="was not generated by wt"):
        claudegen.sync(folder, "feat", repos)

    assert snapshot(folder) == before


def test_sync_refuses_to_overwrite_a_foreign_file_inside_a_generated_claude(
    folder: Path, repos: dict[str, Path]
) -> None:
    claudegen.sync(folder, "feat", repos)
    manifest_path = folder / MANIFEST
    data = manifest(folder)
    del data["files"][".claude/agents/music-front-checker.md"]
    manifest_path.write_text(json.dumps(data))
    (folder / ".claude/agents/music-front-checker.md").write_text("mine")

    with pytest.raises(WtError, match=r"music-front-checker\.md exists and was not generated"):
        claudegen.sync(folder, "feat", repos)

    assert (folder / ".claude/agents/music-front-checker.md").read_text() == "mine"


def test_sync_names_a_corrupt_repo_settings_file(folder: Path, repos: dict[str, Path]) -> None:
    broken = repos["music-back"] / ".claude/settings.json"
    broken.write_text("{not json")

    with pytest.raises(WtError, match=str(broken)):
        claudegen.sync(folder, "feat", repos)

    assert not (folder / ".claude").exists()


def test_sync_names_a_corrupt_manifest(folder: Path, repos: dict[str, Path]) -> None:
    claudegen.sync(folder, "feat", repos)
    (folder / MANIFEST).write_text("{")

    with pytest.raises(WtError, match=r"\.wt-generated\.json is corrupt"):
        claudegen.sync(folder, "feat", repos)


def test_sync_rejects_a_manifest_path_that_leaves_the_folder(
    folder: Path, tmp_path: Path, repos: dict[str, Path]
) -> None:
    claudegen.sync(folder, "feat", repos)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    data = manifest(folder)
    data["files"]["../../victim.txt"] = {"kind": "file", "sha256": "x", "source": ""}
    (folder / MANIFEST).write_text(json.dumps(data))

    with pytest.raises(WtError, match="unsafe path"):
        claudegen.sync(folder, "feat", repos)

    assert victim.read_text() == "keep"


def test_sync_copies_a_skill_file_when_symlinks_fail(
    monkeypatch: pytest.MonkeyPatch, folder: Path, repos: dict[str, Path]
) -> None:
    (repos["music-front"] / ".claude/skills/lint/extra.txt").write_text("extra")

    def refuse(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(os, "symlink", refuse)

    result = claudegen.sync(folder, "feat", repos)

    skill = folder / ".claude/skills/music-front-lint"
    assert skill.is_dir() and not skill.is_symlink()
    assert "name: music-front-lint" in (skill / "SKILL.md").read_text()
    assert (skill / "extra.txt").read_text() == "extra"
    assert (skill / "extra.txt").is_file() and not (skill / "extra.txt").is_symlink()
    assert result.notes == [
        ".claude/skills/music-front-lint/extra.txt: symlinks are not available here; copied it"
    ]
    assert manifest(folder)["notes"] == result.notes
    assert manifest(folder)["files"][".claude/skills/music-front-lint/extra.txt"]["kind"] == "copy"
    # the original skill is untouched
    assert "name: lint" in (repos["music-front"] / ".claude/skills/lint/SKILL.md").read_text()


def test_sync_with_failing_symlinks_is_still_idempotent_and_upgrades_to_links(
    monkeypatch: pytest.MonkeyPatch, folder: Path, repos: dict[str, Path]
) -> None:
    (repos["music-front"] / ".claude/skills/lint/extra.txt").write_text("extra")
    with monkeypatch.context() as patch:

        def refuse(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            raise OSError(1, "no")

        patch.setattr(os, "symlink", refuse)
        claudegen.sync(folder, "feat", repos)
        before = snapshot(folder)
        assert claudegen.sync(folder, "feat", repos).changed is False
        assert snapshot(folder) == before

    result = claudegen.sync(folder, "feat", repos)

    assert result.changed is True
    assert (folder / ".claude/skills/music-front-lint/extra.txt").is_symlink()
    assert manifest(folder)["files"][".claude/skills/music-front-lint/extra.txt"]["kind"] == "link"
    assert (repos["music-front"] / ".claude/skills/lint/extra.txt").read_text() == "extra"
    assert result.notes == []


def test_sync_replaces_a_v0_13_0_skill_folder_link_without_touching_the_repo(
    folder: Path, repos: dict[str, Path]
) -> None:
    source = repos["music-front"] / ".claude/skills/lint"
    claudegen.sync(folder, "feat", repos)
    # the v0.13.0 layout: the whole skill folder is one link, listed in the manifest
    skill = folder / ".claude/skills/music-front-lint"
    for item in skill.iterdir():
        item.unlink()
    skill.rmdir()
    skill.symlink_to(source.absolute())
    data = manifest(folder)
    data["files"] = {k: v for k, v in data["files"].items() if not k.startswith(f"{SKILLS}/")}
    data["files"][".claude/skills/music-front-lint"] = {"kind": "link", "sha256": "x", "source": ""}
    (folder / MANIFEST).write_text(json.dumps(data))

    result = claudegen.sync(folder, "feat", repos)

    assert result.changed is True
    assert skill.is_dir() and not skill.is_symlink()
    assert "name: music-front-lint" in (skill / "SKILL.md").read_text()
    assert sorted(path.name for path in source.iterdir()) == ["SKILL.md"]
    assert (source / "SKILL.md").read_text() == SKILL
    assert ".claude/skills/music-front-lint" not in manifest(folder)["files"]
    assert claudegen.sync(folder, "feat", repos).changed is False


def test_remove_deletes_exactly_the_generated_files(folder: Path, repos: dict[str, Path]) -> None:
    claudegen.sync(folder, "feat", repos)
    (folder / ".claude/mine.md").write_text("keep")
    (folder / "music-front").mkdir()
    (folder / "music-front/file.txt").write_text("repo")

    removed = claudegen.remove(folder)

    assert sorted(removed) == sorted(
        [
            ".claude/agents/music-front-checker.md",
            ".claude/agents/music-back-builder.md",
            ".claude/skills/music-front-lint/SKILL.md",
            ".claude/skills/music-back-deploy/SKILL.md",
            ".claude/settings.json",
            ".claude/wt-hook.sh",
            "CLAUDE.md",
        ]
    )
    assert sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*")) == [
        ".claude",
        ".claude/mine.md",
        "music-front",
        "music-front/file.txt",
    ]
    assert (repos["music-front"] / ".claude/skills/lint/SKILL.md").is_file()


def test_remove_leaves_no_trace_when_everything_is_generated(
    folder: Path, repos: dict[str, Path]
) -> None:
    claudegen.sync(folder, "feat", repos)

    claudegen.remove(folder)

    assert list(folder.iterdir()) == []


def test_remove_without_a_manifest_deletes_nothing(folder: Path) -> None:
    (folder / ".claude").mkdir()
    (folder / ".claude/mine.md").write_text("keep")
    (folder / "CLAUDE.md").write_text("mine")

    assert claudegen.remove(folder) == []

    assert (folder / ".claude/mine.md").read_text() == "keep"
    assert (folder / "CLAUDE.md").read_text() == "mine"


def test_remove_deletes_a_link_not_its_target(
    folder: Path, tmp_path: Path, repos: dict[str, Path]
) -> None:
    (repos["music-front"] / ".claude/skills/lint/extra.txt").write_text("extra")
    claudegen.sync(folder, "feat", repos)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("keep")
    link = folder / ".claude/skills/music-front-lint/extra.txt"
    link.unlink()
    link.symlink_to(outside)

    claudegen.remove(folder)

    assert (outside / "file.txt").read_text() == "keep"
    assert not link.is_symlink()
    assert (repos["music-front"] / ".claude/skills/lint/extra.txt").read_text() == "extra"


def test_remove_rejects_a_manifest_path_that_leaves_the_folder(
    folder: Path, tmp_path: Path, repos: dict[str, Path]
) -> None:
    claudegen.sync(folder, "feat", repos)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    data = manifest(folder)
    data["files"]["../../victim.txt"] = {"kind": "file", "sha256": "x", "source": ""}
    (folder / MANIFEST).write_text(json.dumps(data))

    with pytest.raises(WtError, match="unsafe path"):
        claudegen.remove(folder)

    assert victim.read_text() == "keep"
