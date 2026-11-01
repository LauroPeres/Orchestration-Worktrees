"""The v0.7.0 behaviours (SPEC section 23) through the real CLI.

Plugin providers run in a throwaway venv that has `wt` and `examples/provider-echo` installed.
Every test has its own WT_HOME, WT_INSTANCE, catalog and free ports; no mkcert on PATH.
"""

import difflib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from .isolation import reserve_free_indexes
from .test_v061 import Wt, docker, free_port, serve_service

REPO_ROOT = Path(__file__).parent.parent.parent
ECHO_PACKAGE = REPO_ROOT / "examples" / "provider-echo"
CLI_TIMEOUT = 240
INSTALL_TIMEOUT = 600
TUI_TIMEOUT = 60
BUILT_IN_PROVIDERS = "kafka, keycloak, postgres, redis"
SAFE_PATH = "/usr/bin:/bin:/usr/local/bin"  # no mkcert: the self-signed path
UV = shutil.which("uv") or "uv"  # resolved before PATH is trimmed: CI keeps uv outside SAFE_PATH

# Four services in three repos, with comments, odd spacing and a trailing comment to preserve.
CATALOG_TEXT = """\
# shop - a commented catalog the edit commands must not reflow
project: shop          # the project
domain: shop.localhost

repos:
  back: /srv/back     # the API
  front: /srv/front
  # spare: nobody uses it yet
  spare: /srv/spare

services:
  # the API
  api:
    repo: back
    run: ./api --port {{ port }}
    port: auto
    subdomain: api       # https://api.<branch>.shop.localhost
  worker:
    repo: back
    run: ./worker
  web:                   # the web app
    repo: front
    run: npm run dev -- --port {{ port }}
    port: auto
    subdomain: ""
    consumes:
      api: API_URL
  tool:
    repo: spare
    run: ./tool
# end of file
"""


def env_for(instance: str, wt_home: Path, catalog: Path) -> dict[str, str]:
    return {
        **os.environ,
        "COLUMNS": "400",
        "WT_INSTANCE": instance,
        "WT_HOME": str(wt_home),
        "WT_CATALOG": str(catalog),
        "WT_HTTP_PORT": str(free_port()),
        "WT_HTTPS_PORT": str(free_port()),
        "PATH": SAFE_PATH,
    }


class VenvWt(Wt):
    """A Wt that runs `python -m wt` with another interpreter (a venv with plugins installed)."""

    python: str = sys.executable

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.python, "-m", "wt", *args],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
            env={**os.environ, "COLUMNS": "400"},
        )


def changes(before: str, after: str) -> tuple[list[str], list[str]]:
    diff = list(difflib.ndiff(before.splitlines(), after.splitlines()))
    return (
        [line[2:] for line in diff if line.startswith("- ")],
        [line[2:] for line in diff if line.startswith("+ ")],
    )


def plain_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run `wt` in the current interpreter with a fresh WT_HOME (no Docker needed)."""
    env = env_for(f"wtv07{free_port()}", tmp_path / "wt-home", tmp_path / "catalog.yaml")
    return subprocess.run(
        [sys.executable, "-m", "wt", *args],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT,
        env=env,
        cwd=tmp_path,
    )


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[VenvWt]:
    """An isolated instance whose CLI runs from a venv with wt and the echo plugin."""
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.skip("docker is not available")
    venv = tmp_path / "venv"
    subprocess.run([UV, "venv", "-q", str(venv)], check=True, timeout=CLI_TIMEOUT)
    python = str(venv / "bin" / "python")
    subprocess.run(
        [UV, "pip", "install", "-q", "--python", python, str(REPO_ROOT), str(ECHO_PACKAGE)],
        check=True,
        timeout=INSTALL_TIMEOUT,
    )
    instance = f"wtv07{free_port()}"
    wt_home = Path(os.environ["WT_HOME"])
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "catalog.yaml"))
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("PATH", f"{venv / 'bin'}:{SAFE_PATH}")
    reserve_free_indexes(wt_home)
    wt = VenvWt(instance, wt_home, tmp_path / "catalog.yaml")
    wt.python = python
    yield wt
    wt.cleanup()


@pytest.mark.docker
def test_plugin_provider_lifecycle_and_missing_plugin(stack: VenvWt, make_repo) -> None:
    echo_log = stack.catalog.parent / "echo.log"
    repo = make_repo("app")
    services = {"app": serve_service("app", infra={"echo": {"file": str(echo_log)}})}
    stack.write_catalog({"app": repo}, services)

    assert stack.up("feat/p", "app")["status"] == "up"
    assert "create feat-p" in echo_log.read_text().splitlines()

    doctor = json.loads(stack.run("doctor", "--json").stdout)
    assert [c for c in doctor["checks"] if c["name"].startswith("plugin") and not c["ok"]] == []

    stack.json("down", "feat/p")
    assert echo_log.read_text().splitlines() == ["create feat-p", "destroy feat-p"]

    uninstall = subprocess.run(
        [UV, "pip", "uninstall", "--python", stack.python, "wt-provider-echo"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert uninstall.returncode == 0, uninstall.stderr
    result = stack.run("ls")
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert f"unknown infra 'echo' (installed providers: {BUILT_IN_PROVIDERS})" in (
        result.stdout + result.stderr
    )
    assert "Traceback" not in result.stderr


def test_unknown_infra_fails_every_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        "project: p\ndomain: p.localhost\nrepos:\n  r: /srv/r\n"
        "services:\n  s:\n    repo: r\n    run: ./s\n    infra:\n      nope: {}\n"
    )
    for args in (("ls",), ("ls", "--json"), ("up", "feat/x"), ("add", "feat/x", "--uses", "s")):
        result = plain_cli(tmp_path, monkeypatch, *args)
        text = result.stdout + result.stderr
        assert result.returncode == 1, (args, text)
        assert "unknown infra 'nope'" in text, (args, text)
        assert BUILT_IN_PROVIDERS in text, (args, text)
        assert "Traceback" not in text


def test_catalog_check_rejects_unknown_infra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "catalog.yaml").write_text(
        "project: p\ndomain: p.localhost\nrepos:\n  r: /srv/r\n"
        "services:\n  s:\n    repo: r\n    run: ./s\n    infra:\n      nope: {}\n"
    )
    result = plain_cli(tmp_path, monkeypatch, "catalog", "--check")
    assert result.returncode == 1, result.stdout
    assert "unknown infra 'nope'" in result.stdout + result.stderr


def test_catalog_edit_round_trip_keeps_untouched_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(CATALOG_TEXT)
    for name in ("extra", "ghost"):
        (tmp_path / name).mkdir()

    def edit(*args: str) -> dict[str, object]:
        result = plain_cli(tmp_path, monkeypatch, "catalog", *args, "--json")
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        document: dict[str, object] = json.loads(result.stdout)
        assert document["schema"] == 1
        assert document["changed"] is True
        return document

    def check() -> None:
        result = plain_cli(tmp_path, monkeypatch, "catalog", "--check")
        assert result.returncode == 0, (result.stdout, result.stderr)

    check()
    edit("add-repo", "extra", str(tmp_path / "extra"))
    edit(
        "add-service",
        "cache",
        "--repo",
        "extra",
        "--run",
        "./cache --port {{ port }}",
        "--port",
        "auto",
        "--subdomain",
        "x",
        "--consumes",
        "api:API_URL",
        "--setup",
        "echo hi",
    )
    check()
    added = catalog.read_text()
    removed_lines, added_lines = changes(CATALOG_TEXT, added)
    assert removed_lines == [], removed_lines
    assert any("extra:" in line for line in added_lines)
    assert any("cache:" in line for line in added_lines)
    assert all(line in added.splitlines() for line in CATALOG_TEXT.splitlines())

    edit("remove-service", "cache")
    edit("remove-repo", "extra")
    check()
    assert catalog.read_text() == CATALOG_TEXT  # byte for byte, comments and order included

    refused = plain_cli(tmp_path, monkeypatch, "catalog", "remove-service", "api")
    assert refused.returncode == 1  # web consumes api
    assert catalog.read_text() == CATALOG_TEXT
    duplicate = plain_cli(tmp_path, monkeypatch, "catalog", "add-repo", "back", "/srv/x")
    assert duplicate.returncode == 1
    assert catalog.read_text() == CATALOG_TEXT


def test_init_with_writes_the_infra_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = plain_cli(tmp_path, monkeypatch, "init", "--with", "postgres,redis")
    assert result.returncode == 0, (result.stdout, result.stderr)
    catalog = tmp_path / "catalog.yaml"  # WT_CATALOG points here
    text = catalog.read_text()
    assert "postgres:" in text and "redis:" in text
    check = plain_cli(tmp_path, monkeypatch, "catalog", "--check")
    assert check.returncode == 0, (check.stdout, check.stderr)


def test_init_with_unknown_provider_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = plain_cli(tmp_path, monkeypatch, "init", "--with", "nope")
    text = result.stdout + result.stderr
    assert result.returncode == 1, text
    assert BUILT_IN_PROVIDERS in text
    assert not list(tmp_path.glob("**/catalog.yaml"))


@pytest.mark.skipif(sys.platform != "linux", reason="util-linux `script -c`; BSD script differs")
def test_tui_services_view_log_page_round_trip(tmp_path: Path, make_repo) -> None:
    repo = make_repo("app")
    catalog = tmp_path / "catalog.yaml"
    wt_home = tmp_path / "wt-home"
    catalog.write_text(
        f"project: tuiv07\ndomain: tuiv07.localhost\nrepos:\n  app: {repo}\n"
        "services:\n  app:\n    repo: app\n    run: ./app\n"
    )
    env = env_for(f"wtv07{free_port()}", wt_home, catalog)
    add = subprocess.run(
        [sys.executable, "-m", "wt", "add", "feat/t", "--uses", "app"],
        capture_output=True,
        text=True,
        env=env,
        timeout=CLI_TIMEOUT,
    )
    assert add.returncode == 0, (add.stdout, add.stderr)
    # enter opens the services view, l the log page (no log yet), esc twice, q quits
    keys = "sleep 4; printf '\\r'; sleep 3; printf 'l'; sleep 2; printf '\\033'; sleep 1; "
    keys += "printf '\\033'; sleep 1; printf 'q'; sleep 1"
    command = f"({keys}) | script -qec '{sys.executable} -m wt tui' /dev/null"
    result = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env={**env, "TERM": "xterm-256color", "LINES": "40"},
        timeout=TUI_TIMEOUT,
    )
    screen = result.stdout + result.stderr
    assert "Traceback" not in screen, screen[-2000:]
    assert result.returncode == 0, screen[-2000:]
    assert "no log yet" in screen, screen[-2000:]
