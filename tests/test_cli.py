import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import SHARED, FakeDocker
from wt import __version__, cli, compose, host
from wt.cli import app
from wt.config import load_config
from wt.errors import WtError
from wt.orchestrator import ServiceReport

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
    assert "1/1" in listed.output

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
    listed = runner.invoke(app, [*_cfg(tmp_path), "ls"])
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


SERVE = Path(__file__).parent / "serve.py"


def _assert_urls_whole(output: str) -> None:
    urls = [t for ln in output.splitlines() for t in ln.split() if t.startswith("https://")]
    assert {"https://api.feat-cart.shop.localhost", "https://feat-cart.shop.localhost"} <= set(urls)
    for ln in output.splitlines():
        assert len(ln) <= 80 or "https://" not in ln, ln


def test_up_and_ls_tables_keep_real_urls_whole_and_count_all_services(
    orch, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = {"repo": "api", "run": f"{sys.executable} {SERVE} {{{{ port }}}}", "port": "auto"}
    o = orch(extra_services={"web": {**web, "subdomain": ""}})
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli.console, "_width", 80)
    try:
        up = runner.invoke(app, [*cfg, "up", "feat/cart"])
        assert up.exit_code == 0, up.output
        _assert_urls_whole(up.output)
        listed = runner.invoke(app, [*cfg, "ls", "--no-probe"])
        assert listed.exit_code == 0, listed.output
        _assert_urls_whole(listed.output)
        # 1 compose container (fake docker) + 1 live run process, no " running" suffix
        assert "2/2" in listed.output
        assert "running" not in listed.output
    finally:
        for bs in o.store.load().branches.values():
            for ps in bs.processes.values():
                host.stop(ps, grace=0.2)


def test_up_table_and_ls_show_kind_and_databases(
    orch, fake_provider, docker: FakeDocker, tmp_path: Path
) -> None:
    orch(shared=SHARED)
    cfg = _cfg(tmp_path)
    up = runner.invoke(app, [*cfg, "up", "feat/cart"])
    assert up.exit_code == 0, up.output
    assert "KIND" in up.output and "compose" in up.output
    listed = runner.invoke(app, [*cfg, "ls", "--no-probe"])
    assert "KIND" in listed.output and "compose" in listed.output
    assert "databases:" in listed.output and "db_feat_cart" in listed.output


def test_ls_shows_docker_unavailable(
    orch, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch()
    cfg = _cfg(tmp_path)
    runner.invoke(app, [*cfg, "up", "feat/cart"])

    def broken(project: str) -> list[compose.Container]:
        raise WtError("docker is not reachable")

    monkeypatch.setattr(compose, "ps", broken)
    monkeypatch.setattr(cli.console, "_width", 120)
    listed = runner.invoke(app, [*cfg, "ls", "--no-probe"])
    assert listed.exit_code == 0, listed.output
    assert "docker unavailable" in listed.output
    assert "0/0" not in listed.output


def test_down_unknown_branch_prints_only_not_found(
    orch, docker: FakeDocker, tmp_path: Path
) -> None:
    orch()
    result = runner.invoke(app, [*_cfg(tmp_path), "down", "feat/nope"])
    assert result.exit_code == 0, result.output
    assert "not found" in result.output
    assert "is down" not in result.output


def test_up_table_keeps_url_whole_next_to_a_long_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real output of the shop example: the health detail is long and used to squeeze the URL.
    monkeypatch.setattr(cli.console, "_width", 80)
    reports = [
        ServiceReport(
            "api",
            "ok",
            "GET /healthz -> HTTP 200",
            url="https://api.main.shop.localhost",
            kind="compose",
        ),
        ServiceReport(
            "web", "ok", "GET / -> HTTP 200", url="https://main.shop.localhost", kind="run"
        ),
    ]
    with cli.console.capture() as captured:
        cli.console.print(cli._report_table(reports))
    out = captured.get()
    assert "https://api.main.shop.localhost" in out
    assert "https://main.shop.localhost" in out


LONG_URL = "https://api.a-very-long-branch-name-for-width-checks.shop.localhost:18443"


def test_non_tty_output_is_never_wrapped_or_cut() -> None:
    reports = [
        ServiceReport("api", "ok", "GET /healthz -> HTTP 200 " * 4, url=LONG_URL, kind="compose")
    ]
    import io

    from rich.console import Console

    buf = io.StringIO()
    wide = cli.NoLimitConsole(file=buf, highlight=False)
    wide.print(cli._report_table(reports))
    [row] = [ln for ln in buf.getvalue().splitlines() if "https://" in ln]
    assert LONG_URL in row and "HTTP 200" in row
    assert isinstance(wide, Console) and not wide.is_terminal


def test_ls_piped_keeps_each_row_on_one_line(orch, docker: FakeDocker, tmp_path: Path) -> None:
    orch()
    cfg = _cfg(tmp_path)
    runner.invoke(app, [*cfg, "up", "feat/cart"])
    listed = runner.invoke(app, [*cfg, "ls", "--no-probe"])
    lines = [ln for ln in listed.output.splitlines() if "feat/cart" in ln or "https://" in ln]
    assert len(lines) == 1
    assert "https://api.feat-cart.shop.localhost" in lines[0]
