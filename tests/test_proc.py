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
