import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from wt.claudemerge import (
    HOOK_SCRIPT,
    HOOK_SCRIPT_NAME,
    Conflict,
    RepoClaude,
    merge_agent,
    merge_settings,
    prefixed,
    render_claude_md,
)

WORK_LINE = "You work in ./music-back/. Every path in your instructions is relative to that folder."


def repo(name: str, settings: dict[str, Any] | None = None) -> RepoClaude:
    return RepoClaude(repo=name, agents={}, skills=(), settings=settings or {}, has_claude_md=False)


def wrapped(repo_name: str, command: str) -> str:
    import shlex

    return f'bash "$CLAUDE_PROJECT_DIR/.claude/wt-hook.sh" {repo_name} {shlex.quote(command)}'


# --- prefixed ---
def test_prefixed_joins_repo_and_name() -> None:
    assert prefixed("music-back", "checker") == "music-back-checker"


# --- merge_agent ---
AGENT = """---
name: checker
description: Runs the checks. Use the `linter` agent for style.
tools: Read, Bash
---

Run the tests, then call the `linter` agent and read the `clean-code` skill.
"""


def test_agent_name_is_prefixed_and_work_line_follows_the_frontmatter() -> None:
    out = merge_agent("music-back", "checker", AGENT, ["checker", "linter"], ["clean-code"])
    lines = out.splitlines()
    assert lines[:2] == ["---", "name: music-back-checker"]
    assert lines[4] == "---"
    assert lines[5] == WORK_LINE
    assert lines[6] == ""


def test_agent_own_frontmatter_name_wins_over_the_stem() -> None:
    text = AGENT.replace("name: checker", "name: the-checker")
    out = merge_agent("music-back", "checker", text, ["checker"], [])
    assert "name: music-back-the-checker\n" in out


def test_agent_without_a_name_uses_the_stem() -> None:
    text = "---\ndescription: x\n---\n\nBody\n"
    out = merge_agent("music-back", "checker", text, ["checker"], [])
    assert out.startswith("---\nname: music-back-checker\ndescription: x\n---\n")


def test_agent_without_frontmatter_gets_a_minimal_one() -> None:
    out = merge_agent("music-back", "checker", "Just text.\n", ["checker"], [])
    assert out == f"---\nname: music-back-checker\n---\n{WORK_LINE}\n\nJust text.\n"


def test_quoted_name_is_unquoted_before_prefixing() -> None:
    out = merge_agent("music-back", "c", '---\nname: "checker"\n---\nBody\n', ["c"], [])
    assert "name: music-back-checker\n" in out


def test_backticked_agent_and_skill_names_are_rewritten() -> None:
    out = merge_agent("music-back", "checker", AGENT, ["checker", "linter"], ["clean-code"])
    assert "call the `music-back-linter` agent" in out
    assert "the `music-back-clean-code` skill" in out
    assert "Use the `music-back-linter` agent for style." in out  # frontmatter description too


def test_plain_prose_words_are_not_rewritten() -> None:
    text = "---\nname: a\n---\nThe checker agent and the linter run clean-code things.\n"
    out = merge_agent("music-back", "a", text, ["checker", "linter"], ["clean-code"])
    assert out.endswith("The checker agent and the linter run clean-code things.\n")


def test_only_whole_names_inside_backticks_are_rewritten() -> None:
    text = (
        "---\nname: a\n---\n"
        "`checker-extra` `my-checker` `src/checker/x.py` `checker.md` `code-framework:checker`"
        " `@checker`\n"
    )
    out = merge_agent("music-back", "a", text, ["checker"], [])
    assert out.endswith(
        "`checker-extra` `my-checker` `src/checker/x.py` `checker.md` "
        "`code-framework:checker` `@checker`\n"
    )


def test_names_in_a_backtick_span_with_other_words_and_slash_commands() -> None:
    text = "---\nname: a\n---\nRun `/clean-code now`, then `Task(checker)`.\n"
    out = merge_agent("music-back", "a", text, ["checker"], ["clean-code"])
    assert out.endswith("Run `/music-back-clean-code now`, then `Task(music-back-checker)`.\n")


def test_already_prefixed_names_are_left_alone() -> None:
    text = "---\nname: a\n---\nUse `music-back-checker` or `checker`.\n"
    out = merge_agent("music-back", "a", text, ["checker"], [])
    assert out.endswith("Use `music-back-checker` or `music-back-checker`.\n")


def test_fenced_code_blocks_are_not_rewritten() -> None:
    text = "---\nname: a\n---\n```\nrun `checker`\n```\nthen `checker`\n"
    out = merge_agent("music-back", "a", text, ["checker"], [])
    assert out.endswith("```\nrun `checker`\n```\nthen `music-back-checker`\n")


@pytest.mark.parametrize(
    ("skills_form", "expected"),
    [
        (
            "skills: [clean-code, plug:other, 'checks']",
            "skills: [music-back-clean-code, plug:other, 'music-back-checks']",
        ),
        ("skills: clean-code, checks", "skills: music-back-clean-code, music-back-checks"),
        (
            'skills:\n  - clean-code\n  - "checks"\n  - plug:other',
            'skills:\n  - music-back-clean-code\n  - "music-back-checks"\n  - plug:other',
        ),
    ],
)
def test_skills_lists_are_rewritten_in_every_form(skills_form: str, expected: str) -> None:
    text = f"---\nname: a\n{skills_form}\nmodel: sonnet\n---\nBody\n"
    out = merge_agent("music-back", "a", text, [], ["clean-code", "checks"])
    assert f"name: music-back-a\n{expected}\nmodel: sonnet\n---\n" in out


def test_block_skills_list_ends_at_the_next_key() -> None:
    text = "---\nname: a\nskills:\n  - checks\ntools:\n  - checks\n---\nBody\n"
    out = merge_agent("music-back", "a", text, [], ["checks"])
    assert "skills:\n  - music-back-checks\ntools:\n  - checks\n" in out


def test_agent_key_value_is_rewritten_only_for_this_repos_agents() -> None:
    text = "---\nname: a\nagent: checker\n---\nBody\n"
    assert "agent: music-back-checker\n" in merge_agent("music-back", "a", text, ["checker"], [])
    assert "agent: checker\n" in merge_agent("music-back", "a", text, ["other"], [])


def test_skills_names_are_not_rewritten_in_the_agent_key() -> None:
    text = "---\nname: a\nagent: checks\n---\nBody\n"
    assert "agent: checks\n" in merge_agent("music-back", "a", text, [], ["checks"])


def test_merge_agent_is_deterministic() -> None:
    args = ("music-back", "checker", AGENT, ["checker", "linter"], ["clean-code"])
    assert merge_agent(*args) == merge_agent(*args)


# --- merge_settings: permissions ---
def permissions(**kinds: list[str]) -> dict[str, Any]:
    return {"permissions": kinds}


def test_no_repos_and_empty_settings_give_nothing() -> None:
    assert merge_settings([]).settings == {}
    result = merge_settings([repo("a"), repo("b")])
    assert (result.settings, result.conflicts) == ({}, [])


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("Read(./.env)", "Read(./music-back/.env)"),
        ("Edit(./src/**)", "Edit(./music-back/src/**)"),
        ("Write(src/**)", "Write(./music-back/src/**)"),
        ("Read(.env)", "Read(./music-back/.env)"),
        ("Glob(./)", "Glob(./music-back/)"),
        ("Read(**/secrets/*)", "Read(**/secrets/*)"),
        ("Read(/abs/file)", "Read(/abs/file)"),
        ("Read(//abs/file)", "Read(//abs/file)"),
        ("Read(~/.ssh/**)", "Read(~/.ssh/**)"),
        ("Read", "Read"),
        ("Read()", "Read()"),
        ("Bash(npm test:*)", "Bash(npm test:*)"),
        ("Bash(./scripts/x.sh)", "Bash(./scripts/x.sh)"),
        ("WebFetch(domain:example.com)", "WebFetch(domain:example.com)"),
        ("mcp__server__tool", "mcp__server__tool"),
    ],
)
def test_path_rules_get_the_repo_folder(rule: str, expected: str) -> None:
    result = merge_settings([repo("music-back", permissions(deny=[rule]))])
    assert result.settings["permissions"]["deny"] == [expected]


def test_permission_lists_are_unioned_in_repo_order_without_duplicates() -> None:
    a = repo("a", permissions(allow=["Bash(ls:*)", "Read(./x)"], ask=["Bash(git push:*)"]))
    b = repo("b", permissions(allow=["Bash(ls:*)", "Read(./x)", "Bash(pwd)"], ask=[]))
    perms = merge_settings([a, b]).settings["permissions"]
    assert perms["allow"] == ["Bash(ls:*)", "Read(./a/x)", "Read(./b/x)", "Bash(pwd)"]
    assert perms["ask"] == ["Bash(git push:*)"]
    assert "deny" not in perms


def test_deny_wins_over_allow() -> None:
    a = repo("a", permissions(allow=["Bash(rm:*)", "Bash(ls:*)", "Read(./.env)"]))
    b = repo("b", permissions(deny=["Bash(rm:*)", "Read(./.env)"]))
    perms = merge_settings([a, b]).settings["permissions"]
    assert perms["allow"] == ["Bash(ls:*)", "Read(./a/.env)"]  # b's deny is for b's folder
    assert perms["deny"] == ["Bash(rm:*)", "Read(./b/.env)"]


def test_deny_in_the_same_repo_removes_its_own_allow() -> None:
    a = repo(
        "a", {"permissions": {"allow": ["Read(./.env)", "Bash(ls:*)"], "deny": ["Read(./.env)"]}}
    )
    perms = merge_settings([a]).settings["permissions"]
    assert perms == {"allow": ["Bash(ls:*)"], "deny": ["Read(./a/.env)"]}


def test_permission_keys_other_than_the_lists_follow_the_conflict_rule() -> None:
    a = repo("a", {"permissions": {"defaultMode": "acceptEdits", "allow": ["X"]}})
    b = repo("b", {"permissions": {"defaultMode": "plan"}})
    result = merge_settings([a, b])
    assert result.settings["permissions"] == {"allow": ["X"]}
    assert result.conflicts == [Conflict("permissions.defaultMode", ("a", "b"))]


def test_equal_permission_mode_is_kept() -> None:
    a = repo("a", {"permissions": {"defaultMode": "plan"}})
    b = repo("b", {"permissions": {"defaultMode": "plan"}})
    assert merge_settings([a, b]).settings["permissions"] == {"defaultMode": "plan"}


# --- merge_settings: enabledPlugins ---
def test_plugins_are_unioned_when_repos_agree() -> None:
    a = repo("a", {"enabledPlugins": {"p@m": True, "q@m": False}})
    b = repo("b", {"enabledPlugins": {"p@m": True, "r@m": True}})
    result = merge_settings([a, b])
    assert result.settings["enabledPlugins"] == {"p@m": True, "q@m": False, "r@m": True}
    assert result.conflicts == []


def test_plugin_that_one_repo_enables_and_another_disables_is_a_conflict() -> None:
    a = repo("a", {"enabledPlugins": {"p@m": True, "ok@m": True}})
    b = repo("b", {"enabledPlugins": {"p@m": False}})
    result = merge_settings([a, b])
    assert result.settings["enabledPlugins"] == {"ok@m": True}
    assert result.conflicts == [Conflict("enabledPlugins.p@m", ("a", "b"))]


def test_only_plugin_conflict_leaves_no_enabled_plugins_key() -> None:
    a = repo("a", {"enabledPlugins": {"p@m": True}})
    b = repo("b", {"enabledPlugins": {"p@m": False}})
    assert "enabledPlugins" not in merge_settings([a, b]).settings


# --- merge_settings: other keys ---
def test_other_keys_are_kept_when_identical_or_set_by_one_repo() -> None:
    a = repo("a", {"model": "opus", "env": {"A": "1"}, "language": "en"})
    b = repo("b", {"model": "opus", "cleanupPeriodDays": 7})
    result = merge_settings([a, b])
    assert result.settings == {
        "model": "opus",
        "env": {"A": "1"},
        "language": "en",
        "cleanupPeriodDays": 7,
    }
    assert result.conflicts == []


def test_differing_other_keys_are_left_out_and_listed_sorted() -> None:
    a = repo("a", {"model": "opus", "language": "en", "env": {"X": "1"}})
    b = repo("b", {"model": "sonnet", "language": "pt", "env": {"X": "1"}})
    c = repo("c", {"model": "opus"})
    result = merge_settings([a, b, c])
    assert result.settings == {"env": {"X": "1"}}
    assert result.conflicts == [
        Conflict("language", ("a", "b")),
        Conflict("model", ("a", "b", "c")),
    ]


# --- merge_settings: hooks ---
def command_hook(command: str, **extra: Any) -> dict[str, Any]:
    return {"type": "command", "command": command, **extra}


def test_command_hooks_are_wrapped_with_their_repo_and_keep_the_timeout() -> None:
    hook = command_hook("$CLAUDE_PROJECT_DIR/.claude/hooks/it's.sh", timeout=30)
    a = repo("a", {"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [hook]}]}})
    group = merge_settings([a]).settings["hooks"]["PostToolUse"][0]
    assert group["matcher"] == "Edit"
    assert group["hooks"] == [
        {"type": "command", "command": wrapped("a", hook["command"]), "timeout": 30}
    ]


def test_hook_groups_with_the_same_event_and_matcher_are_concatenated_in_repo_order() -> None:
    a = repo(
        "a",
        {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Edit", "hooks": [command_hook("one")]},
                    {"matcher": "Bash", "hooks": [command_hook("two")]},
                ],
                "Stop": [{"hooks": [command_hook("stop-a")]}],
            }
        },
    )
    b = repo(
        "b",
        {
            "hooks": {
                "Stop": [{"hooks": [command_hook("stop-b")]}],
                "PostToolUse": [{"matcher": "Edit", "hooks": [command_hook("three")]}],
            }
        },
    )
    hooks = merge_settings([a, b]).settings["hooks"]
    assert list(hooks) == ["PostToolUse", "Stop"]
    edit, bash = hooks["PostToolUse"]
    assert [h["command"] for h in edit["hooks"]] == [wrapped("a", "one"), wrapped("b", "three")]
    assert [h["command"] for h in bash["hooks"]] == [wrapped("a", "two")]
    (stop,) = hooks["Stop"]
    assert "matcher" not in stop
    assert [h["command"] for h in stop["hooks"]] == [wrapped("a", "stop-a"), wrapped("b", "stop-b")]


def test_hook_without_a_command_string_is_kept_as_it_is() -> None:
    prompt = {"type": "prompt", "prompt": "Is it done?"}
    odd = {"type": "command", "command": ["not", "a", "string"]}
    a = repo("a", {"hooks": {"Stop": [{"hooks": [prompt, odd]}]}})
    assert merge_settings([a]).settings["hooks"]["Stop"][0]["hooks"] == [prompt, odd]


def test_identical_hook_entries_in_a_group_appear_once() -> None:
    hook = command_hook("same")
    a = repo("a", {"hooks": {"Stop": [{"hooks": [hook, hook]}, {"hooks": [hook]}]}})
    (group,) = merge_settings([a]).settings["hooks"]["Stop"]
    assert len(group["hooks"]) == 1


def test_merge_settings_does_not_change_its_input() -> None:
    settings = {
        "permissions": {"allow": ["Read(./x)"]},
        "hooks": {"Stop": [{"hooks": [command_hook("c")]}]},
    }
    before = json.dumps(settings)
    merge_settings([repo("a", settings)])
    assert json.dumps(settings) == before


def test_merge_settings_is_deterministic() -> None:
    repos = [
        repo("a", {"model": "x", "permissions": {"allow": ["Read(./1)"]}}),
        repo("b", {"model": "y", "permissions": {"deny": ["Read(./1)"]}}),
    ]
    assert merge_settings(repos) == merge_settings(repos)


# --- render_claude_md ---
def test_claude_md_imports_each_repo_that_has_one_and_names_the_plan_folder() -> None:
    out = render_claude_md("feat-cart", ["music-front", "music-back"], "music-front")
    assert out == (
        "<!-- Generated by wt. Do not edit; run wt claude sync feat-cart. -->\n"
        "# feat-cart\n"
        "\n"
        "@music-front/CLAUDE.md\n"
        "@music-back/CLAUDE.md\n"
        "\n"
        "Plans live in music-front/.claude/plans/<task>/.\n"
    )


def test_claude_md_without_repo_instructions_has_no_imports() -> None:
    out = render_claude_md("w", [], "a")
    assert out == (
        "<!-- Generated by wt. Do not edit; run wt claude sync w. -->\n"
        "# w\n"
        "\n"
        "Plans live in a/.claude/plans/<task>/.\n"
    )


# --- HOOK_SCRIPT, run for real ---
def run_hook(
    tmp_path: Path, repo_name: str, command: str, stdin: str
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / HOOK_SCRIPT_NAME
    script.write_text(HOOK_SCRIPT)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)}
    return subprocess.run(
        ["bash", str(script), repo_name, command],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def folders(tmp_path: Path) -> Path:
    (tmp_path / "front").mkdir()
    (tmp_path / "back").mkdir()
    return tmp_path


def record_command(out: Path) -> str:
    return f'{{ pwd; echo "$CLAUDE_PROJECT_DIR"; cat; }} > {out}'


def test_hook_script_is_a_bash_script() -> None:
    assert HOOK_SCRIPT.startswith("#!/usr/bin/env bash\n")
    assert HOOK_SCRIPT_NAME == "wt-hook.sh"


def test_hook_runs_in_its_repo_for_a_file_inside_it(folders: Path) -> None:
    out = folders / "out.txt"
    payload = json.dumps({"tool_input": {"file_path": str(folders / "back" / "src" / "a.py")}})
    done = run_hook(folders, "back", record_command(out), payload)
    assert done.returncode == 0
    pwd, project_dir, stdin = out.read_text().split("\n", 2)
    assert Path(pwd).resolve() == (folders / "back").resolve()
    assert project_dir == str(folders / "back")
    assert stdin == payload


def test_hook_is_skipped_for_a_file_in_another_repo(folders: Path) -> None:
    out = folders / "out.txt"
    payload = json.dumps({"tool_input": {"file_path": str(folders / "front" / "a.ts")}})
    done = run_hook(folders, "back", record_command(out) + "; exit 5", payload)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert not out.exists()


def test_hook_is_skipped_when_dotdot_leads_to_another_repo(folders: Path) -> None:
    out = folders / "out.txt"
    escaped = f"{folders}/back/../front/a.ts"
    payload = json.dumps({"tool_input": {"file_path": escaped}})
    done = run_hook(folders, "back", record_command(out), payload)
    assert done.returncode == 0
    assert not out.exists()


def test_hook_runs_when_dotdot_stays_inside_the_repo(folders: Path) -> None:
    out = folders / "out.txt"
    inside = f"{folders}/back/src/../lib/a.py"
    payload = json.dumps({"tool_input": {"file_path": inside}})
    run_hook(folders, "back", record_command(out), payload)
    assert out.exists()


def test_hook_reads_a_relative_path_against_the_project_dir(folders: Path) -> None:
    out = folders / "out.txt"
    for path, ran in [("back/a.py", True), ("front/a.py", False), ("back/../front/a.py", False)]:
        out.unlink(missing_ok=True)
        payload = json.dumps({"tool_input": {"file_path": path}})
        run_hook(folders, "back", record_command(out), payload)
        assert out.exists() is ran, path


def test_hook_is_skipped_for_a_notebook_outside_the_repo(folders: Path) -> None:
    out = folders / "out.txt"
    payload = json.dumps({"tool_input": {"notebook_path": str(folders / "front" / "n.ipynb")}})
    run_hook(folders, "back", record_command(out), payload)
    assert not out.exists()


def test_hook_is_skipped_for_a_sibling_folder_with_the_same_prefix(folders: Path) -> None:
    (folders / "back-old").mkdir()
    out = folders / "out.txt"
    payload = json.dumps({"tool_input": {"file_path": str(folders / "back-old" / "a.py")}})
    run_hook(folders, "back", record_command(out), payload)
    assert not out.exists()


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        json.dumps({"hook_event_name": "Stop"}),
        "not json at all",
        "",
    ],
)
def test_hook_runs_when_no_path_is_present(folders: Path, payload: str) -> None:
    out = folders / "out.txt"
    done = run_hook(folders, "back", record_command(out), payload)
    assert done.returncode == 0
    pwd, _, stdin = out.read_text().split("\n", 2)
    assert Path(pwd).resolve() == (folders / "back").resolve()
    assert stdin == payload


def test_hook_passes_the_exit_code_stdout_and_stderr_through(folders: Path) -> None:
    done = run_hook(folders, "back", "echo out; echo err >&2; exit 2", "{}")
    assert (done.returncode, done.stdout, done.stderr) == (2, "out\n", "err\n")


def test_hook_with_a_missing_repo_folder_fails_without_running_the_command(
    folders: Path,
) -> None:
    out = folders / "out.txt"
    done = run_hook(folders, "gone", f"touch {out}", "{}")
    assert done.returncode != 0
    assert not out.exists()


def test_wrapped_command_runs_end_to_end(folders: Path) -> None:
    (folders / ".claude").mkdir()
    (folders / ".claude" / HOOK_SCRIPT_NAME).write_text(HOOK_SCRIPT)
    out = folders / "out.txt"
    entry = merge_settings(
        [repo("back", {"hooks": {"Stop": [{"hooks": [command_hook(record_command(out))]}]}})]
    ).settings["hooks"]["Stop"][0]["hooks"][0]
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(folders)}
    done = subprocess.run(
        ["bash", "-c", entry["command"]], input="{}", env=env, capture_output=True, text=True
    )
    assert done.returncode == 0
    assert out.read_text().split("\n")[1] == str(folders / "back")
