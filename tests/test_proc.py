import subprocess
from pathlib import Path

import pytest

from wt import proc
from wt.errors import WtError


def test_missing_command_says_not_found_on_path() -> None:
    with pytest.raises(WtError, match="'wt-no-such-cmd' was not found on PATH"):
        proc.run(["wt-no-such-cmd"])


def test_missing_cwd_names_the_folder(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    with pytest.raises(WtError, match="gone") as info:
        proc.run(["git", "--version"], cwd=missing)
    assert "not found on PATH" not in str(info.value)


def test_other_oserror_names_command_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied", "/x/tool")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(WtError, match=r"tool.*Permission denied"):
        proc.run(["tool", "--flag"])


def test_uses_utf8_with_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    proc.run(["x"])
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert "text" not in seen


def test_on_line_streams_merged_output_and_keeps_returncode() -> None:
    lines: list[str] = []
    with pytest.raises(proc.CommandError) as info:
        proc.run(
            ["sh", "-c", "echo a; sleep 0.1; echo b >&2; exit 3"],
            capture=False,
            on_line=lines.append,
        )
    assert lines == ["a", "b"]
    assert info.value.result.returncode == 3
    assert info.value.result.stdout == "a\nb\n"


def test_on_line_without_check_returns_result() -> None:
    lines: list[str] = []
    result = proc.run(["sh", "-c", "echo one"], check=False, capture=False, on_line=lines.append)
    assert result.ok and result.stdout == "one\n" and lines == ["one"]


def test_on_line_timeout_kills_and_raises() -> None:
    with pytest.raises(WtError, match=r"timed out after 0\.3s"):
        proc.run(
            ["sh", "-c", "echo x; sleep 5"], capture=False, timeout=0.3, on_line=lambda _: None
        )


def test_on_line_missing_command() -> None:
    with pytest.raises(WtError, match="was not found on PATH"):
        proc.run(["wt-no-such-cmd"], capture=False, on_line=lambda _: None)


def test_streaming_timeout_kills_grandchildren_holding_the_pipe() -> None:
    import time

    start = time.monotonic()
    with pytest.raises(WtError):
        proc.run(["sh", "-c", "sleep 30 & sleep 30"], capture=False, timeout=1, on_line=print)
    assert time.monotonic() - start < 10


SECRET = "postgres://user:s3cret-pw@db/app"
PRINT_ENV_AND_ARGV = 'printf "%s\\n" "$WT_TEST_SECRET"; ps -o args= -p $$'


def run_with_env(script: str, env: dict[str, str], *, streaming: bool) -> proc.Result:
    if streaming:
        return proc.run(["sh", "-c", script], env=env, capture=False, on_line=lambda _: None)
    return proc.run(["sh", "-c", script], env=env)


@pytest.mark.parametrize("streaming", [False, True])
def test_env_reaches_the_child_but_not_its_argv(streaming: bool) -> None:
    result = run_with_env(PRINT_ENV_AND_ARGV, {"WT_TEST_SECRET": SECRET}, streaming=streaming)
    env_line, *argv_lines = result.stdout.splitlines()
    assert env_line == SECRET
    assert SECRET not in "\n".join(argv_lines)


@pytest.mark.parametrize("streaming", [False, True])
def test_env_is_added_to_the_inherited_environment(
    monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    monkeypatch.setenv("WT_TEST_INHERITED", "kept")
    result = run_with_env(
        'echo "$WT_TEST_INHERITED $WT_TEST_NEW"', {"WT_TEST_NEW": "added"}, streaming=streaming
    )
    assert result.stdout.strip() == "kept added"


def test_without_env_the_child_inherits_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)
    proc.run(["x"])
    assert seen["env"] is None


def test_timeout_raises_command_timeout() -> None:
    with pytest.raises(proc.CommandTimeout, match=r"command timed out after 0\.2s: sleep 5"):
        proc.run(["sleep", "5"], timeout=0.2)


def test_streaming_timeout_raises_command_timeout() -> None:
    with pytest.raises(proc.CommandTimeout, match=r"command timed out after 0\.3s"):
        proc.run(
            ["sh", "-c", "echo x; sleep 5"], capture=False, timeout=0.3, on_line=lambda _: None
        )


def test_command_timeout_is_a_wt_error() -> None:
    assert issubclass(proc.CommandTimeout, WtError)
