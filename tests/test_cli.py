import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wt import __version__
from wt.cli import app
from wt.config import load_config
from wt.errors import WtError

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_init_writes_valid_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "My_Shop"
    folder.mkdir()
    monkeypatch.chdir(folder)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert load_config(folder / "worktree.yaml").config.project == "my-shop"
    again = runner.invoke(app, ["init"])
    assert isinstance(again.exception, WtError)


def test_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ls"])
    assert isinstance(result.exception, WtError)
    assert "wt init" in str(result.exception)


def test_errors_print_without_traceback(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "wt", "ls"], cwd=tmp_path, capture_output=True, text=True
    )
    assert proc.returncode == 1
    assert proc.stderr.startswith("error:")
    assert "Traceback" not in proc.stderr
