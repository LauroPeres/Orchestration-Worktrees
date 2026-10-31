import os
import shutil
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import (
    _BASES_SEEN,
    _PROBED_WINDOWS,
    COMPOSE,
    LOWEST_EPHEMERAL_PORT,
    PORT_WINDOW,
    PORT_WINDOW_FLOOR,
    SHARED,
    WINDOW_TRIES,
    FakeDocker,
    FakeProvider,
    _pick_free_window,
    _window_is_free,
)
from tests.conftest import git as git_run
from wt import compose, git, health, host, naming, proc, router, tls
from wt.config import load_config
from wt.errors import WtError
from wt.orchestrator import Orchestrator, ServiceReport
from wt.providers.base import Check
from wt.state import ProcessState


def test_up_creates_everything(orch, docker: FakeDocker, isolated_home: Path) -> None:
    o = orch()
    reports = o.up("feat/cart")
    assert {r.service: r.outcome for r in reports} == {"api": "ok", "db": "ok"}
    assert docker.ups == ["wt-shop-feat-cart-api"]

    worktree = isolated_home / "shop" / "worktrees" / "feat-cart" / "api"
    compose_file, override = docker.files[0]
    assert compose_file == worktree / "docker-compose.yml"
    ov = override.read_text()
    assert "URL: https://api.feat-cart.shop.localhost" in ov
    assert "wt-shop-feat-cart-api" in ov

    route = isolated_home / "traefik" / "dynamic" / "shop--feat-cart.yml"
    assert "http://wt-shop-feat-cart-api:8000" in route.read_text()

    bs = o.store.load().branches["feat/cart"]
    assert bs.status == "up"
    assert bs.repos["api"].created and bs.repos["api"].branch_created
    assert bs.compose_projects == {"api": "wt-shop-feat-cart-api"}


def test_up_twice_is_safe(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/cart")
    o.up("feat/cart")
    bs = o.store.load().branches["feat/cart"]
    assert bs.repos["api"].created and bs.repos["api"].branch_created
    assert docker.ups == ["wt-shop-feat-cart-api"] * 2


def test_template_error_creates_nothing(orch, docker: FakeDocker, isolated_home: Path) -> None:
    o = orch(env={"DB": "{{ nope.x }}"})
    with pytest.raises(WtError, match=r"'nope' is undefined"):
        o.up("feat/cart")
    assert o.store.load().branches == {}
    assert not (isolated_home / "shop" / "worktrees").exists()


def test_slug_collision(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/cart")
    with pytest.raises(WtError, match="same name"):
        o.up("feat-cart")


def test_unhealthy_is_degraded(orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
    docker.healthy = False
    docker.install(monkeypatch)
    o = orch()
    reports = o.up("feat/cart")
    assert {r.service: r.outcome for r in reports}["api"] == "fail"
    assert o.store.load().branches["feat/cart"].status == "degraded"


def test_missing_compose_service(orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compose, "list_services", lambda f: ["web"])
    with pytest.raises(WtError, match="has no service 'api'"):
        orch().up("feat/cart")


def test_down_cleans_everything(orch, docker: FakeDocker, isolated_home: Path) -> None:
    o = orch()
    o.up("feat/cart")
    o.down("feat/cart")
    assert docker.downs == ["wt-shop-feat-cart-api"]
    assert o.store.load().branches == {}
    assert not (isolated_home / "shop" / "worktrees" / "feat-cart").exists()
    assert not (isolated_home / "shop" / "branches" / "feat-cart").exists()
    assert not list((isolated_home / "traefik" / "dynamic").iterdir())
    o.down("feat/cart")  # idempotent


def test_down_refuses_dirty_worktree(orch, docker: FakeDocker, isolated_home: Path) -> None:
    o = orch()
    o.up("feat/cart")
    worktree = isolated_home / "shop" / "worktrees" / "feat-cart" / "api"
    (worktree / "wip.txt").write_text("x")
    with pytest.raises(WtError, match="untracked"):
        o.down("feat/cart")
    assert docker.downs == []  # nothing was touched
    o.down("feat/cart", force=True)
    assert not worktree.exists()


def test_down_never_removes_a_checkout_wt_did_not_create(
    orch, docker: FakeDocker, tmp_path: Path
) -> None:
    o = orch()
    o.up("main")
    assert not o.store.load().branches["main"].repos["api"].created
    o.down("main")
    assert (tmp_path / "shop-api" / "docker-compose.yml").exists()


def test_down_after_failed_up(orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
    docker.fail_up = True
    o = orch()
    with pytest.raises(WtError, match="compose up failed"):
        o.up("feat/cart")
    bs = o.store.load().branches["feat/cart"]
    assert bs.status == "failed"
    assert bs.compose_projects == {"api": "wt-shop-feat-cart-api"}
    o.down("feat/cart")
    assert docker.downs == ["wt-shop-feat-cart-api"]
    assert o.store.load().branches == {}


def test_ls(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/cart")
    [row] = o.ls()
    assert row.branch == "feat/cart"
    assert row.running == 1 and row.total == 1
    assert [(s.service, s.url, s.outcome) for s in row.services] == [
        ("api", "https://api.feat-cart.shop.localhost", "ok")
    ]


def test_ls_reports_kind_and_databases(orch, fake_provider, docker: FakeDocker) -> None:
    o = orch(shared=SHARED)
    reports = o.up("feat/cart")
    assert {r.service: r.kind for r in reports} == {"api": "compose", "db": "compose"}
    [row] = o.ls()
    assert [s.kind for s in row.services] == ["compose"]
    assert row.databases == ["db_feat_cart"]
    assert row.docker_ok


def test_ls_marks_docker_unavailable(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = orch()
    o.up("feat/cart")

    def broken(project: str) -> list[compose.Container]:
        raise WtError("docker is not reachable")

    monkeypatch.setattr(compose, "ps", broken)
    [row] = o.ls(probe=False)
    assert not row.docker_ok and row.total == 0
    assert [s.service for s in row.services] == ["api"]


def test_run_service_row_has_kind_run(run_orch) -> None:
    o = run_orch(run_service(subdomain="web", health="/"))
    reports = {r.service: r for r in o.up("main")}
    assert reports["web"].kind == "run"
    [row] = o.ls(probe=False)
    assert {s.service: s.kind for s in row.services}["web"] == "run"


def test_custom_https_port_in_urls(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WT_HTTPS_PORT", "8443")
    o = orch()
    reports = o.up("feat/cart")
    assert reports[0].url == "https://api.feat-cart.shop.localhost:8443"


def test_provider_context_urls_default_port(orch) -> None:
    o = orch()
    ctx = o.provider_context(o.names("feat/cart"))
    assert ctx.urls == ("https://api.feat-cart.shop.localhost",)


def test_provider_context_urls_include_custom_port(orch, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_HTTPS_PORT", "8443")
    o = orch()
    ctx = o.provider_context(o.names("feat/cart"))
    assert ctx.urls == ("https://api.feat-cart.shop.localhost:8443",)


def test_provider_context_urls_skip_unrouted_services(orch) -> None:
    o = orch()  # `db` has no subdomain
    ctx = o.provider_context(o.names("feat/cart"))
    assert all("db." not in u for u in ctx.urls) and len(ctx.urls) == 1


def test_provider_context_urls_skip_unselected_services(
    catalog_orch: Orchestrator, docker: FakeDocker
) -> None:
    o = catalog_orch
    names = o.names("feat/x")
    assert len(o.provider_context(names).urls) == 2
    o.up("feat/x")
    _set_uses(o, "feat/x", ["db"])
    narrowed = o._for("feat/x")
    assert narrowed.provider_context(names).urls == ("https://dbx.feat-x.shop.localhost",)


def test_logs_errors(orch, docker: FakeDocker) -> None:
    o = orch()
    with pytest.raises(WtError, match="unknown service"):
        o.logs("feat/cart", "nope")
    with pytest.raises(WtError, match="not up"):
        o.logs("feat/cart", "api")


def test_logs_of_run_service_tails_the_log(run_orch, monkeypatch: pytest.MonkeyPatch) -> None:
    o = run_orch()
    o.up("main")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kw: object) -> proc.Result:
        calls.append((args, kw))
        return proc.Result(0, "", "")

    monkeypatch.setattr(proc, "run", fake_run)
    log = o.store.load().branches["main"].processes["web"].log
    o.logs("main", "web", follow=True, tail=7)
    o.logs("main", "web", follow=False, tail=3)
    assert calls[0][0] == ["tail", "-n", "7", "-f", log]
    assert calls[1][0] == ["tail", "-n", "3", log]
    assert calls[0][1].get("capture") is False


def test_log_command_for_run_and_compose_services(run_orch, orch, docker: FakeDocker) -> None:
    o = run_orch()
    o.up("main")
    log = o.store.load().branches["main"].processes["web"].log
    assert o.log_command("main", "web", follow=True, tail=5) == ["tail", "-n", "5", "-f", log]
    c = orch()
    c.up("feat/cart")
    args = c.log_command("feat/cart", "api", follow=False, tail=9)
    assert args[:2] == ["docker", "compose"]
    assert args[-4:-1] == ["logs", "--tail", "9"]
    assert args[-1] == "api"
    assert "-f" not in c.log_command("feat/cart", "api", follow=False, tail=9)
    assert "-f" in c.log_command("feat/cart", "api", follow=True, tail=9)


def test_logs_of_run_service_without_log_file(run_orch) -> None:
    o = run_orch()
    o.up("main")
    Path(o.store.load().branches["main"].processes["web"].log).unlink()
    with pytest.raises(WtError, match="no log yet for web on main"):
        o.logs("main", "web")


def test_failed_up_shows_in_ls(orch, docker: FakeDocker) -> None:
    docker.fail_up = True
    o = orch()
    with pytest.raises(WtError):
        o.up("feat/cart")
    assert [r.status for r in o.ls(probe=False)] == ["failed"]


def test_up_without_routes_removes_old_route_file(
    orch, docker: FakeDocker, isolated_home: Path
) -> None:
    o = orch()
    o.up("feat/cart")
    route = isolated_home / "traefik" / "dynamic" / "shop--feat-cart.yml"
    assert route.exists()
    o.cfg.services["api"].subdomain = None
    o.up("feat/cart")
    assert not route.exists()
    assert o.store.load().branches["feat/cart"].route_file is None


def test_untrusted_ca_warns(orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
    cert = (Path("c.pem"), Path("k.pem"))
    monkeypatch.setattr(tls, "ensure_cert", lambda names: cert)
    monkeypatch.setattr(tls, "ca_ready", lambda: False)
    o = orch()
    logs: list[str] = []
    o.log = logs.append
    o.up("feat/cart")
    assert any("wt doctor --fix" in m for m in logs)


def test_up_announces_traefik_only_when_starting(orch, docker: FakeDocker) -> None:
    o = orch()
    logs: list[str] = []
    o.log = logs.append
    o.up("feat/cart")
    assert any("starting shared Traefik" in m for m in logs)

    docker.traefik_running = True
    logs.clear()
    o.up("feat/other")
    assert not any("starting shared Traefik" in m for m in logs)


def test_down_unknown_branch(orch, docker: FakeDocker) -> None:
    o = orch()
    logs: list[str] = []
    o.log = logs.append
    assert o.down("feat/nope") is False
    assert logs == ["branch feat/nope not found; nothing to remove"]
    assert docker.downs == []


def test_down_cleans_leftovers_when_state_was_lost(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/x")
    route = router.route_file(o.names("feat/x"))
    assert route.exists()
    o.store.path.unlink()  # state lost, route file still on disk
    names = o.names("feat/x")
    for repo in o.cfg.repos:
        shutil.rmtree(o.worktree_target(names, repo), ignore_errors=True)
    shutil.rmtree(o.branch_dir(names), ignore_errors=True)
    o.down("feat/x", force=True)
    assert not route.exists()


@pytest.mark.parametrize("force", [False, True])
def test_down_leaves_a_folder_that_is_not_a_worktree(orch, docker: FakeDocker, force: bool) -> None:
    o = orch()
    logs: list[str] = []
    o.log = logs.append
    names = o.names("feat/x")
    folder = o.worktree_target(names, "api")
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("mine")  # not made by wt, not a git worktree
    o.down("feat/x", force=force)  # nothing of wt to remove: no error
    assert (folder / "notes.txt").read_text() == "mine"
    assert any("not a worktree" in m for m in logs)


def test_down_refuses_slug_owned_by_another_branch(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/x")
    with pytest.raises(WtError, match="same name"):
        o.down("feat-x")
    assert docker.downs == []
    assert "feat/x" in o.store.load().branches


def test_down_interrupted_after_compose_down_can_be_rerun(
    orch, docker: FakeDocker, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = orch()
    o.up("feat/cart")
    real = git.remove_worktree
    calls: list[int] = []

    def flaky(repo: Path, path: Path, *, force: bool) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise WtError("interrupted")
        real(repo, path, force=force)

    monkeypatch.setattr(git, "remove_worktree", flaky)
    with pytest.raises(WtError, match="interrupted"):
        o.down("feat/cart")
    assert o.store.load().branches["feat/cart"].status == "removing"
    o.down("feat/cart")
    assert o.store.load().branches == {}
    assert not (isolated_home / "shop" / "worktrees" / "feat-cart").exists()


def test_down_force_after_worktree_deleted_by_hand(
    orch, docker: FakeDocker, isolated_home: Path
) -> None:
    o = orch()
    o.up("feat/cart")
    worktree = isolated_home / "shop" / "worktrees" / "feat-cart" / "api"
    shutil.rmtree(worktree)
    o.down("feat/cart", force=True)
    assert o.store.load().branches == {}
    assert not worktree.exists()
    assert docker.downs == ["wt-shop-feat-cart-api"]


def test_concurrent_up_of_two_branches(orch, docker: FakeDocker) -> None:
    o = orch()
    errors: list[BaseException] = []

    def run(branch: str) -> None:
        try:
            o.up(branch)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(b,)) for b in ("feat/a", "feat/b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    branches = o.store.load().branches
    assert {b: s.status for b, s in branches.items()} == {"feat/a": "up", "feat/b": "up"}
    assert all(s.repos["api"].created for s in branches.values())
    assert sorted(docker.ups) == ["wt-shop-feat-a-api", "wt-shop-feat-b-api"]


# ---- providers ------------------------------------------------------------------------------


def test_provider_call_order(orch, fake_provider: FakeProvider) -> None:
    o = orch(shared=SHARED)
    o.up("feat/cart")
    assert fake_provider.events == ["ensure", "create", "verify", "compose up"]
    o.down("feat/cart")
    assert fake_provider.events[4:] == ["compose down", "ensure", "destroy"]


def test_down_cannot_start_server_keeps_state_for_rerun(
    orch, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = orch(shared=SHARED)
    o.up("feat/cart")

    def broken() -> None:
        raise WtError("command failed (1): docker compose up")

    working = fake_provider.ensure_server
    monkeypatch.setattr(fake_provider, "ensure_server", broken)
    with pytest.raises(WtError, match="wt doctor --fix") as info:
        o.down("feat/cart")
    assert "wt down feat/cart" in str(info.value)
    assert "destroy" not in fake_provider.events
    assert o.store.load().branches["feat/cart"].providers == {"postgres": ["db_feat_cart"]}
    monkeypatch.setattr(fake_provider, "ensure_server", working)
    o.down("feat/cart")
    assert o.store.load().branches == {}


def test_provider_state_is_recorded_and_cleared(orch, fake_provider: FakeProvider) -> None:
    o = orch(shared=SHARED)
    o.up("feat/cart")
    assert o.store.load().branches["feat/cart"].providers == {"postgres": ["db_feat_cart"]}
    o.down("feat/cart")
    assert o.store.load().branches == {}


def test_provider_name_is_recorded_before_create(orch, fake_provider: FakeProvider) -> None:
    fake_provider.fail_create = True
    o = orch(shared=SHARED)
    with pytest.raises(WtError, match="create failed"):
        o.up("feat/cart")
    bs = o.store.load().branches["feat/cart"]
    assert bs.status == "failed"
    assert bs.providers == {"postgres": []}
    assert "compose up" not in fake_provider.events
    o.down("feat/cart")
    assert fake_provider.events[-1] == "destroy"
    assert o.store.load().branches == {}


def test_second_up_is_idempotent(orch, fake_provider: FakeProvider) -> None:
    o = orch(shared=SHARED)
    o.up("feat/cart")
    o.up("feat/cart")
    bs = o.store.load().branches["feat/cart"]
    assert bs.providers == {"postgres": ["db_feat_cart"]}
    assert bs.status == "up"
    assert fake_provider.events.count("destroy") == 0


def test_failed_verify_fails_up(orch, fake_provider: FakeProvider, docker: FakeDocker) -> None:
    fake_provider.check = Check(False, "row counts differ", [])
    o = orch(shared=SHARED)
    with pytest.raises(WtError, match="row counts differ"):
        o.up("feat/cart")
    assert o.store.load().branches["feat/cart"].status == "failed"
    assert docker.ups == []


def test_verify_warnings_are_logged(orch, fake_provider: FakeProvider) -> None:
    fake_provider.check = Check(True, "ok", ["base db was empty"])
    logs: list[str] = []
    o = orch(shared=SHARED)
    o.log = logs.append
    o.up("feat/cart")
    assert any("base db was empty" in m for m in logs)


def test_template_vars_per_kind(orch, fake_provider: FakeProvider, docker: FakeDocker) -> None:
    o = orch(
        env={"DB": "{{ pg.url }}"},
        shared=SHARED,
        extra_services={
            "web": {"repo": "api", "run": "serve", "env": {"DB": "{{ pg.url }}"}},
        },
    )
    o.up("feat/cart")
    assert set(fake_provider.kinds) == {"compose", "run"}
    assert "DB: compose://feat_cart@main" in docker.files[0][1].read_text()
    o.down("feat/cart")  # stop the run service it started


def test_base_branch_comes_from_config(
    orch, fake_provider: FakeProvider, docker: FakeDocker
) -> None:
    o = orch(
        env={"DB": "{{ pg.url }}"},
        shared={"postgres": {"databases": ["shop"], "base_branch": "develop"}},
    )
    o.up("feat/cart")
    assert "@develop" in docker.files[0][1].read_text()


def test_provider_template_error_creates_nothing(orch, fake_provider: FakeProvider) -> None:
    o = orch(env={"DB": "{{ pg.nope }}"}, shared=SHARED)
    with pytest.raises(WtError, match="nope"):
        o.up("feat/cart")
    assert fake_provider.events == []
    assert o.store.load().branches == {}


def test_provider_makes_every_service_join_the_network(
    orch, fake_provider: FakeProvider, docker: FakeDocker
) -> None:
    o = orch(shared=SHARED)
    o.up("feat/cart")
    parsed = docker.files[0][1].read_text()
    assert parsed.count("wt: {}") == 1  # db has no alias but joins; api has an alias
    assert "name: wt" in parsed


def test_no_provider_keeps_unrouted_services_off_the_network(orch, docker: FakeDocker) -> None:
    orch().up("feat/cart")
    assert "wt: {}" not in docker.files[0][1].read_text()


def test_override_file_is_private(orch, fake_provider: FakeProvider, docker: FakeDocker) -> None:
    orch(shared=SHARED).up("feat/cart")
    assert docker.files[0][1].stat().st_mode & 0o777 == 0o600


def test_note_only_for_unregistered_providers(
    orch, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wt import providers

    monkeypatch.delitem(providers.REGISTRY, "kafka")
    logs: list[str] = []
    o = orch(shared={**SHARED, "kafka": {"groups": ["g"]}})
    o.log = logs.append
    o.up("feat/cart")
    notes = [m for m in logs if "not available" in m]
    assert len(notes) == 1 and "kafka" in notes[0] and "postgres" not in notes[0]


def test_no_note_when_all_providers_are_registered(orch, fake_provider: FakeProvider) -> None:
    logs: list[str] = []
    o = orch(shared=SHARED)
    o.log = logs.append
    o.up("feat/cart")
    assert not any("not available" in m for m in logs)


def test_down_lists_resources_of_unavailable_provider(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/x")
    with o.store.edit() as state:
        state.branches["feat/x"].providers["gone"] = ["db__feat_x"]
    logs: list[str] = []
    o.log = logs.append
    o.down("feat/x")
    assert any("'gone'" in line and "db__feat_x" in line for line in logs)


# ---- host processes (run: services) ------------------------------------------------------

SERVE = Path(__file__).parent / "serve.py"
SERVER = f"{sys.executable} {SERVE} {{{{ port }}}}"


def run_service(**extra: object) -> dict[str, object]:
    return {"web": {"repo": "api", "run": SERVER, "port": "auto", **extra}}


@pytest.fixture
def run_orch(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Orchestrator]]:
    built: list[Orchestrator] = []
    launched: list[ProcessState] = []
    real_start = host.start

    def recording_start(*args: Any, **kwargs: Any) -> ProcessState:
        ps = real_start(*args, **kwargs)
        launched.append(ps)
        return ps

    monkeypatch.setattr(host, "start", recording_start)

    def build(services: dict[str, object] | None = None, **kwargs: Any) -> Orchestrator:
        o: Orchestrator = orch(extra_services=services or run_service(), **kwargs)
        built.append(o)
        return o

    yield build
    recorded = [
        ps for o in built for bs in o.store.load().branches.values() for ps in bs.processes.values()
    ]
    stop_errors: list[str] = []
    for ps in [*recorded, *launched]:  # one failing stop must not leave the others running
        try:
            host.stop(ps, grace=0.2)
        except OSError as error:
            stop_errors.append(f"pid {ps.pid}: {error}")
    # `stop` waits for the process group to vanish, not for its listening socket to close.
    open_ports = [ps.port for ps in launched if ps.port is not None and not port_closes(ps.port)]
    assert not stop_errors, f"host.stop failed: {stop_errors}"
    assert not open_ports, f"ports still answer after stop: {open_ports}"


def wait_until(check: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


LOG_TAIL_LINES = 20


def why(o: Orchestrator, branch: str, reports: list[ServiceReport]) -> str:
    """Assertion message for a run service that did not come up: report details and log tails."""
    lines = [f"{r.service}: {r.outcome} - {r.detail}" for r in reports]
    state = o.store.load().branches.get(branch)
    for name, ps in (state.processes if state else {}).items():
        log = Path(ps.log)
        tail = (
            log.read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:]
            if log.is_file()
            else ["<no log>"]
        )
        lines += [f"--- {name} log ---", *tail]
    return "\n".join(lines)


def port_closes(port: int) -> bool:
    return wait_until(lambda: not host.tcp_ok(port))


def test_run_service_is_started_and_recorded(run_orch, isolated_home: Path) -> None:
    o = run_orch()
    done = o.up("feat/cart")
    reports = {r.service: r for r in done}
    assert reports["web"].outcome == "ok", why(o, "feat/cart", done)  # unrouted: TCP connect
    ps = o.store.load().branches["feat/cart"].processes["web"]
    assert host.alive(ps)
    assert ps.port == naming.host_port(0, 2)  # index 0, third service
    assert ps.log == str(isolated_home / "shop" / "branches" / "feat-cart" / "logs" / "web.log")
    assert Path(ps.log).is_file()
    assert f"serve.py {ps.port}" in ps.command


def test_run_service_runs_in_worktree_with_rendered_env(run_orch, isolated_home: Path) -> None:
    services = {
        "job": {
            "repo": "api",
            "run": 'pwd; echo "branch=$B port=$P"; sleep 60',
            "env": {"B": "{{ branch }}", "P": "{{ services.job.port }}"},
        }
    }
    o = run_orch(services)
    done = o.up("feat/cart")
    reports = {r.service: r for r in done}
    assert reports["job"].outcome == "ok", why(o, "feat/cart", done)  # no port: alive
    ps = o.store.load().branches["feat/cart"].processes["job"]
    assert ps.port is None
    worktree = isolated_home / "shop" / "worktrees" / "feat-cart" / "api"
    assert wait_until(lambda: "branch=" in Path(ps.log).read_text())
    log = Path(ps.log).read_text()
    assert str(worktree.resolve()) in log
    assert "branch=feat/cart port=" in log
    assert os.environ["PATH"]  # the parent environment is inherited (sh and sleep were found)


def test_port_is_rendered_into_env_and_command(run_orch) -> None:
    services = run_service(env={"SELF": "{{ port }}", "SAME": "{{ services.web.port }}"})
    o = run_orch(services)
    o.up("main")
    port = o.store.load().branches["main"].processes["web"].port
    assert port is not None
    cmd = o.store.load().branches["main"].processes["web"].command
    assert f"serve.py {port}" in cmd


def test_up_twice_does_not_restart_a_live_process(run_orch) -> None:
    o = run_orch()
    o.up("main")
    first = o.store.load().branches["main"].processes["web"]
    o.up("main")
    assert o.store.load().branches["main"].processes["web"] == first
    assert host.alive(first)


def test_up_restarts_a_dead_process_on_the_same_port(run_orch) -> None:
    o = run_orch()
    o.up("main")
    first = o.store.load().branches["main"].processes["web"]
    host.stop(first, grace=0.2)
    o.up("main")
    second = o.store.load().branches["main"].processes["web"]
    assert second.port == first.port
    assert second.pid != first.pid
    assert host.alive(second)


@pytest.mark.parametrize(
    "changed",
    [
        run_service(env={"MODE": "two"}),  # env changed
        {"web": {"repo": "api", "run": SERVER + " --x", "port": "auto", "env": {"MODE": "one"}}},
    ],
    ids=["env", "command"],
)
def test_up_restarts_a_live_process_whose_run_or_env_changed(
    run_orch, monkeypatch: pytest.MonkeyPatch, changed: dict[str, object]
) -> None:
    o = run_orch(run_service(env={"MODE": "one"}))
    o.up("main")
    first = o.store.load().branches["main"].processes["web"]
    o2 = run_orch(changed)
    o2.up("main")
    second = o2.store.load().branches["main"].processes["web"]
    assert second.pid != first.pid
    assert not host.alive(first)
    assert host.alive(second)
    assert second.port == first.port


def test_process_from_older_state_without_env_hash_is_kept(
    run_orch, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = run_orch()
    o.up("main")
    with o.store.edit() as state:
        state.branches["main"].processes["web"].env_hash = None
    first = o.store.load().branches["main"].processes["web"]
    o.up("main")
    assert o.store.load().branches["main"].processes["web"].pid == first.pid


def test_two_worktrees_get_index_0_and_1_and_formula_ports(run_orch) -> None:
    o = run_orch()
    o.up("main")
    o.up("feat/cart")
    branches = o.store.load().branches
    assert (branches["main"].index, branches["feat/cart"].index) == (0, 1)
    assert branches["main"].processes["web"].port == naming.host_port(0, 2)
    assert branches["feat/cart"].processes["web"].port == naming.host_port(1, 2)
    assert branches["feat/cart"].ports["api"] == naming.host_port(1, 0)


def test_stop_keeps_the_index_and_down_frees_it(run_orch) -> None:
    o = run_orch()
    o.add("main")
    o.add("feat/cart")
    o.stop("main")
    assert o.store.load().branches["main"].index == 0
    o.add("feat/other")
    assert o.store.load().branches["feat/other"].index == 2
    assert o.down("main")
    o.add("feat/new")
    assert o.store.load().branches["feat/new"].index == 0


def test_remove_frees_the_index(run_orch) -> None:
    o = run_orch()
    o.add("main")
    o.add("feat/cart")
    assert o.remove("feat/cart")
    o.add("feat/new")
    assert o.store.load().branches["feat/new"].index == 1


def test_a_branch_without_an_index_gets_one_on_up(run_orch) -> None:
    o = run_orch()
    o.add("main")
    with o.store.edit() as state:
        state.branches["main"].index = None
    o.up("main")
    assert o.store.load().branches["main"].index == 0


def test_busy_formula_port_fails_with_the_message(run_orch, docker: FakeDocker) -> None:
    port = naming.host_port(0, 2)
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        o = run_orch()
        with pytest.raises(WtError, match=rf"port {port} for service 'web' \(worktree index 0\)"):
            o.up("main")
    assert docker.ups == []
    assert "main" not in o.store.load().branches
    o.up("main")  # the index was released, the port is free again
    assert o.store.load().branches["main"].index == 0


def test_compose_service_with_a_port_is_published_on_loopback(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/cart")
    override = docker.files[0][1].read_text()
    assert f"127.0.0.1:{naming.host_port(0, 0)}:8000" in override
    assert o.store.load().branches["feat/cart"].ports == {"api": naming.host_port(0, 0)}


def test_template_port_of_a_compose_service_stays_the_container_port(
    orch, docker: FakeDocker
) -> None:
    o = orch(env={"P": "{{ port }}"})
    o.up("feat/cart")
    assert "P: '8000'" in docker.files[0][1].read_text()


def test_explicit_port_in_use_fails_before_anything_is_created(
    run_orch, docker: FakeDocker, isolated_home: Path
) -> None:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        s.listen()
        port = int(s.getsockname()[1])
        o = run_orch(run_service(port=port))
        with pytest.raises(WtError, match="in use"):
            o.up("main")
    assert docker.ups == []
    assert not (isolated_home / "shop" / "worktrees").exists()


def test_routed_run_service_goes_through_host_gateway(run_orch, isolated_home: Path) -> None:
    o = run_orch(run_service(subdomain="web"))
    reports = {r.service: r for r in o.up("main")}
    assert reports["web"].url == "https://web.main.shop.localhost"
    port = o.store.load().branches["main"].processes["web"].port
    route = (isolated_home / "traefik" / "dynamic" / "shop--main.yml").read_text()
    assert f"http://host.docker.internal:{port}" in route
    assert "http://wt-shop-main-api:8000" in route


def test_dead_process_is_reported_as_failed(run_orch) -> None:
    o = run_orch({"web": {"repo": "api", "run": "exit 3", "port": "auto"}})
    reports = {r.service: r for r in o.up("main", timeout=1)}
    assert reports["web"].outcome == "fail"
    assert o.store.load().branches["main"].status == "degraded"


def test_up_order_with_processes(run_orch, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    real_start, real_write = host.start, router.write_routes
    real_up, real_down, real_stop = compose.up, compose.down, host.stop

    def start(*a: Any, **k: Any) -> ProcessState:
        events.append("process start")
        return real_start(*a, **k)

    def write(*a: Any, **k: Any) -> Path:
        events.append("routes")
        return real_write(*a, **k)

    monkeypatch.setattr(host, "start", start)
    monkeypatch.setattr(router, "write_routes", write)

    def up(*a: Any, **k: Any) -> None:
        events.append("compose up")
        real_up(*a, **k)

    def down(*a: Any, **k: Any) -> None:
        events.append("compose down")
        real_down(*a, **k)

    def stop(*a: Any, **k: Any) -> None:
        events.append("process stop")
        real_stop(*a, **k)

    monkeypatch.setattr(compose, "up", up)
    monkeypatch.setattr(compose, "down", down)
    monkeypatch.setattr(host, "stop", stop)
    o = run_orch(run_service(subdomain="web"))
    o.up("main")
    assert events == ["compose up", "process start", "routes"]
    events.clear()
    o.down("main")
    assert events == ["process stop", "compose down"]


def test_down_stops_the_process_and_clears_state(run_orch) -> None:
    o = run_orch()
    o.up("main")
    ps = o.store.load().branches["main"].processes["web"]
    assert o.down("main")
    assert not host.alive(ps)
    assert o.store.load().branches == {}


def test_down_stops_a_process_of_a_service_removed_from_config(run_orch) -> None:
    o = run_orch()
    o.up("main")
    ps = o.store.load().branches["main"].processes["web"]
    del o.cfg.services["web"]
    o.down("main")
    assert not host.alive(ps)


def test_down_after_the_process_died_is_fine(run_orch) -> None:
    o = run_orch()
    o.up("main")
    host.stop(o.store.load().branches["main"].processes["web"], grace=0.2)
    assert o.down("main")


def test_concurrent_up_never_picks_the_same_port(run_orch) -> None:
    o = run_orch()
    errors: list[BaseException] = []

    def up(branch: str) -> None:
        try:
            o.up(branch)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=up, args=(b,)) for b in ("main", "feat/cart")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    branches = o.store.load().branches
    chosen = {b: bs.processes["web"].port for b, bs in branches.items()}
    assert len(set(chosen.values())) == 2, chosen


def test_port_is_recorded_before_the_process_starts(
    run_orch, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = run_orch()
    seen: list[dict[str, int]] = []
    real_start = host.start

    def start(*a: Any, **k: Any) -> ProcessState:
        seen.append(dict(o.store.load().branches["main"].ports))
        return real_start(*a, **k)

    monkeypatch.setattr(host, "start", start)
    o.up("main")
    port = o.store.load().branches["main"].processes["web"].port
    assert seen == [{"api": naming.host_port(0, 0), "web": port}]


def test_ls_status_is_degraded_when_a_check_fails(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = orch()
    o.up("feat/cart")
    assert [r.status for r in o.ls()] == ["up"]
    monkeypatch.setattr(health, "probe", lambda *a, **k: health.Check(False, "HTTP 502"))
    [row] = o.ls()
    assert row.status == "degraded"
    # without probing there is no evidence of a failure
    assert [r.status for r in o.ls(probe=False)] == ["up"]


def test_ls_status_is_degraded_when_not_all_services_run(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = orch()
    o.up("feat/cart")
    monkeypatch.setattr(compose, "ps", lambda p: [compose.Container("api", "exited", "")])
    [row] = o.ls(probe=False)
    assert row.running == 0 and row.total == 1
    assert row.status == "degraded"


def _store_status(o: Any, status: str) -> None:
    with o.store.edit() as state:
        state.branches["feat/cart"].status = status


def test_ls_status_degraded_recovers_to_up(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = orch()
    o.up("feat/cart")
    _store_status(o, "degraded")
    assert [r.status for r in o.ls()] == ["up"]
    assert [r.status for r in o.ls(probe=False)] == ["up"]
    monkeypatch.setattr(health, "probe", lambda *a, **k: health.Check(False, "HTTP 502"))
    assert [r.status for r in o.ls()] == ["degraded"]


def test_ls_status_other_states_are_shown_as_stored(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/cart")
    for status in ("failed", "creating", "removing"):
        _store_status(o, status)
        assert [r.status for r in o.ls()] == [status]


def test_ls_status_keeps_stored_when_docker_is_unreachable(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = orch()
    o.up("feat/cart")
    _store_status(o, "degraded")

    def boom(p: str) -> list[compose.Container]:
        raise WtError("docker down")

    monkeypatch.setattr(compose, "ps", boom)
    assert [r.status for r in o.ls()] == ["degraded"]


def test_up_and_down_hold_the_provider_lock(
    orch, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wt import orchestrator

    held: list[str] = []
    seen: dict[str, list[str]] = {}
    real = orchestrator.file_lock  # type: ignore[attr-defined]

    @contextmanager
    def spy(name: str) -> Iterator[None]:
        held.append(name)
        try:
            with real(name):
                yield
        finally:
            held.remove(name)

    monkeypatch.setattr(orchestrator, "file_lock", spy)
    for method in ("ensure_server", "create", "destroy"):
        original = getattr(fake_provider, method)

        def wrapped(*a, _m=method, _o=original, **k):
            seen[_m] = list(held)
            return _o(*a, **k)

        monkeypatch.setattr(fake_provider, method, wrapped)
    o = orch(shared=SHARED)
    o.up("feat/cart")
    o.down("feat/cart")
    for method in ("ensure_server", "create", "destroy"):
        assert "postgres" in seen[method], method


def _git_branches(root: Path) -> str:
    return proc.run(["git", "branch", "--list"], cwd=root).stdout


def test_refused_base_alias_has_no_side_effects(orch, docker: FakeDocker, tmp_path: Path) -> None:
    o = orch(shared=SHARED)
    with pytest.raises(WtError, match="same databases"):
        o.up("Main")
    assert o.store.load().branches == {}
    assert "Main" not in _git_branches(tmp_path / "shop-api")


def test_refused_up_leaves_the_slug_free(orch, fake_provider: FakeProvider) -> None:
    o = orch(shared=SHARED)
    fake_provider.refuse = True
    with pytest.raises(WtError, match="refused"):
        o.up("Main")
    assert o.store.load().branches == {}
    fake_provider.refuse = False
    o.up("main")
    assert "main" in o.store.load().branches


def test_up_route_visible_does_not_restart_traefik(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    restarts: list[int] = []
    monkeypatch.setattr(router, "missing_in_container", lambda: [])
    monkeypatch.setattr(router, "restart", lambda *a, **k: restarts.append(1))
    orch().up("feat/cart")
    assert restarts == []


def test_up_recreates_traefik_when_route_file_is_not_visible(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = [["shop--feat-cart.yml", "other.yml"], ["other.yml"]]
    restarts: list[int] = []
    monkeypatch.setattr(router, "missing_in_container", lambda: answers.pop(0))
    monkeypatch.setattr(router, "restart", lambda *a, **k: restarts.append(1))
    o = orch()
    logs: list[str] = []
    o.log = logs.append
    o.up("feat/cart")
    assert restarts == [1]
    assert any("Traefik does not see the route file; recreating it" in m for m in logs)
    assert [r.status for r in o.ls(probe=False)] == ["up"]


def test_up_fails_when_route_file_is_still_missing(
    orch, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    restarts: list[int] = []
    monkeypatch.setattr(router, "missing_in_container", lambda: ["shop--feat-cart.yml"])
    monkeypatch.setattr(router, "restart", lambda *a, **k: restarts.append(1))
    o = orch()
    with pytest.raises(WtError, match=r"shop--feat-cart\.yml.*wt doctor --fix"):
        o.up("feat/cart")
    assert restarts == [1]
    assert [r.status for r in o.ls(probe=False)] == ["failed"]


def test_consumes_url_reaches_the_consumer_env(
    make_repo, tmp_path: Path, docker: FakeDocker, isolated_home: Path
) -> None:
    make_repo("shop-api", {"docker-compose.yml": COMPOSE})
    (tmp_path / "catalog.yaml").write_text(
        """\
repos: {r: ./shop-api}
services:
  back:
    repo: r
    compose: docker-compose.yml
    compose_service: api
    port: 8000
    subdomain: api
    remote: https://api.stg.example
  front:
    repo: r
    compose: docker-compose.yml
    compose_service: db
    port: 80
    subdomain: ""
    consumes: {back: API_URL}
"""
    )
    path = tmp_path / "worktree.yaml"
    path.write_text(
        "project: shop\ndomain: shop.localhost\ncatalog: ./catalog.yaml\nuses: [front, back]\n"
    )
    Orchestrator(load_config(path), log=lambda m: None).up("feat/x")
    override = docker.files[0][1].read_text()
    assert "API_URL: https://api.feat-x.shop.localhost" in override


# ---- start order (needs) -----------------------------------------------------------------


@pytest.fixture
def start_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Compose group and process starts, in call order."""
    events: list[str] = []
    real_start, real_up = host.start, compose.up

    def start(command: str, cwd: Path, *a: Any, **k: Any) -> ProcessState:
        events.append(f"process {Path(cwd).name}:{command.split()[-1]}")
        return real_start(command, cwd, *a, **k)

    def up(project: str, *a: Any, **k: Any) -> None:
        events.append(f"compose {project}")
        real_up(project, *a, **k)

    monkeypatch.setattr(host, "start", start)
    monkeypatch.setattr(compose, "up", up)
    return events


def sleeper(tag: str, **extra: object) -> dict[str, object]:
    return {"repo": "api", "run": f"sleep 60 # {tag}", **extra}


def test_run_service_needing_a_compose_group_starts_after_it(
    run_orch, start_events: list[str]
) -> None:
    o = run_orch({"front": sleeper("front", needs=["db"])})
    o.up("main")
    assert [e.split(":")[0] for e in start_events] == [
        "compose wt-shop-main-api",
        "process shop-api",
    ]


def test_compose_group_needing_a_run_service_starts_after_it(
    run_orch, start_events: list[str]
) -> None:
    services = {"job": sleeper("job")}
    o = run_orch(services, extra_needs={"db": ["job"]})
    o.up("main")
    assert [e.split(":")[0] for e in start_events] == [
        "process shop-api",
        "compose wt-shop-main-api",
    ]


def test_run_services_start_in_needs_order(run_orch, start_events: list[str]) -> None:
    services = {
        "front": sleeper("front", needs=["mid"]),
        "mid": sleeper("mid", needs=["back"]),
        "back": sleeper("back"),
    }
    o = run_orch(services)
    o.up("main")
    assert start_events[1:] == [
        "process shop-api:back",
        "process shop-api:mid",
        "process shop-api:front",
    ]


def test_without_needs_the_order_is_compose_then_run_in_config_order(
    run_orch, start_events: list[str]
) -> None:
    o = run_orch({"one": sleeper("one"), "two": sleeper("two")})
    o.up("main")
    assert start_events == [
        "compose wt-shop-main-api",
        "process shop-api:one",
        "process shop-api:two",
    ]


def test_consumer_waits_for_a_run_service_port(run_orch, start_events: list[str]) -> None:
    services = {
        "back": sleeper("back", port="auto"),
        "front": sleeper("front", needs=["back"]),
    }
    o = run_orch(services)
    with pytest.raises(WtError, match="back"):
        o.up("main", timeout=1)
    assert start_events == [
        "compose wt-shop-main-api",
        "process shop-api:back",
    ]  # front never started


def test_cycle_across_compose_groups_is_an_error_before_anything_starts(
    make_repo, tmp_path: Path, docker: FakeDocker, start_events: list[str]
) -> None:
    make_repo("one", {"docker-compose.yml": COMPOSE})
    make_repo("two", {"docker-compose.yml": COMPOSE})
    config = {
        "project": "shop",
        "domain": "shop.localhost",
        "repos": {"one": "./one", "two": "./two"},
        "services": {
            "a1": {"repo": "one", "compose": "docker-compose.yml", "compose_service": "api"},
            "a2": {"repo": "one", "compose": "docker-compose.yml", "compose_service": "db"},
            "b1": {
                "repo": "two",
                "compose": "docker-compose.yml",
                "compose_service": "api",
                "needs": ["a1"],
            },
            "b2": {"repo": "two", "compose": "docker-compose.yml", "compose_service": "db"},
        },
    }
    config["services"]["a2"]["needs"] = ["b2"]  # type: ignore[index]
    path = tmp_path / "worktree.yaml"
    path.write_text(yaml.safe_dump(config))
    o = Orchestrator(load_config(path), log=lambda m: None)
    with pytest.raises(WtError, match="cycle"):
        o.up("main")
    assert start_events == [] and docker.ups == []
    assert "main" not in o.store.load().branches


# ---- lifecycle: add / stop / remove (SPEC section 16) ----------------------------------------


def test_add_creates_worktrees_and_ports_but_no_containers(
    run_orch, docker: FakeDocker, isolated_home: Path
) -> None:
    o = run_orch()
    assert o.add("feat/cart") is True
    bs = o.store.load().branches["feat/cart"]
    assert bs.status == "stopped"
    assert bs.repos["api"].created and Path(bs.repos["api"].path).is_dir()
    assert bs.ports == {"api": naming.host_port(0, 0), "web": naming.host_port(0, 2)}
    assert bs.index == 0 and not bs.processes
    assert docker.ups == [] and docker.downs == []
    assert not (isolated_home / "traefik" / "dynamic").exists()
    [row] = o.ls(probe=False)
    assert row.status == "stopped" and row.ports == sorted(bs.ports.values())


def test_add_twice_is_a_noop(orch, docker: FakeDocker) -> None:
    o = orch()
    assert o.add("feat/cart") is True
    assert o.add("feat/cart") is False


def test_add_runs_the_provider_preflight_only(orch, fake_provider: FakeProvider) -> None:
    fake_provider.refuse = True
    o = orch(shared=SHARED)
    with pytest.raises(WtError, match="refused"):
        o.add("feat/cart")
    fake_provider.refuse = False
    o.add("feat/cart")
    assert fake_provider.events == []  # no ensure, create or verify


def test_up_after_add_reuses_worktree_and_ports(run_orch, docker: FakeDocker) -> None:
    o = run_orch()
    o.add("feat/cart")
    before = o.store.load().branches["feat/cart"]
    done = o.up("feat/cart")
    after = o.store.load().branches["feat/cart"]
    assert after.status == "up", why(o, "feat/cart", done)
    assert after.ports == before.ports
    assert after.repos["api"].path == before.repos["api"].path


def test_stop_keeps_worktrees_and_ports_and_removes_the_route(
    run_orch, docker: FakeDocker, isolated_home: Path
) -> None:
    o = run_orch()
    o.up("feat/cart")
    before = o.store.load().branches["feat/cart"]
    o.stop("feat/cart")
    bs = o.store.load().branches["feat/cart"]
    assert bs.status == "stopped" and not bs.processes and bs.route_file is None
    assert docker.kept_downs == ["wt-shop-feat-cart-api"]  # volumes kept
    assert bs.ports == before.ports
    assert Path(bs.repos["api"].path).is_dir()
    assert not list((isolated_home / "traefik" / "dynamic").iterdir())
    assert not host.alive(before.processes["web"])
    done = o.up("feat/cart")
    assert o.store.load().branches["feat/cart"].status == "up", why(o, "feat/cart", done)


def test_stop_unknown_branch_is_an_error(orch, docker: FakeDocker) -> None:
    with pytest.raises(WtError, match="not found"):
        orch().stop("feat/nope")


def test_remove_deletes_the_git_branch(orch, docker: FakeDocker, tmp_path: Path) -> None:
    o = orch()
    o.up("feat/cart")
    assert o.remove("feat/cart") is True
    assert o.store.load().branches == {}
    assert "feat/cart" not in git_run(tmp_path / "shop-api", "branch", "--list")


def test_remove_refuses_dirty_worktree_without_force(
    orch, docker: FakeDocker, isolated_home: Path, tmp_path: Path
) -> None:
    o = orch()
    o.up("feat/cart")
    (isolated_home / "shop" / "worktrees" / "feat-cart" / "api" / "wip.txt").write_text("x")
    with pytest.raises(WtError, match="untracked"):
        o.remove("feat/cart")
    assert o.remove("feat/cart", force=True) is True
    assert "feat/cart" not in git_run(tmp_path / "shop-api", "branch", "--list")


def test_remove_unknown_branch_deletes_nothing(orch, docker: FakeDocker, tmp_path: Path) -> None:
    o = orch()
    git_run(tmp_path / "shop-api", "branch", "feat/old")
    assert o.remove("feat/old") is False
    assert "feat/old" in git_run(tmp_path / "shop-api", "branch", "--list")


def test_remove_keeps_the_branch_checked_out_in_the_main_folder(
    orch, docker: FakeDocker, tmp_path: Path
) -> None:
    o = orch()
    o.up("main")
    assert o.remove("main") is True
    assert "main" in git_run(tmp_path / "shop-api", "branch", "--list")


def test_remove_keeps_the_postgres_base_branch(
    orch, fake_provider: FakeProvider, tmp_path: Path
) -> None:
    o = orch(shared={"postgres": {"databases": ["shop"], "base_branch": "develop"}})
    git_run(tmp_path / "shop-api", "branch", "develop")
    o.up("develop")
    assert o.remove("develop") is True
    assert "develop" in git_run(tmp_path / "shop-api", "branch", "--list")


def test_remove_keeps_a_branch_checked_out_in_a_foreign_folder(
    orch, docker: FakeDocker, tmp_path: Path
) -> None:
    o = orch()
    foreign = tmp_path / "foreign"
    git_run(tmp_path / "shop-api", "worktree", "add", "-b", "feat/x", str(foreign))
    o.up("feat/x")
    assert not o.store.load().branches["feat/x"].repos["api"].created
    assert o.remove("feat/x") is True
    assert foreign.is_dir()
    assert "feat/x" in git_run(tmp_path / "shop-api", "branch", "--list")


# --- per-worktree selection (§17 D-a, D-d) ---
SELECTION_CATALOG = """\
repos:
  api: ./shop-api
services:
  db:
    repo: api
    compose: docker-compose.yml
    compose_service: db
    port: 5432
    subdomain: dbx
    remote: https://db.staging.example
  web:
    repo: api
    compose: docker-compose.yml
    compose_service: api
    port: 8000
    subdomain: web
    env:
      DB_URL: "{{ services.db.url }}|{{ services.db.host }}|{{ services.db.port }}"
    consumes:
      db: DB
"""


@pytest.fixture
def catalog_orch(make_repo, tmp_path: Path, docker: FakeDocker):
    make_repo("shop-api", {"docker-compose.yml": COMPOSE})
    (tmp_path / "catalog.yaml").write_text(SELECTION_CATALOG)
    path = tmp_path / "worktree.yaml"
    path.write_text(
        "project: shop\ndomain: shop.localhost\ncatalog: ./catalog.yaml\nuses: [db, web]\n"
    )
    return Orchestrator(load_config(path), log=lambda m: None)


def _set_uses(o: Orchestrator, branch: str, uses: list[str] | None) -> None:
    with o.store.edit() as state:
        state.branches[branch].uses = uses


def test_branch_state_uses_defaults_to_none(catalog_orch: Orchestrator, docker: FakeDocker) -> None:
    catalog_orch.up("feat/x")
    assert catalog_orch.store.load().branches["feat/x"].uses is None
    assert catalog_orch.store.load().branches["feat/x"].route_file is not None


def test_stored_uses_wins_over_the_yaml(catalog_orch: Orchestrator, docker: FakeDocker) -> None:
    o = catalog_orch
    o.up("feat/x")  # yaml: [db, web] -> web routed
    _set_uses(o, "feat/x", ["db"])
    reports = o.up("feat/x")
    assert [r.service for r in reports] == ["db"]
    assert o.store.load().branches["feat/x"].route_file is not None
    assert "--web" not in Path(o.store.load().branches["feat/x"].route_file or "").read_text()
    other = o.up("feat/y")  # a branch without a stored selection keeps the yaml
    assert [r.service for r in other] == ["db", "web"]


def test_ls_down_use_the_branch_selection(catalog_orch: Orchestrator, docker: FakeDocker) -> None:
    o = catalog_orch
    o.up("feat/x")
    _set_uses(o, "feat/x", ["db"])
    assert [r.service for r in o.ls(probe=False)[0].services] == ["db"]  # web is not selected
    assert o.down("feat/x") is True
    assert o.store.load().branches == {}


def test_stop_uses_the_branch_selection(
    catalog_orch: Orchestrator, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    o = catalog_orch
    o.up("feat/x")
    _set_uses(o, "feat/x", ["db"])
    seen: list[list[str]] = []
    real = Orchestrator.compose_groups

    def spy(self: Orchestrator) -> Any:
        seen.append(list(self.cfg.services))
        return real(self)

    monkeypatch.setattr(Orchestrator, "compose_groups", spy)
    o.stop("feat/x")
    assert seen and all(s == ["db"] for s in seen)


def test_add_uses_the_branch_selection_when_known(
    catalog_orch: Orchestrator, docker: FakeDocker
) -> None:
    o = catalog_orch
    assert o.add("feat/x") is True  # new branch: yaml selection
    _set_uses(o, "feat/x", ["db"])
    assert o.add("feat/x") is False


def test_unselected_service_renders_its_remote(
    catalog_orch: Orchestrator, docker: FakeDocker
) -> None:
    o = catalog_orch.__class__(catalog_orch.loaded.with_uses(["web"]), log=lambda m: None)
    names = o.names("feat/x")
    ctx = o.template_context(names, o.cfg.services["web"])
    assert ctx["services"]["db"] == {
        "url": "https://db.staging.example",
        "host": "",
        "port": None,
    }
    envs, _ = o._render(names, {})
    assert envs["web"]["DB_URL"] == "https://db.staging.example||None"


def test_unselected_service_without_remote_renders_empty(
    make_repo, tmp_path: Path, docker: FakeDocker
) -> None:
    make_repo("shop-api", {"docker-compose.yml": COMPOSE})
    (tmp_path / "catalog.yaml").write_text(
        "repos: {api: ./shop-api}\nservices:\n"
        "  a: {repo: api, compose: docker-compose.yml, compose_service: api}\n"
        "  b: {repo: api, compose: docker-compose.yml, compose_service: db}\n"
    )
    path = tmp_path / "worktree.yaml"
    path.write_text("project: s\ndomain: s.localhost\ncatalog: ./catalog.yaml\nuses: [a]\n")
    o = Orchestrator(load_config(path), log=lambda m: None)
    ctx = o.template_context(o.names("x"), o.cfg.services["a"])
    assert ctx["services"]["b"] == {"url": "", "host": "", "port": None}


# ---- configure / catalog_choices (§17 D-e) -----------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def cfg_orch(make_repo, tmp_path: Path, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch):
    """Catalog project: db+api (repo api, compose), web (repo ui, compose), front (repo ui, run)."""
    make_repo("shop-api", {"docker-compose.yml": COMPOSE})
    make_repo("shop-ui", {"docker-compose.yml": COMPOSE})
    (tmp_path / "catalog.yaml").write_text(
        f"""\
repos: {{api: ./shop-api, ui: ./shop-ui}}
services:
  db: {{repo: api, compose: docker-compose.yml, compose_service: db}}
  api:
    repo: api
    compose: docker-compose.yml
    compose_service: api
    port: 8000
    subdomain: api
    remote: https://api.staging.example
  web:
    repo: ui
    compose: docker-compose.yml
    compose_service: db
  front:
    repo: ui
    run: "{sys.executable} {SERVE} {{{{ port }}}}"
    port: {_free_port()}
    subdomain: front
    env: {{PLAIN: x}}
    consumes: {{api: API_URL}}
"""
    )
    path = tmp_path / "worktree.yaml"
    path.write_text(
        "project: shop\ndomain: shop.localhost\ncatalog: ./catalog.yaml\nuses: [api, front]\n"
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(compose, "down_keep_volumes", lambda p: calls.append(("down", p)))
    monkeypatch.setattr(compose, "remove_service", lambda p, s: calls.append(("rm", f"{p}/{s}")))
    o = Orchestrator(load_config(path), log=lambda m: None)
    o.calls = calls  # type: ignore[attr-defined]
    yield o
    for bs in o.store.load().branches.values():
        for ps in bs.processes.values():
            host.stop(ps, grace=0.2)


def _calls(o: Orchestrator) -> list[tuple[str, str]]:
    return o.calls  # type: ignore[attr-defined,no-any-return]


def test_catalog_choices_follow_the_branch_selection(cfg_orch: Orchestrator) -> None:
    o = cfg_orch
    o.add("feat/x")
    got = o.catalog_choices("feat/x")
    assert got is not None
    assert [(c.service, c.repo, c.selected) for c in got] == [
        ("db", "api", False),
        ("api", "api", True),
        ("web", "ui", False),
        ("front", "ui", True),
    ]
    o.configure("feat/x", ["db"])
    assert [c.service for c in o.catalog_choices("feat/x") or [] if c.selected] == ["db"]


def test_catalog_choices_none_for_services_project(orch, docker: FakeDocker) -> None:
    o = orch()
    o.add("feat/x")
    assert o.catalog_choices("feat/x") is None


def test_catalog_choices_unknown_branch(cfg_orch: Orchestrator) -> None:
    with pytest.raises(WtError, match="not found"):
        cfg_orch.catalog_choices("nope")


def test_configure_validates(cfg_orch: Orchestrator, orch, docker: FakeDocker) -> None:
    o = cfg_orch
    o.add("feat/x")
    with pytest.raises(WtError, match="select at least one service"):
        o.configure("feat/x", [])
    with pytest.raises(WtError, match="unknown service 'nope'"):
        o.configure("feat/x", ["nope"])
    with pytest.raises(WtError, match="not found"):
        o.configure("missing", ["api"])
    assert o.store.load().branches["feat/x"].uses is None
    with pytest.raises(WtError, match="needs a catalog project"):
        orch().configure("feat/x", ["api"])


def test_configure_adds_a_service_and_starts_it(cfg_orch: Orchestrator, docker: FakeDocker) -> None:
    o = cfg_orch
    o.up("feat/x")
    docker.ups.clear()
    o.configure("feat/x", ["api", "front", "web"])
    bs = o.store.load().branches["feat/x"]
    assert bs.uses == ["api", "front", "web"]
    assert "wt-shop-feat-x-ui" in docker.ups
    assert bs.status == "up"
    assert "front" in bs.processes


def test_configure_removes_a_run_service(cfg_orch: Orchestrator, docker: FakeDocker) -> None:
    o = cfg_orch
    o.up("feat/x")
    bs = o.store.load().branches["feat/x"]
    ps = bs.processes["front"]
    assert host.alive(ps)
    worktree = Path(bs.repos["ui"].path)
    route = Path(bs.route_file or "")
    assert "front" in route.read_text()
    o.configure("feat/x", ["api"])
    after = o.store.load().branches["feat/x"]
    assert after.uses == ["api"]
    assert "front" not in after.processes and "front" not in after.ports
    assert not host.alive(ps)
    assert "front" not in Path(after.route_file or "").read_text()
    assert worktree.is_dir()
    assert _calls(o) == []


def test_configure_consumer_restarts_with_the_remote(
    cfg_orch: Orchestrator, docker: FakeDocker
) -> None:
    o = cfg_orch
    o.up("feat/x")
    o.configure("feat/x", ["front"])
    after = o.store.load().branches["feat/x"]
    assert after.uses == ["front"]
    bo = o._for("feat/x")
    envs, _ = bo._render(bo.names("feat/x"), {"front": after.processes["front"].port or 0})
    assert envs["front"]["API_URL"] == "https://api.staging.example"


def test_configure_drops_one_compose_service_of_a_kept_repo(
    cfg_orch: Orchestrator, docker: FakeDocker
) -> None:
    o = cfg_orch
    o.up("feat/x")
    o.configure("feat/x", ["db", "api", "front"])
    o.configure("feat/x", ["api", "front"])
    assert _calls(o) == [("rm", "wt-shop-feat-x-api/db")]
    assert "api" in o.store.load().branches["feat/x"].compose_projects


def test_configure_downs_a_repo_when_none_of_its_services_stays(
    cfg_orch: Orchestrator, docker: FakeDocker
) -> None:
    o = cfg_orch
    o.up("feat/x")
    o.configure("feat/x", ["api", "web"])  # front leaves, web (repo ui) arrives
    assert _calls(o) == []  # front is a run service: no compose
    o.configure("feat/x", ["api"])  # last service of repo ui leaves
    assert _calls(o) == [("down", "wt-shop-feat-x-ui")]
    assert "ui" not in o.store.load().branches["feat/x"].compose_projects


def test_configure_on_a_stopped_branch_does_not_start_it(
    cfg_orch: Orchestrator, docker: FakeDocker
) -> None:
    o = cfg_orch
    o.up("feat/x")
    o.stop("feat/x")
    docker.ups.clear()
    o.configure("feat/x", ["api"])
    bs = o.store.load().branches["feat/x"]
    assert (bs.uses, bs.status, docker.ups) == (["api"], "stopped", [])
    o.configure("feat/x", ["api", "web"])  # something new: it comes up
    assert o.store.load().branches["feat/x"].status == "up"
    assert docker.ups


def test_stop_keeps_compose_volumes_and_down_removes_them(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/cart")
    o.stop("feat/cart")
    assert docker.kept_downs == ["wt-shop-feat-cart-api"]  # compose down without -v
    assert docker.downs == []
    o.down("feat/cart")
    assert docker.downs == ["wt-shop-feat-cart-api"]  # down removes volumes


def _set_ps(monkeypatch: pytest.MonkeyPatch, *containers: compose.Container) -> None:
    monkeypatch.setattr(compose, "ps", lambda p: list(containers))


def _rows(o: Orchestrator, branch: str = "feat/x") -> dict[str, str]:
    return {r.name: r.status for r in o.services(branch)}


def test_services_rows_of_a_healthy_branch(cfg_orch: Orchestrator, monkeypatch) -> None:
    o = cfg_orch
    o.up("feat/x")
    _set_ps(monkeypatch, compose.Container("api", "running", "healthy"))
    front = o.store.load().branches["feat/x"].processes["front"]
    assert wait_until(lambda: host.tcp_ok(front.port or 0))
    api, ui = o.services("feat/x")
    bs = o.store.load().branches["feat/x"]
    assert (api.name, api.status, api.port) == ("api", "healthy", bs.ports.get("api"))
    assert api.url == "https://api.feat-x.shop.localhost"
    assert (ui.name, ui.status, ui.port) == ("ui", "healthy", bs.ports["front"])
    assert ui.url == "https://front.feat-x.shop.localhost"


@pytest.mark.parametrize(
    ("container", "expected"),
    [
        (compose.Container("api", "running", "starting"), "starting"),
        (compose.Container("api", "restarting", ""), "starting"),
        (compose.Container("api", "running", "unhealthy"), "unhealthy"),
        (compose.Container("api", "exited", ""), "stopped"),
    ],
)
def test_services_compose_states(
    cfg_orch: Orchestrator, monkeypatch, container: compose.Container, expected: str
) -> None:
    o = cfg_orch
    o.up("feat/x")
    _set_ps(monkeypatch, container)
    assert _rows(o)["api"] == expected


def test_services_one_row_for_two_compose_services_of_a_repo(
    cfg_orch: Orchestrator, monkeypatch
) -> None:
    o = cfg_orch
    o.up("feat/x")
    _set_ps(
        monkeypatch,
        compose.Container("db", "running", "healthy"),
        compose.Container("api", "exited", ""),
    )
    rows = o.services("feat/x")
    assert [r.name for r in rows] == ["api", "ui"]
    assert rows[0].status == "unhealthy"  # something runs, one container is down


def test_services_precedence_starting_over_unhealthy(cfg_orch: Orchestrator, monkeypatch) -> None:
    o = cfg_orch
    o.up("feat/x")
    _set_ps(
        monkeypatch,
        compose.Container("db", "running", "unhealthy"),
        compose.Container("api", "running", "starting"),
    )
    assert _rows(o)["api"] == "starting"


def test_services_dead_run_process_and_closed_port(cfg_orch: Orchestrator, monkeypatch) -> None:
    o = cfg_orch
    o.up("feat/x")
    _set_ps(monkeypatch, compose.Container("api", "running", "healthy"))
    monkeypatch.setattr(host, "tcp_ok", lambda *a, **k: False)
    assert _rows(o)["ui"] == "unhealthy"  # alive, nothing listening
    ps = o.store.load().branches["feat/x"].processes["front"]
    host.stop(ps, grace=0.2)
    assert _rows(o)["ui"] == "stopped"  # nothing runs in that repo


def test_services_stopped_and_missing_branch(cfg_orch: Orchestrator, monkeypatch) -> None:
    o = cfg_orch
    o.up("feat/x")
    o.stop("feat/x")
    assert _rows(o) == {"api": "stopped", "ui": "stopped"}
    rows = o.services("nope")
    assert [(r.name, r.status, r.port) for r in rows] == [
        ("api", "stopped", None),
        ("ui", "stopped", None),
    ]
    assert rows[0].url == "https://api.nope.shop.localhost"


def test_services_creating_branch_is_starting(cfg_orch: Orchestrator, monkeypatch) -> None:
    o = cfg_orch
    o.up("feat/x")
    _set_ps(monkeypatch, compose.Container("api", "running", ""))
    o._record("feat/x", lambda b: setattr(b, "status", "creating"))
    assert _rows(o)["api"] == "starting"


def test_services_follow_the_branch_selection(cfg_orch: Orchestrator, monkeypatch) -> None:
    o = cfg_orch
    o.up("feat/x")
    o.configure("feat/x", ["front"])
    assert [r.name for r in o.services("feat/x")] == ["ui"]


def test_ls_marks_the_repos_own_checkout(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("main")  # already checked out in the repo folder: wt does not create a worktree
    o.up("feat/cart")
    rows = {r.branch: r for r in o.ls(probe=False)}
    assert rows["main"].main_checkout
    assert not rows["feat/cart"].main_checkout


def test_private_port_window_stays_below_the_ephemeral_range(private_port_base: int) -> None:
    assert private_port_base >= PORT_WINDOW_FLOOR
    assert private_port_base + PORT_WINDOW <= LOWEST_EPHEMERAL_PORT


def test_private_port_window_is_probed_once_per_run(private_port_base: int) -> None:
    assert {private_port_base} == _BASES_SEEN  # a different base means a per-test probe
    assert 1 <= len(_PROBED_WINDOWS) <= WINDOW_TRIES
    assert _PROBED_WINDOWS[-1] == private_port_base


def test_pick_free_window_fails_loudly_when_no_window_is_free() -> None:
    probed: list[int] = []
    with pytest.raises(RuntimeError, match="no free window"):
        _pick_free_window(lambda base: False, probed)
    assert len(probed) == WINDOW_TRIES


def test_pick_free_window_stops_at_the_first_free_window() -> None:
    probed: list[int] = []
    base = _pick_free_window(lambda base: len(probed) == 3, probed)
    assert probed == [*probed[:2], base]
    assert len(probed) == 3


def test_window_is_free_rejects_a_port_that_is_bound_but_not_listening() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        assert not _window_is_free(port, size=1)
    assert _window_is_free(port, size=1)


def test_window_is_free_rejects_a_port_held_by_a_client_connection() -> None:
    with socket.socket() as server, socket.socket() as client:
        server.bind(("127.0.0.1", 0))
        server.listen()
        client.connect(server.getsockname())
        client_port = client.getsockname()[1]  # nothing listens here, only the client holds it
        assert not _window_is_free(client_port, size=1)


@contextmanager
def stranger_listening(port: int) -> Iterator[None]:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen()
        yield


def test_up_refuses_a_port_a_stranger_took_after_add(run_orch) -> None:
    o = run_orch()
    o.add("feat/cart")
    port = o.store.load().branches["feat/cart"].ports["web"]
    assert port == naming.host_port(0, 2)
    with stranger_listening(port), pytest.raises(WtError, match=f"port {port}.*in use"):
        o.up("feat/cart")


def test_up_refuses_a_stranger_on_the_port_of_an_exited_process(run_orch) -> None:
    o = run_orch()
    o.up("feat/cart")
    bs = o.store.load().branches["feat/cart"]
    ps, port = bs.processes["web"], bs.ports["web"]
    host.stop(ps, grace=0.2)
    assert wait_until(lambda: not host.tcp_ok(port))
    assert bs.status == "up"  # recorded status is stale: the process is gone
    with stranger_listening(port), pytest.raises(WtError, match=f"port {port}.*in use"):
        o.up("feat/cart")


def test_status_does_not_accept_a_stranger_answering_for_a_dead_process(run_orch) -> None:
    o = run_orch()
    o.up("feat/cart")
    bs = o.store.load().branches["feat/cart"]
    ps = bs.processes["web"]
    host.stop(ps, grace=0.2)
    assert wait_until(lambda: not host.tcp_ok(ps.port))
    with stranger_listening(ps.port):
        report = o._check_process("web", ps, 1)
    assert report.outcome == "fail" and "exited" in report.detail


def test_up_accepts_the_port_of_its_own_compose_container(orch, docker: FakeDocker) -> None:
    o = orch()
    o.up("feat/cart")
    port = o.store.load().branches["feat/cart"].ports["api"]
    assert port == naming.host_port(0, 0)
    with stranger_listening(port):  # the fake docker publishes nothing: stand in for the container
        assert {r.service: r.outcome for r in o.up("feat/cart")} == {"api": "ok", "db": "ok"}


def test_up_refuses_a_stranger_on_a_compose_port_after_add(orch, docker: FakeDocker) -> None:
    o = orch()
    o.add("feat/cart")
    port = o.store.load().branches["feat/cart"].ports["api"]
    with stranger_listening(port), pytest.raises(WtError, match=f"port {port}.*in use"):
        o.up("feat/cart")
    assert docker.ups == []
