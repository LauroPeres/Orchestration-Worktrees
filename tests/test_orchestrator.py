import shutil
import threading
from pathlib import Path

import pytest

from tests.conftest import SHARED, FakeDocker, FakeProvider
from wt import compose, git, router, tls
from wt.errors import WtError
from wt.providers.base import Check


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
    assert fake_provider.events[4:] == ["compose down", "destroy"]


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
