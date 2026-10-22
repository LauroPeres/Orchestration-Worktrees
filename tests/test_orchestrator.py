import os
import shutil
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SHARED, FakeDocker, FakeProvider
from wt import compose, git, host, naming, proc, router, tls
from wt.errors import WtError
from wt.orchestrator import Orchestrator
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
    o = orch(env={"DB": "{{ kafka.topic('x') }}"})
    with pytest.raises(WtError, match=r"v0\.3"):
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

    monkeypatch.setattr(fake_provider, "ensure_server", broken)
    with pytest.raises(WtError, match="wt doctor --fix") as info:
        o.down("feat/cart")
    assert "wt down feat/cart" in str(info.value)
    assert "destroy" not in fake_provider.events
    assert o.store.load().branches["feat/cart"].providers == {"postgres": ["db_feat_cart"]}
    monkeypatch.undo()
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


def test_note_only_for_unregistered_providers(orch, fake_provider: FakeProvider) -> None:
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
def run_orch(orch, docker: FakeDocker) -> Iterator[Callable[..., Orchestrator]]:
    built: list[Orchestrator] = []

    def build(services: dict[str, object] | None = None, **kwargs: object) -> Orchestrator:
        o: Orchestrator = orch(extra_services=services or run_service(), **kwargs)
        built.append(o)
        return o

    yield build
    for o in built:
        for bs in o.store.load().branches.values():
            for ps in bs.processes.values():
                host.stop(ps, grace=0.2)


def wait_until(check: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_run_service_is_started_and_recorded(run_orch, isolated_home: Path) -> None:
    o = run_orch()
    reports = {r.service: r for r in o.up("feat/cart")}
    assert reports["web"].outcome == "ok"  # unrouted with a port: TCP connect
    ps = o.store.load().branches["feat/cart"].processes["web"]
    assert host.alive(ps)
    assert ps.port is not None and 20000 <= ps.port < 40000
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
    reports = {r.service: r for r in o.up("feat/cart")}
    assert reports["job"].outcome == "ok"  # no port: alive
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


def test_auto_port_skips_ports_of_other_branches(run_orch, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        base = int(s.getsockname()[1])
    monkeypatch.setattr(naming, "auto_port", lambda *a: base)
    o = run_orch()
    o.up("main")
    o.up("feat/cart")
    branches = o.store.load().branches
    main_port = branches["main"].processes["web"].port
    cart_port = branches["feat/cart"].processes["web"].port
    assert main_port is not None and cart_port is not None
    # Both start from the same derived port; feat/cart must skip main's port and probe upward.
    # (No exact `base + 1`: on a busy CI host the next port may already be taken.)
    assert main_port >= base
    assert cart_port > main_port


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


def test_concurrent_up_never_picks_the_same_port(run_orch, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        base = int(sock.getsockname()[1])
    monkeypatch.setattr(naming, "auto_port", lambda *a: base)
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
    assert seen == [{"web": port}]
