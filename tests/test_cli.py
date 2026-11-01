import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import typer
import yaml
from typer.testing import CliRunner

from tests.conftest import COMPOSE, SHARED, SHOP_CATALOG, FakeDocker
from wt import __version__, cli, compose, doctor, host, proc
from wt.catalog import load_project
from wt.cli import app
from wt.errors import WtError
from wt.orchestrator import BranchStatus, Orchestrator, ServiceReport
from wt.state import BranchState, StateStore

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_catalog_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WT_CATALOG of the machine running the tests must not decide where wt looks."""
    monkeypatch.delenv("WT_CATALOG", raising=False)


@pytest.fixture
def shop(make_repo, tmp_path: Path, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch):
    """Write the shop catalog (back <- front) and point WT_CATALOG at it; returns the file."""

    def build(
        extra_services: dict[str, object] | None = None, infra: dict[str, object] | None = None
    ) -> Path:
        if not (tmp_path / "shop-api").exists():
            make_repo("shop-api", {"docker-compose.yml": COMPOSE})
        catalog: dict[str, Any] = copy.deepcopy(SHOP_CATALOG)
        catalog["services"].update(extra_services or {})
        if infra:
            catalog["services"]["back"]["infra"] = infra
        path = tmp_path / "catalog.yaml"
        path.write_text(yaml.safe_dump(catalog))
        monkeypatch.setenv("WT_CATALOG", str(path))
        return path

    return build


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_init_writes_a_catalog_that_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "My_Shop"
    folder.mkdir()
    monkeypatch.chdir(folder)
    target = tmp_path / "wt-home" / "catalog.yaml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    loaded = load_project(target)
    assert loaded.config.project == "my-shop"
    assert loaded.config.repos == {"app": str(folder)}  # absolute, not "."
    assert list(loaded.config.services) == ["app"]
    assert not (folder / "worktree.yaml").exists() and not (folder / "catalog.yaml").exists()


def test_init_uses_the_catalog_option_and_creates_its_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "deep" / "er" / "mine.yaml"
    result = runner.invoke(app, ["--catalog", str(target), "init", "--project", "blog"])
    assert result.exit_code == 0, result.output
    assert load_project(target).config.project == "blog"
    assert str(target) in result.output


def test_init_refuses_to_overwrite_unless_forced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "c.yaml"
    target.write_text("mine")
    monkeypatch.setenv("WT_CATALOG", str(target))
    again = runner.invoke(app, ["init"])
    assert isinstance(again.exception, WtError)
    assert f"catalog already exists at {target}" in str(again.exception)
    assert target.read_text() == "mine"
    forced = runner.invoke(app, ["init", "--force"])
    assert forced.exit_code == 0, forced.output
    assert load_project(target).config.services


def test_init_keeps_a_folder_name_that_starts_with_a_digit_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "2048"
    folder.mkdir()
    monkeypatch.chdir(folder)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert load_project(tmp_path / "wt-home" / "catalog.yaml").config.project == "app-2048"


def test_init_quotes_a_path_that_yaml_would_misread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "a: b # c"
    folder.mkdir()
    monkeypatch.chdir(folder)
    assert runner.invoke(app, ["init", "--project", "odd"]).exit_code == 0
    assert load_project(tmp_path / "wt-home" / "catalog.yaml").config.repos["app"] == str(folder)


def test_missing_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ls"])
    assert isinstance(result.exception, WtError)
    assert f"no catalog at {tmp_path / 'wt-home' / 'catalog.yaml'}" in str(result.exception)
    assert "wt init" in str(result.exception)


def test_the_current_folder_and_its_parents_are_never_searched(
    shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop()
    monkeypatch.delenv("WT_CATALOG")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "catalog.yaml").write_text(yaml.safe_dump(SHOP_CATALOG))
    (tmp_path / "work" / "worktree.yaml").write_text("project: x\n")
    monkeypatch.chdir(tmp_path / "work")
    result = runner.invoke(app, ["ls"])
    assert isinstance(result.exception, WtError)
    assert "no catalog at" in str(result.exception)


def test_catalog_location_order_option_then_env_then_home(
    shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = shop()
    other = tmp_path / "other.yaml"
    other.write_text(yaml.safe_dump({**SHOP_CATALOG, "project": "other", "domain": "o.localhost"}))
    projects: list[str] = []

    def fake_ls(self: Orchestrator, *, probe: bool) -> list[BranchStatus]:
        projects.append(self.cfg.project)
        return []

    monkeypatch.setattr(Orchestrator, "ls", fake_ls)
    runner.invoke(app, ["ls"])  # WT_CATALOG
    runner.invoke(app, ["-c", str(other), "ls"])  # the option wins over the environment
    runner.invoke(app, ["--catalog", str(other), "ls"])
    monkeypatch.delenv("WT_CATALOG")
    (tmp_path / "wt-home").mkdir()
    (tmp_path / "wt-home" / "catalog.yaml").write_text(env_file.read_text())
    runner.invoke(app, ["ls"])  # $WT_HOME/catalog.yaml
    assert projects == ["shop", "other", "other", "shop"]


def test_errors_print_without_traceback(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "wt", "ls"], cwd=tmp_path, capture_output=True, text=True
    )
    assert proc.returncode == 1
    assert proc.stderr.startswith("error:")
    assert "Traceback" not in proc.stderr


def test_lifecycle_healthy(shop, docker: FakeDocker) -> None:
    shop()
    up = runner.invoke(app, ["up", "feat/cart", "--uses", "front"])
    assert up.exit_code == 0, up.output
    assert "feat/cart is up" in up.output
    assert docker.ups == ["wt-shop-feat-cart-api"]

    listed = runner.invoke(app, ["ls"])
    assert listed.exit_code == 0, listed.output
    assert "feat/cart" in listed.output
    assert "back" in listed.output and "front" in listed.output  # the dependency came along

    down = runner.invoke(app, ["down", "feat/cart"])
    assert down.exit_code == 0, down.output
    assert docker.downs == ["wt-shop-feat-cart-api"]
    empty = runner.invoke(app, ["ls"])
    assert "no branches are up" in empty.output


def test_up_degraded_exits_1(shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
    docker.healthy = False
    docker.install(monkeypatch)
    shop()
    result = runner.invoke(app, ["up", "feat/cart", "--uses", "front"])
    assert result.exit_code == 1
    assert "not healthy" in result.output
    listed = runner.invoke(app, ["ls"])
    assert "degraded" in listed.output


def test_up_without_a_selection_says_how_to_choose(shop) -> None:
    shop()
    result = runner.invoke(app, ["up", "feat/cart"])
    assert isinstance(result.exception, WtError)
    assert str(result.exception) == (
        "branch feat/cart has no services yet; run wt add feat/cart --uses a,b"
    )


def test_uses_takes_comma_separated_names_and_ignores_blanks(shop) -> None:
    shop()
    added = runner.invoke(app, ["add", "feat/a", "--uses", " back , ,front "])
    assert added.exit_code == 0, added.output
    stored = StateStore("shop").load().branches["feat/a"].uses
    assert stored == ["back", "front"]


@pytest.mark.parametrize("command", ["add", "up"])
def test_an_empty_uses_value_is_refused(shop, command: str) -> None:
    shop()
    result = runner.invoke(app, [command, "feat/a", "--uses", " , "])
    assert isinstance(result.exception, WtError)
    assert "choose at least one service" in str(result.exception)
    assert "feat/a" not in StateStore("shop").load().branches


@pytest.mark.parametrize("command", ["add", "up"])
def test_an_unknown_service_lists_the_catalog(shop, command: str) -> None:
    shop()
    result = runner.invoke(app, [command, "feat/a", "--uses", "ghost"])
    assert isinstance(result.exception, WtError)
    assert "unknown service 'ghost'" in str(result.exception)
    assert "back, front" in str(result.exception)


def test_logs_passes_options(shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: object) -> proc.Result:
        calls.append(args)
        return proc.Result(0, "", "")

    shop()
    runner.invoke(app, ["up", "feat/cart", "--uses", "front"])
    monkeypatch.setattr(proc, "run", fake_run)
    result = runner.invoke(app, ["logs", "feat/cart", "back", "--no-follow", "--tail", "5"])
    assert result.exit_code == 0, result.output
    assert calls == [
        ["docker", "compose", "-p", "wt-shop-feat-cart-api", "logs", "--tail", "5", "db"]
    ]


def test_logs_of_branch_not_up_fails(shop, docker: FakeDocker) -> None:
    shop()
    result = runner.invoke(app, ["logs", "feat/cart", "back"])
    assert isinstance(result.exception, WtError)
    assert "not up" in str(result.exception)


def test_ls_never_breaks_a_url_at_80_columns(
    shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop()
    runner.invoke(app, ["up", "feat/cart", "--uses", "front"])
    monkeypatch.setattr(cli.console, "_width", 80)
    listed = runner.invoke(app, ["ls", "--no-probe"])
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
    assert {"https://back.feat-cart.shop.localhost", "https://feat-cart.shop.localhost"} <= set(
        urls
    )
    for ln in output.splitlines():
        assert len(ln) <= 80 or "https://" not in ln, ln


def test_up_and_ls_tables_keep_real_urls_whole_and_count_all_services(
    shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = {"repo": "api", "run": f"{sys.executable} {SERVE} {{{{ port }}}}", "port": "auto"}
    shop(extra_services={"web": {**web, "subdomain": ""}})
    monkeypatch.setattr(cli.console, "_width", 80)
    try:
        up = runner.invoke(app, ["up", "feat/cart", "--uses", "back,web"])
        assert up.exit_code == 0, up.output
        _assert_urls_whole(up.output)
        listed = runner.invoke(app, ["ls", "--no-probe"])
        assert listed.exit_code == 0, listed.output
        _assert_urls_whole(listed.output)
        # 1 compose container (fake docker) + 1 live run process, no " running" suffix
        assert "2/2" in listed.output
        assert "running" not in listed.output
    finally:
        for bs in StateStore("shop").load().branches.values():
            for ps in bs.processes.values():
                host.stop(ps, grace=0.2)


def test_up_table_and_ls_show_kind_and_databases(shop, fake_provider, docker: FakeDocker) -> None:
    shop(infra=SHARED)
    up = runner.invoke(app, ["up", "feat/cart", "--uses", "back"])
    assert up.exit_code == 0, up.output
    assert "KIND" in up.output and "compose" in up.output
    listed = runner.invoke(app, ["ls", "--no-probe"])
    assert "KIND" in listed.output and "compose" in listed.output
    assert "databases:" in listed.output and "db_feat_cart" in listed.output


def test_ls_shows_docker_unavailable(
    shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop()
    runner.invoke(app, ["up", "feat/cart", "--uses", "front"])

    def broken(project: str) -> list[compose.Container]:
        raise WtError("docker is not reachable")

    monkeypatch.setattr(compose, "ps", broken)
    monkeypatch.setattr(cli.console, "_width", 120)
    listed = runner.invoke(app, ["ls", "--no-probe"])
    assert listed.exit_code == 0, listed.output
    assert "docker unavailable" in listed.output
    assert "0/0" not in listed.output


def test_down_unknown_branch_prints_only_not_found(shop, docker: FakeDocker) -> None:
    shop()
    result = runner.invoke(app, ["down", "feat/nope"])
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


def test_ls_piped_keeps_each_row_on_one_line(shop, docker: FakeDocker) -> None:
    shop()
    runner.invoke(app, ["up", "feat/cart", "--uses", "back"])
    listed = runner.invoke(app, ["ls", "--no-probe"])
    lines = [ln for ln in listed.output.splitlines() if "feat/cart" in ln or "https://" in ln]
    assert len(lines) == 1
    assert "https://back.feat-cart.shop.localhost" in lines[0]


CATALOG = """\
project: notes
domain: notes.localhost
repos:
  notes-back: ./back
  notes-front: ./front
services:
  back:
    repo: notes-back
    compose: docker-compose.yml
    port: 8000
    subdomain: api
    remote: https://api.staging.example
    infra:
      postgres: {databases: [notes]}
  front:
    repo: notes-front
    run: python3 server.py --port {{ port }}
    port: auto
    subdomain: ""
    consumes:
      back: API_URL
"""


def test_catalog_table_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "c.yaml").write_text(CATALOG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "c.yaml"))
    result = runner.invoke(app, ["catalog"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == [
        "SERVICE",
        "REPO",
        "KIND",
        "INFRA",
        "CONSUMES",
        "CONSUMED",
        "BY",
        "REMOTE",
    ]
    assert lines[1].split() == [
        "back",
        "notes-back",
        "compose",
        "postgres",
        "-",
        "front",
        "https://api.staging.example",
    ]
    assert lines[2].split() == ["front", "notes-front", "run", "-", "back", "-", "-"]


def test_catalog_option_wins_and_the_cwd_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "mine.yaml").write_text(CATALOG)
    (tmp_path / "worktree.yaml").write_text("project: x\ndomain: x.localhost\nuses: [front]\n")
    (tmp_path / "catalog.yaml").write_text("not: a catalog")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "missing.yaml"))
    result = runner.invoke(app, ["-c", str(tmp_path / "mine.yaml"), "catalog", "--check"])
    assert result.exit_code == 0, result.output
    assert f"catalog ok: {tmp_path / 'mine.yaml'}" in result.output


def test_catalog_defaults_to_the_home_catalog(tmp_path: Path) -> None:
    (tmp_path / "wt-home").mkdir()
    (tmp_path / "wt-home" / "catalog.yaml").write_text(CATALOG)
    result = runner.invoke(app, ["catalog"])
    assert result.exit_code == 0, result.output
    assert "notes-back" in result.output


def test_catalog_check_passes_a_good_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _check_catalog(tmp_path, monkeypatch, CATALOG)
    assert result.exit_code == 0, result.output
    assert "catalog ok" in result.output


def test_error_line_is_not_wrapped_when_stderr_is_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    long_path = "/var/log/wt/" + "x" * 200 + "/run.log"
    fake = typer.Typer()

    def boom() -> None:
        raise WtError(f"api exited; see {long_path}")

    fake.command()(boom)

    monkeypatch.setattr(sys, "argv", ["wt"])
    with pytest.raises(SystemExit) as stop:
        cli._run(fake)
    assert stop.value.code == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines == [f"error: api exited; see {long_path}"]


def test_catalog_check_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "c.yaml").write_text(CATALOG.replace("repo: notes-front", "repo: nope"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "c.yaml"))
    result = runner.invoke(app, ["catalog", "--check"])
    assert result.exit_code == 1
    assert "unknown repo 'nope'" in result.output


def _check_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Any:
    (tmp_path / "c.yaml").write_text(text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "c.yaml"))
    return runner.invoke(app, ["catalog", "--check"])


def test_catalog_check_reports_a_dependency_without_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _check_catalog(tmp_path, monkeypatch, CATALOG.replace("    subdomain: api\n", ""))
    assert result.exit_code == 1, result.output
    assert "service 'front'" in result.output
    assert "has no 'subdomain'" in result.output
    assert "catalog ok" not in result.output


def test_catalog_check_reports_env_that_sets_the_consumed_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = CATALOG.replace("    consumes:\n", "    env: {API_URL: http://x}\n    consumes:\n")
    result = _check_catalog(tmp_path, monkeypatch, text)
    assert result.exit_code == 1, result.output
    assert "service 'front'" in result.output
    assert "env already sets API_URL" in result.output


def test_catalog_check_reports_a_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = CATALOG.replace(
        "    infra:\n      postgres: {databases: [notes]}\n",
        "    consumes:\n      front: FRONT_URL\n",
    )
    result = _check_catalog(tmp_path, monkeypatch, text)
    assert result.exit_code == 1, result.output
    assert "cycle" in result.output


def test_catalog_check_reports_an_unknown_consumes_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _check_catalog(
        tmp_path, monkeypatch, CATALOG.replace("back: API_URL", "ghost: API_URL")
    )
    assert result.exit_code == 1, result.output
    assert "ghost" in result.output


def test_catalog_check_reports_every_failing_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = CATALOG.replace("    subdomain: api\n", "") + (
        "  admin:\n    repo: notes-front\n    run: python3 admin.py\n"
        "    consumes:\n      back: BACK_URL\n"
    )
    result = _check_catalog(tmp_path, monkeypatch, text)
    assert result.exit_code == 1, result.output
    assert "service 'front'" in result.output
    assert "service 'admin'" in result.output
    assert "service 'back'" not in result.output


def test_catalog_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "none.yaml"))
    result = runner.invoke(app, ["catalog"])
    assert isinstance(result.exception, WtError)
    assert "catalog not found" in str(result.exception)


def _capture_tui(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from wt.tui import WtApp

    opened: list[Any] = []
    monkeypatch.setattr(WtApp, "run", lambda self, *a, **k: opened.append(self))
    return opened


def test_tui_with_a_catalog_opens_its_project(shop, tmp_path: Path, monkeypatch) -> None:
    shop()
    opened = _capture_tui(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 0, result.output
    assert opened[0].orch is not None
    assert opened[0].orch.cfg.project == "shop"


def test_worktree_entry_point_takes_the_catalog_option(
    shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = shop()
    monkeypatch.delenv("WT_CATALOG")
    opened = _capture_tui(monkeypatch)
    result = runner.invoke(cli.worktree_app, ["--catalog", str(path)])
    assert result.exit_code == 0, result.output
    assert opened[0].orch.cfg.project == "shop"
    monkeypatch.setenv("WT_CATALOG", str(path))  # and the environment variable
    assert runner.invoke(cli.worktree_app, []).exit_code == 0
    assert opened[1].orch.cfg.project == "shop"


def _record_project(name: str, config: Path) -> None:
    with StateStore(name).edit() as state:
        state.config_path = str(config)
        state.branches["main"] = BranchState(branch="main", slug="main")


def test_tui_all_lists_known_projects_even_with_a_catalog(
    shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = shop()
    other = tmp_path / "blog.yaml"
    other.write_text(yaml.safe_dump({**SHOP_CATALOG, "project": "blog", "domain": "b.localhost"}))
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    old = legacy / "worktree.yaml"
    old.write_text("project: old\ndomain: old.localhost\nservices: {}\n")
    uses_style = tmp_path / "mine.yaml"
    uses_style.write_text("project: tiny\ndomain: t.localhost\nuses: [front]\n")
    shared_style = tmp_path / "plain.yaml"
    shared_style.write_text("project: plain\ndomain: p.localhost\nshared: {}\n")
    wrong_project = tmp_path / "else.yaml"
    wrong_project.write_text(path.read_text())
    for name, config in {
        "shop": path,
        "blog": other,
        "gone": tmp_path / "nope.yaml",
        "old": old,
        "tiny": uses_style,
        "plain": shared_style,
        "mismatch": wrong_project,
    }.items():
        _record_project(name, config)
    opened = _capture_tui(monkeypatch)
    result = runner.invoke(app, ["tui", "--all"])
    assert result.exit_code == 0, result.output
    app_ = opened[0]
    assert app_.orch is None
    entries = {e.name: e for e in app_._discover()}
    assert entries["gone"].orch is None and "config not found" in entries["gone"].note
    assert str(tmp_path / "nope.yaml") in entries["gone"].note
    for legacy_project, file in (("old", old), ("tiny", uses_style), ("plain", shared_style)):
        assert entries[legacy_project].orch is None
        assert entries[legacy_project].note == (
            f"created with a worktree.yaml: run wt migrate {file}"
        )
    assert entries["mismatch"].orch is None and "wt migrate" in entries["mismatch"].note
    for usable in ("shop", "blog"):
        orchestrator = entries[usable].orch
        assert orchestrator is not None and orchestrator() is orchestrator()  # built once, lazily
    assert entries["blog"].orch is not None
    assert entries["blog"].orch().cfg.project == "blog"


def test_tui_without_a_catalog_lists_known_projects_and_knows_where_it_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = _capture_tui(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 0, result.output
    assert opened[0].orch is None
    assert opened[0]._missing_catalog == tmp_path / "wt-home" / "catalog.yaml"
    assert opened[0]._discover() == []


def test_tui_with_a_catalog_keeps_no_missing_catalog_in_all_view(shop, monkeypatch) -> None:
    shop()
    opened = _capture_tui(monkeypatch)
    assert runner.invoke(app, ["tui", "--all"]).exit_code == 0
    assert opened[0]._missing_catalog is None


def test_tui_with_a_broken_catalog_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "c.yaml"
    broken.write_text("project: x\n")
    opened = _capture_tui(monkeypatch)
    result = runner.invoke(app, ["-c", str(broken), "tui"])
    assert isinstance(result.exception, WtError)
    assert opened == []


def test_add_stop_remove_commands(shop, docker: FakeDocker) -> None:
    shop()
    added = runner.invoke(app, ["add", "feat/cart", "--uses", "front"])
    assert added.exit_code == 0, added.output
    assert "stopped" in runner.invoke(app, ["ls", "--no-probe"]).output
    assert runner.invoke(app, ["stop", "feat/cart"]).exit_code == 0
    declined = runner.invoke(app, ["remove", "feat/cart"], input="n\n")
    assert declined.exit_code != 0
    assert "feat/cart" in runner.invoke(app, ["ls", "--no-probe"]).output
    removed = runner.invoke(app, ["remove", "feat/cart", "--yes"])
    assert removed.exit_code == 0, removed.output
    assert "no branches" in runner.invoke(app, ["ls", "--no-probe"]).output


def test_add_then_up_starts_the_selection_without_uses_again(shop, docker: FakeDocker) -> None:
    shop()
    assert runner.invoke(app, ["add", "feat/cart", "--uses", "back"]).exit_code == 0
    up = runner.invoke(app, ["up", "feat/cart"])
    assert up.exit_code == 0, up.output
    assert docker.ups == ["wt-shop-feat-cart-api"]


def test_add_of_a_known_branch_keeps_its_selection(shop) -> None:
    shop()
    runner.invoke(app, ["add", "feat/cart", "--uses", "back"])
    again = runner.invoke(app, ["add", "feat/cart", "--uses", "front"])
    assert again.exit_code == 0, again.output
    assert StateStore("shop").load().branches["feat/cart"].uses == ["back"]


def test_up_with_uses_on_an_unknown_branch_adds_then_starts(shop, docker: FakeDocker) -> None:
    shop()
    up = runner.invoke(app, ["up", "feat/cart", "--uses", "back"])
    assert up.exit_code == 0, up.output
    assert StateStore("shop").load().branches["feat/cart"].uses == ["back"]


def test_add_without_uses_off_a_terminal_prints_the_core_error(shop) -> None:
    shop()
    result = runner.invoke(app, ["add", "feat/cart"], input="1\n")  # nobody reads this
    assert isinstance(result.exception, WtError)
    assert str(result.exception) == (
        "branch feat/cart has no services yet; run wt add feat/cart --uses a,b"
    )
    assert "Services" not in result.output


@pytest.fixture
def on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_interactive", lambda: True)


def test_add_on_a_terminal_lists_the_catalog_and_reads_numbers(shop, on_a_terminal) -> None:
    shop()
    result = runner.invoke(app, ["add", "feat/cart"], input="2\n")
    assert result.exit_code == 0, result.output
    assert "1. back (api)" in result.output and "2. front (api)" in result.output
    assert "feat/cart is added" in result.output
    assert StateStore("shop").load().branches["feat/cart"].uses == ["front"]


def test_add_on_a_terminal_accepts_names_and_numbers_mixed(shop, on_a_terminal) -> None:
    shop()
    result = runner.invoke(app, ["add", "feat/cart"], input="front, 1\n")
    assert result.exit_code == 0, result.output
    assert StateStore("shop").load().branches["feat/cart"].uses == ["front", "back"]


def test_add_on_a_terminal_with_an_empty_answer_cancels_and_creates_nothing(
    shop, on_a_terminal
) -> None:
    shop()
    result = runner.invoke(app, ["add", "feat/cart"], input="\n")
    assert result.exit_code == 1
    assert "cancelled" in result.output and "nothing was created" in result.output
    assert "feat/cart" not in StateStore("shop").load().branches


@pytest.mark.parametrize("answer", ["0", "3"])
def test_add_on_a_terminal_refuses_a_number_outside_the_list(
    shop, on_a_terminal, answer: str
) -> None:
    shop()
    result = runner.invoke(app, ["add", "feat/cart"], input=f"{answer}\n")
    assert isinstance(result.exception, WtError)
    assert f"there is no service number {answer} (choose 1-2)" in str(result.exception)
    assert "feat/cart" not in StateStore("shop").load().branches


def test_add_on_a_terminal_reports_an_unknown_name(shop, on_a_terminal) -> None:
    shop()
    result = runner.invoke(app, ["add", "feat/cart"], input="ghost\n")
    assert isinstance(result.exception, WtError)
    assert "unknown service 'ghost'" in str(result.exception)


def test_add_with_uses_never_prompts_even_on_a_terminal(shop, on_a_terminal) -> None:
    shop()
    result = runner.invoke(app, ["add", "feat/cart", "--uses", "back"])
    assert result.exit_code == 0, result.output
    assert "Services" not in result.output


def _stub_ls(monkeypatch: pytest.MonkeyPatch, rows: list[BranchStatus]) -> None:
    monkeypatch.setattr(Orchestrator, "ls", lambda self, *, probe: rows)


def test_ls_shows_a_dash_for_the_health_of_a_stopped_branch(
    shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop()
    skipped = ServiceReport("api", "skipped", "", "http://api.feat-cart.localhost")
    live = ServiceReport("web", "ok", "200 in 12ms", "http://web.feat-up.localhost")
    _stub_ls(
        monkeypatch,
        [
            BranchStatus("feat/cart", "stopped", 0, 1, services=[skipped]),
            BranchStatus("feat/up", "up", 1, 1, services=[live]),
        ],
    )
    listed = runner.invoke(app, ["ls"])
    assert listed.exit_code == 0, listed.output
    stopped_line = next(line for line in listed.output.splitlines() if "feat/cart" in line)
    up_line = next(line for line in listed.output.splitlines() if "feat/up" in line)
    assert "skipped" not in listed.output
    assert stopped_line.rstrip().endswith("-")
    assert "200 in 12ms" in up_line


def test_ls_no_probe_still_shows_a_dash_for_health(shop, monkeypatch: pytest.MonkeyPatch) -> None:
    shop()
    live = ServiceReport("web", "ok", "200 in 12ms", "http://web.feat-up.localhost")
    _stub_ls(monkeypatch, [BranchStatus("feat/up", "up", 1, 1, services=[live])])
    listed = runner.invoke(app, ["ls", "--no-probe"])
    assert listed.exit_code == 0, listed.output
    assert "200 in 12ms" not in listed.output


REMOVE_QUESTION = (
    "remove feat/cart: removes what wt created for it (containers, volumes, databases, "
    "worktrees) and deletes the git branch in every repo of the project where it exists; "
    "unpushed commits are lost. Remove? [y/N]"
)


def _record_removes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    calls: list[tuple[str, bool]] = []

    def fake_remove(self: Any, branch: str, *, force: bool = False) -> bool:
        calls.append((branch, force))
        return True

    monkeypatch.setattr(Orchestrator, "remove", fake_remove)
    return calls


@pytest.mark.parametrize("answer", ["n\n", "\n"])
def test_remove_asks_the_full_question_and_no_calls_nothing(
    shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    shop()
    calls = _record_removes(monkeypatch)
    result = runner.invoke(app, ["remove", "feat/cart"], input=answer)
    assert result.exit_code != 0, result.output
    assert REMOVE_QUESTION in result.output
    assert calls == []


def test_remove_yes_skips_the_question(
    shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop()
    calls = _record_removes(monkeypatch)
    result = runner.invoke(app, ["remove", "feat/cart", "--yes", "--force"])
    assert result.exit_code == 0, result.output
    assert "Remove?" not in result.output
    assert calls == [("feat/cart", True)]


def test_remove_confirmed_calls_remove_once(
    shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop()
    calls = _record_removes(monkeypatch)
    result = runner.invoke(app, ["remove", "feat/cart"], input="y\n")
    assert result.exit_code == 0, result.output
    assert REMOVE_QUESTION in result.output
    assert calls == [("feat/cart", False)]


def test_stop_unknown_branch_fails(shop, docker: FakeDocker) -> None:
    shop()
    assert runner.invoke(app, ["stop", "feat/nope"]).exit_code != 0


# --- wt migrate ---------------------------------------------------------------------------

LEGACY = {
    "project": "shop",
    "domain": "shop.localhost",
    "repos": {"api": "../shop-api"},
    "services": {
        "back": {
            "repo": "api",
            "compose": "docker-compose.yml",
            "compose_service": "db",
            "port": 5432,
            "subdomain": "back",
        },
        "front": {
            "repo": "api",
            "compose": "docker-compose.yml",
            "compose_service": "api",
            "port": 8000,
            "subdomain": "front",
            "needs": ["back"],
        },
    },
}


@pytest.fixture
def legacy_file(tmp_path: Path):
    """Write `LEGACY` (or an edited copy) as a worktree.yaml in its own folder."""

    def write(**changes: Any) -> Path:
        folder = tmp_path / "legacy"
        folder.mkdir(exist_ok=True)
        path = folder / "worktree.yaml"
        path.write_text(yaml.safe_dump({**copy.deepcopy(LEGACY), **changes}))
        return path

    return write


@pytest.fixture
def new_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A catalog location that does not exist yet, in a folder that does not exist either."""
    path = tmp_path / "home" / "wt" / "catalog.yaml"
    monkeypatch.setenv("WT_CATALOG", str(path))
    return path


def _adopt_state(project: str, *branches: str) -> None:
    with StateStore(project).edit() as state:
        for branch in branches:
            state.branches[branch] = BranchState(branch=branch, slug=branch.replace("/", "-"))


def test_migrate_creates_the_catalog_and_reports_each_service(
    tmp_path: Path, legacy_file, new_catalog: Path
) -> None:
    result = runner.invoke(app, ["migrate", str(legacy_file())])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[:2] == ["added service back", "added service front"]
    assert "adopted branches: none" in lines
    loaded = load_project(new_catalog)
    assert loaded.config.repos == {"api": str(tmp_path / "shop-api")}  # absolute
    assert "comments" not in result.output  # a new file has nothing to lose


def test_migrate_gives_branches_without_a_selection_the_migrated_stack(
    legacy_file, new_catalog: Path
) -> None:
    _adopt_state("shop", "feat/old", "feat/kept")
    with StateStore("shop").edit() as state:
        state.branches["feat/kept"].uses = ["back"]
    result = runner.invoke(app, ["migrate", str(legacy_file())])
    assert result.exit_code == 0, result.output
    assert "adopted branches: feat/old" in result.output
    branches = StateStore("shop").load().branches
    assert branches["feat/old"].uses == ["back", "front"]
    assert branches["feat/kept"].uses == ["back"]


def test_migrate_twice_changes_nothing(legacy_file, new_catalog: Path) -> None:
    path = legacy_file()
    runner.invoke(app, ["migrate", str(path)])
    first = new_catalog.read_bytes()
    again = runner.invoke(app, ["migrate", str(path)])
    assert again.exit_code == 0, again.output
    assert new_catalog.read_bytes() == first
    assert again.output.splitlines()[:2] == [
        "skipped service back (already in the catalog)",
        "skipped service front (already in the catalog)",
    ]
    assert "adopted branches: none" in again.output
    assert "rewrote" not in again.output


def test_migrate_leaves_an_unchanged_catalog_with_its_comments(
    legacy_file, new_catalog: Path
) -> None:
    path = legacy_file()
    runner.invoke(app, ["migrate", str(path)])
    text = "# my notes\n" + new_catalog.read_text()
    new_catalog.write_text(text)
    result = runner.invoke(app, ["migrate", str(path)])
    assert result.exit_code == 0, result.output
    assert new_catalog.read_text() == text


def test_migrate_adds_to_an_existing_catalog_and_says_comments_are_not_kept(
    shop, legacy_file, tmp_path: Path
) -> None:
    path = shop()
    path.write_text("# my notes\n" + path.read_text())
    extra = {"repo": "api", "compose": "docker-compose.yml", "port": 9000, "subdomain": "w"}
    legacy = legacy_file(repos={"api": "../shop-api"}, services={"worker": extra}, project="shop")
    result = runner.invoke(app, ["migrate", str(legacy)])
    assert result.exit_code == 0, result.output
    assert "added service worker" in result.output
    assert "comments in an existing catalog are not preserved" in result.output
    merged = load_project(path, uses=["worker"])
    assert set(merged.config.services) == {"worker"}
    assert "# my notes" not in path.read_text()
    assert list(yaml.safe_load(path.read_text())["services"]) == ["back", "front", "worker"]


def test_migrate_conflict_leaves_the_catalog_untouched(shop, legacy_file) -> None:
    path = shop()
    before = path.read_bytes()
    legacy = legacy_file(repos={"api": "../shop-api"})  # `back` here has other fields
    result = runner.invoke(app, ["migrate", str(legacy)])
    assert isinstance(result.exception, WtError)
    assert "differs from the catalog's" in str(result.exception)
    assert path.read_bytes() == before


def test_migrate_validation_failure_leaves_the_catalog_untouched(
    shop, legacy_file, tmp_path: Path
) -> None:
    path = shop()
    before = path.read_bytes()
    broken = {"repo": "api", "compose": "docker-compose.yml", "needs": ["ghost"]}
    legacy = legacy_file(repos={"api": "../shop-api"}, services={"broken": broken})
    result = runner.invoke(app, ["migrate", str(legacy)])
    assert isinstance(result.exception, WtError)
    assert "service 'broken' needs unknown service 'ghost'" in str(result.exception)
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir() if p.name.startswith(".")] == []


def test_migrate_a_failed_new_catalog_is_not_created(legacy_file, new_catalog: Path) -> None:
    broken = {"repo": "api", "compose": "docker-compose.yml", "needs": ["ghost"]}
    legacy = legacy_file(services={"broken": broken})
    result = runner.invoke(app, ["migrate", str(legacy)])
    assert isinstance(result.exception, WtError)
    assert not new_catalog.exists()


def test_migrate_refuses_a_catalog_of_another_project(shop, legacy_file) -> None:
    shop()
    legacy = legacy_file(project="other", domain="other.localhost")
    result = runner.invoke(app, ["migrate", str(legacy)])
    assert isinstance(result.exception, WtError)
    assert "catalog is project shop" in str(result.exception)


def test_migrate_reports_a_missing_worktree_yaml(tmp_path: Path, new_catalog: Path) -> None:
    result = runner.invoke(app, ["migrate", str(tmp_path / "nope" / "worktree.yaml")])
    assert isinstance(result.exception, WtError)
    assert "not found" in str(result.exception)
    assert not new_catalog.exists()


def test_migrate_reports_an_existing_catalog_that_is_not_yaml(
    legacy_file, new_catalog: Path
) -> None:
    new_catalog.parent.mkdir(parents=True)
    new_catalog.write_text("a: [unclosed")
    result = runner.invoke(app, ["migrate", str(legacy_file())])
    assert isinstance(result.exception, WtError)
    assert "is not valid YAML" in str(result.exception)
    assert new_catalog.read_text() == "a: [unclosed"


def test_migrate_a_uses_style_file_copies_the_services_it_names(
    tmp_path: Path, new_catalog: Path
) -> None:
    source = tmp_path / "old-catalog.yaml"
    source.write_text(CATALOG)
    folder = tmp_path / "legacy"
    folder.mkdir()
    legacy = folder / "worktree.yaml"
    legacy.write_text(
        "project: notes\ndomain: notes.localhost\ncatalog: ../old-catalog.yaml\nuses: [front]\n"
    )
    _adopt_state("notes", "feat/x")
    result = runner.invoke(app, ["migrate", str(legacy)])
    assert result.exit_code == 0, result.output
    assert "added service back" in result.output and "added service front" in result.output
    assert "adopted branches: feat/x" in result.output
    assert StateStore("notes").load().branches["feat/x"].uses == ["front", "back"]


# --- wt doctor and wt catalog --------------------------------------------------------------


def _record_doctor(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    calls: list[tuple[Any, Any]] = []

    def fake(loaded: Any, config_error: Any, *, fix: bool) -> list[doctor.Check]:
        calls.append((loaded, config_error))
        return []

    monkeypatch.setattr(doctor, "run_checks", fake)
    return calls


def test_doctor_loads_the_catalog(shop, monkeypatch: pytest.MonkeyPatch) -> None:
    shop()
    calls = _record_doctor(monkeypatch)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    [(loaded, error)] = calls
    assert loaded.config.project == "shop" and error is None


def test_doctor_without_a_catalog_reports_where_it_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_doctor(monkeypatch)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    [(loaded, error)] = calls
    assert loaded is None
    assert error == (
        f"no catalog at {tmp_path / 'wt-home' / 'catalog.yaml'}. "
        "Run wt init, or wt migrate <worktree.yaml>"
    )


def test_doctor_reports_an_invalid_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = tmp_path / "c.yaml"
    broken.write_text(CATALOG.replace("domain: notes.localhost\n", ""))
    calls = _record_doctor(monkeypatch)
    runner.invoke(app, ["-c", str(broken), "doctor"])
    loaded, error = calls[0]
    assert loaded is None
    assert "missing required key(s): domain" in error


def test_catalog_without_a_file_fails_with_its_path(tmp_path: Path) -> None:
    result = runner.invoke(app, ["catalog"])
    assert isinstance(result.exception, WtError)
    assert str(tmp_path / "wt-home" / "catalog.yaml") in str(result.exception)


def test_catalog_check_runs_each_service_through_load_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[list[str] | None] = []
    real = load_project

    def spy(path: Path, uses: list[str] | None = None) -> Any:
        asked.append(uses)
        return real(path, uses=uses)

    monkeypatch.setattr(cli, "load_project", spy)
    result = _check_catalog(tmp_path, monkeypatch, CATALOG)
    assert result.exit_code == 0, result.output
    assert asked == [["back"], ["front"]]


def test_catalog_check_needs_a_project_and_a_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _check_catalog(tmp_path, monkeypatch, CATALOG.replace("project: notes\n", ""))
    assert result.exit_code == 1, result.output
    assert "missing required key(s): project" in result.output


# --- --json (SPEC §21) ---------------------------------------------------------------------

BACK_URL = "https://back.feat-cart.shop.localhost"
FRONT_URL = "https://front.feat-cart.shop.localhost"
SERVICE_KEYS = {"name", "kind", "status", "health", "url", "port"}
ACTION_KEYS = ["schema", "branch", "action", "status", "urls", "warnings"]
NO_CATALOG_COMMANDS = [
    ["ls"],
    ["catalog"],
    ["add", "feat/cart", "--uses", "front"],
    ["up", "feat/cart", "--uses", "front"],
    ["down", "feat/cart"],
    ["stop", "feat/cart"],
    ["remove", "feat/cart", "--yes"],
]


def _document(result: Any) -> dict[str, Any]:
    """The one JSON document on stdout; json.loads rejects anything printed around it."""
    document: dict[str, Any] = json.loads(result.stdout)
    return document


def _stub_doctor(monkeypatch: pytest.MonkeyPatch, checks: list[doctor.Check]) -> None:
    monkeypatch.setattr(doctor, "run_checks", lambda loaded, config_error, *, fix: checks)


def test_ls_json_of_a_running_branch(shop, docker: FakeDocker) -> None:
    shop()
    assert runner.invoke(app, ["up", "feat/cart", "--uses", "front"]).exit_code == 0
    result = runner.invoke(app, ["ls", "--json"])
    assert result.exit_code == 0, result.output
    document = _document(result)
    assert list(document) == ["schema", "project", "branches"]
    assert document["schema"] == 1 and document["project"] == "shop"
    [branch] = document["branches"]
    assert list(branch) == ["branch", "status", "services"]
    assert branch["branch"] == "feat/cart" and branch["status"] == "up"
    services = {svc["name"]: svc for svc in branch["services"]}
    assert set(services) == {"back", "front"}  # the dependency came along
    assert all(set(svc) == SERVICE_KEYS for svc in services.values())
    assert services["back"] == {
        "name": "back",
        "kind": "compose",
        "status": "up",
        "health": "ok",
        "url": BACK_URL,
        "port": None,
    }
    assert services["front"]["url"] == FRONT_URL


def test_ls_json_no_probe_has_no_health(shop, docker: FakeDocker) -> None:
    shop()
    runner.invoke(app, ["up", "feat/cart", "--uses", "back"])
    result = runner.invoke(app, ["ls", "--json", "--no-probe"])
    assert result.exit_code == 0, result.output
    [branch] = _document(result)["branches"]
    assert branch["services"] and all(svc["health"] is None for svc in branch["services"])
    assert all(svc["status"] == branch["status"] for svc in branch["services"])


def test_ls_json_of_a_stopped_branch_has_no_health(shop, docker: FakeDocker) -> None:
    shop()
    runner.invoke(app, ["add", "feat/cart", "--uses", "front"])
    result = runner.invoke(app, ["ls", "--json"])
    assert result.exit_code == 0, result.output
    [branch] = _document(result)["branches"]
    assert branch["status"] == "stopped"
    assert branch["services"]
    for svc in branch["services"]:
        assert (svc["status"], svc["health"]) == ("stopped", None)


def test_ls_json_without_branches_is_an_empty_list(shop, docker: FakeDocker) -> None:
    shop()
    result = runner.invoke(app, ["ls", "--json"])
    assert result.exit_code == 0, result.output
    assert _document(result) == {"schema": 1, "project": "shop", "branches": []}


def test_ls_document_maps_probe_outcomes_and_sorts_branches() -> None:
    statuses = [
        BranchStatus(
            "feat/b",
            "degraded",
            1,
            2,
            services=[
                ServiceReport("web", "fail", "HTTP 502", "https://web.b.localhost", "run"),
                ServiceReport("api", "ok", "HTTP 200", "https://api.b.localhost"),
            ],
        ),
        BranchStatus(
            "feat/a",
            "stopped",
            0,
            1,
            services=[ServiceReport("api", "skipped", "", "https://api.a.localhost")],
        ),
    ]
    document = cli.ls_document("shop", statuses, probed=True)
    assert [b["branch"] for b in document["branches"]] == ["feat/a", "feat/b"]
    stopped, degraded = document["branches"]
    assert [(s["status"], s["health"]) for s in stopped["services"]] == [("stopped", None)]
    web, api = degraded["services"]
    assert (web["kind"], web["status"], web["health"]) == ("run", "degraded", "fail")
    assert (api["kind"], api["status"], api["health"]) == ("compose", "up", "ok")
    unprobed = cli.ls_document("shop", statuses, probed=False)
    healths = [s["health"] for b in unprobed["branches"] for s in b["services"]]
    assert healths == [None, None, None]


def test_doctor_json_lists_checks_and_fails_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_doctor(
        monkeypatch,
        [
            doctor.Check("git", "ok", "git 2.45"),
            doctor.Check("mkcert", "warn", "local CA not created", "run `wt doctor --fix`"),
            doctor.Check("docker", "fail", "daemon not running", "start docker"),
        ],
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.output
    assert _document(result) == {
        "schema": 1,
        "ok": False,
        "checks": [
            {"name": "git", "ok": True, "detail": "git 2.45", "fixable": False},
            {"name": "mkcert", "ok": True, "detail": "local CA not created", "fixable": True},
            {"name": "docker", "ok": False, "detail": "daemon not running", "fixable": False},
        ],
    }


def test_doctor_json_when_all_is_well_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_doctor(monkeypatch, [doctor.Check("git", "ok", "git 2.45")])
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    document = _document(result)
    assert document["ok"] is True and list(document) == ["schema", "ok", "checks"]


def test_doctor_json_reports_a_missing_catalog_as_a_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda loaded, config_error, *, fix: [doctor.Check("config", "fail", config_error or "")],
    )
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.output
    [check] = _document(result)["checks"]
    assert check["name"] == "config" and check["ok"] is False
    assert check["detail"].startswith(f"no catalog at {tmp_path / 'wt-home' / 'catalog.yaml'}")


def test_catalog_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "c.yaml").write_text(CATALOG)
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "c.yaml"))
    result = runner.invoke(app, ["catalog", "--json"])
    assert result.exit_code == 0, result.output
    assert _document(result) == {
        "schema": 1,
        "project": "notes",
        "domain": "notes.localhost",
        "services": [
            {
                "name": "back",
                "repo": "notes-back",
                "kind": "compose",
                "infra": ["postgres"],
                "consumes": {},
                "consumed_by": ["front"],
                "remote": "https://api.staging.example",
            },
            {
                "name": "front",
                "repo": "notes-front",
                "kind": "run",
                "infra": [],
                "consumes": {"back": "API_URL"},
                "consumed_by": [],
                "remote": None,
            },
        ],
    }


def test_catalog_json_refuses_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "c.yaml").write_text(CATALOG)
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "c.yaml"))
    result = runner.invoke(app, ["catalog", "--check", "--json"])
    assert result.exit_code == 1, result.output
    document = _document(result)
    assert list(document) == ["schema", "error"] and "--check" in document["error"]


def test_up_json_reports_status_urls_and_warnings(shop, docker: FakeDocker) -> None:
    shop()
    result = runner.invoke(app, ["up", "feat/cart", "--uses", "front", "--json"])
    assert result.exit_code == 0, result.output
    document = _document(result)
    assert list(document) == ACTION_KEYS
    assert document["schema"] == 1
    assert (document["branch"], document["action"], document["status"]) == (
        "feat/cart",
        "up",
        "up",
    )
    assert document["urls"] == {"back": BACK_URL, "front": FRONT_URL}
    assert document["warnings"] == []
    assert docker.ups == ["wt-shop-feat-cart-api"]


def test_up_json_degraded_exits_1_with_the_document(
    shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker.healthy = False
    docker.install(monkeypatch)
    shop()
    result = runner.invoke(app, ["up", "feat/cart", "--uses", "front", "--json"])
    assert result.exit_code == 1, result.output
    document = _document(result)
    assert (document["action"], document["status"]) == ("up", "degraded")
    assert document["warnings"]
    assert all("is not healthy" in warning for warning in document["warnings"])


def test_add_json(shop, docker: FakeDocker) -> None:
    shop()
    result = runner.invoke(app, ["add", "feat/cart", "--uses", "front", "--json"])
    assert result.exit_code == 0, result.output
    document = _document(result)
    assert list(document) == ACTION_KEYS
    assert (document["branch"], document["action"], document["status"]) == (
        "feat/cart",
        "add",
        "stopped",
    )
    assert document["urls"] == {} and document["warnings"] == []
    assert docker.ups == []


def test_add_json_of_a_known_branch_has_no_status(shop, docker: FakeDocker) -> None:
    shop()
    runner.invoke(app, ["add", "feat/cart", "--uses", "back"])
    result = runner.invoke(app, ["add", "feat/cart", "--uses", "front", "--json"])
    assert result.exit_code == 0, result.output
    assert _document(result)["status"] is None
    assert StateStore("shop").load().branches["feat/cart"].uses == ["back"]


def test_add_json_never_prompts_on_a_terminal(shop, on_a_terminal) -> None:
    shop()
    result = runner.invoke(app, ["add", "feat/cart", "--json"], input="1\n")
    assert result.exit_code == 1, result.output
    assert _document(result) == {
        "schema": 1,
        "error": "branch feat/cart has no services yet; run wt add feat/cart --uses a,b",
    }
    assert "Services" not in result.stdout
    assert "feat/cart" not in StateStore("shop").load().branches


def test_stop_json(shop, docker: FakeDocker) -> None:
    shop()
    runner.invoke(app, ["up", "feat/cart", "--uses", "front"])
    result = runner.invoke(app, ["stop", "feat/cart", "--json"])
    assert result.exit_code == 0, result.output
    document = _document(result)
    assert list(document) == ACTION_KEYS
    assert (document["action"], document["status"]) == ("stop", "stopped")
    assert document["urls"] == {} and document["warnings"] == []
    assert docker.kept_downs == ["wt-shop-feat-cart-api"]


def test_down_json(shop, docker: FakeDocker) -> None:
    shop()
    runner.invoke(app, ["up", "feat/cart", "--uses", "front"])
    result = runner.invoke(app, ["down", "feat/cart", "--json"])
    assert result.exit_code == 0, result.output
    document = _document(result)
    assert list(document) == ACTION_KEYS
    assert (document["branch"], document["action"], document["status"]) == (
        "feat/cart",
        "down",
        None,
    )
    assert docker.downs == ["wt-shop-feat-cart-api"]


def test_down_json_of_an_unknown_branch_still_prints_the_document(shop, docker: FakeDocker) -> None:
    shop()
    result = runner.invoke(app, ["down", "feat/nope", "--json"])
    assert result.exit_code == 0, result.output
    assert _document(result)["action"] == "down"


def test_remove_json_with_yes(shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
    shop()
    calls = _record_removes(monkeypatch)
    result = runner.invoke(app, ["remove", "feat/cart", "--yes", "--force", "--json"])
    assert result.exit_code == 0, result.output
    document = _document(result)
    assert list(document) == ACTION_KEYS
    assert (document["action"], document["status"]) == ("remove", None)
    assert calls == [("feat/cart", True)]


def test_remove_json_without_yes_is_an_error_and_never_asks(
    shop, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop()
    calls = _record_removes(monkeypatch)
    result = runner.invoke(app, ["remove", "feat/cart", "--json"], input="y\n")
    assert result.exit_code == 1, result.output
    document = _document(result)
    assert list(document) == ["schema", "error"] and "--yes" in document["error"]
    assert "Remove?" not in result.stdout
    assert calls == []


def test_json_error_replaces_the_traceback_free_message(shop) -> None:
    shop()
    result = runner.invoke(app, ["up", "feat/cart", "--json"])
    assert result.exit_code == 1, result.output
    assert _document(result) == {
        "schema": 1,
        "error": "branch feat/cart has no services yet; run wt add feat/cart --uses a,b",
    }


@pytest.mark.parametrize("args", NO_CATALOG_COMMANDS, ids=lambda args: args[0])
def test_json_error_text_is_the_one_the_cli_prints(args: list[str], tmp_path: Path) -> None:
    plain = runner.invoke(app, args)
    assert isinstance(plain.exception, WtError)
    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == 1, result.output
    assert _document(result) == {"schema": 1, "error": str(plain.exception)}
    assert str(tmp_path / "wt-home" / "catalog.yaml") in str(plain.exception)


def test_json_mode_sends_progress_to_stderr(shop, monkeypatch: pytest.MonkeyPatch) -> None:
    shop()
    monkeypatch.setattr(Orchestrator, "stop", lambda self, branch: self.log("stopping now"))
    result = runner.invoke(app, ["stop", "feat/cart", "--json"])
    assert result.exit_code == 0, result.output
    assert _document(result)["action"] == "stop"
    assert "stopping now" in result.stderr
    assert "stopping now" not in result.stdout


def test_without_json_progress_stays_on_stdout(shop, monkeypatch: pytest.MonkeyPatch) -> None:
    shop()
    monkeypatch.setattr(Orchestrator, "stop", lambda self, branch: self.log("stopping now"))
    result = runner.invoke(app, ["stop", "feat/cart"])
    assert result.exit_code == 0, result.output
    assert "stopping now" in result.stdout
    assert "stopping now" not in result.stderr


def test_action_document_defaults() -> None:
    assert cli.action_document("feat/x", "down", None) == {
        "schema": 1,
        "branch": "feat/x",
        "action": "down",
        "status": None,
        "urls": {},
        "warnings": [],
    }
