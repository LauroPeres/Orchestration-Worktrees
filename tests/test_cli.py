import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import FakeDocker
from wt import __version__, cli, compose
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


def _cfg(tmp_path: Path) -> list[str]:
    return ["--config", str(tmp_path / "worktree.yaml")]


def test_lifecycle_healthy(orch, docker: FakeDocker, tmp_path: Path) -> None:
    orch()
    cfg = _cfg(tmp_path)
    up = runner.invoke(app, [*cfg, "up", "feat/cart"])
    assert up.exit_code == 0, up.output
    assert "feat/cart is up" in up.output
    assert docker.ups == ["wt-shop-feat-cart-api"]

    listed = runner.invoke(app, [*cfg, "ls"])
    assert listed.exit_code == 0, listed.output
    assert "feat/cart" in listed.output
    assert "1/1 running" in listed.output

    down = runner.invoke(app, [*cfg, "down", "feat/cart"])
    assert down.exit_code == 0, down.output
    assert docker.downs == ["wt-shop-feat-cart-api"]
    empty = runner.invoke(app, [*cfg, "ls"])
    assert "no branches are up" in empty.output


def test_up_degraded_exits_1(
    orch, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker.healthy = False
    docker.install(monkeypatch)
    orch()
    result = runner.invoke(app, [*_cfg(tmp_path), "up", "feat/cart"])
    assert result.exit_code == 1
    assert "not healthy" in result.output
    listed = runner.invoke(app, [*_cfg(tmp_path), "ls", "--no-probe"])
    assert "degraded" in listed.output


def test_logs_passes_options(
    orch, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, bool, int]] = []

    def fake_logs(project: str, service: str, *, follow: bool, tail: int) -> None:
        calls.append((project, service, follow, tail))

    monkeypatch.setattr(compose, "logs", fake_logs)
    orch()
    runner.invoke(app, [*_cfg(tmp_path), "up", "feat/cart"])
    result = runner.invoke(
        app, [*_cfg(tmp_path), "logs", "feat/cart", "api", "--no-follow", "--tail", "5"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [("wt-shop-feat-cart-api", "api", False, 5)]


def test_logs_of_branch_not_up_fails(orch, docker: FakeDocker, tmp_path: Path) -> None:
    orch()
    result = runner.invoke(app, [*_cfg(tmp_path), "logs", "feat/cart", "api"])
    assert isinstance(result.exception, WtError)
    assert "not up" in str(result.exception)


def test_ls_never_breaks_a_url_at_80_columns(
    orch, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch()
    cfg = _cfg(tmp_path)
    runner.invoke(app, [*cfg, "up", "feat/cart"])
    monkeypatch.setattr(cli.console, "_width", 80)
    listed = runner.invoke(app, [*cfg, "ls", "--no-probe"])
    assert listed.exit_code == 0, listed.output
    lines = [ln for ln in listed.output.splitlines() if "https://" in ln]
    assert lines
    # the URL is whole on its line: it ends in the host (or host:port) and no tail is orphaned
    url = next(t for t in lines[0].split() if t.startswith("https://"))
    assert url.rstrip("/").endswith(".localhost") or ".localhost:" in url
    assert not any(ln.strip().startswith(("lhost", "ost")) for ln in listed.output.splitlines())


def test_down_unknown_branch_prints_only_not_found(
    orch, docker: FakeDocker, tmp_path: Path
) -> None:
    orch()
    result = runner.invoke(app, [*_cfg(tmp_path), "down", "feat/nope"])
    assert result.exit_code == 0, result.output
    assert "not found" in result.output
    assert "is down" not in result.output
