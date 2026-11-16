import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from wt import compose, demo, doctor, paths, proc, router, tls
from wt.errors import WtError
from wt.orchestrator import RemoveResult, ServiceReport
from wt.providers import kafka, keycloak, postgres, redis
from wt.state import StateStore

DOCKER_OK = [doctor.Check("docker", "ok", "engine 27")]
HOST_FOR = {"main": "main.shop.localhost", "feat/cart": "feat-cart.shop.localhost"}


@dataclass
class Harness:
    """Fakes for everything `wt.demo` calls below the core, recording what it was asked."""

    state: Path
    events: list[str] = field(default_factory=list)
    envs: dict[str, dict[str, str]] = field(default_factory=dict)  # event -> WT_* at that moment
    reports: dict[str, list[ServiceReport]] = field(default_factory=dict)
    fail_up_on: str | None = None
    busy_ports: set[int] = field(default_factory=set)
    probed: list[int] = field(default_factory=list)
    traefik_running: bool = False
    docker_checks: list[doctor.Check] = field(default_factory=lambda: list(DOCKER_OK))
    network_removals: list[str] = field(default_factory=list)
    network_present: bool = False  # the `wtdemo` Docker network exists
    compose_downs: list[str] = field(default_factory=list)

    @property
    def home(self) -> Path:
        return self.state / "wt-demo"

    def note(self, event: str) -> None:
        self.events.append(event)
        self.envs[event] = {k: v for k, v in os.environ.items() if k.startswith("WT_")}

    def probe(self, port: int) -> bool:
        self.probed.append(port)
        return port in self.busy_ports

    def run(self, args: list[str], **kwargs: Any) -> proc.Result:
        if args[0] == "bash":
            self.note("bootstrap")
            self.bootstrap_args = args
            shop = Path(args[2])
            shop.mkdir(parents=True)
            (shop / "catalog.yaml").write_text("project: shop\n")
        elif args[:3] == ["docker", "network", "inspect"]:
            return proc.Result(0 if self.network_present else 1, "", "")
        elif args[:3] == ["docker", "network", "rm"]:
            self.note("network rm")
            self.network_removals.append(args[3])
            self.network_present = False
        else:
            raise AssertionError(f"unexpected command {args}")
        return proc.Result(0, "", "")

    def compose_down(self, project: str) -> None:
        self.note(f"compose down {project}")
        self.compose_downs.append(project)

    def make_orchestrator(self, loaded: object, log: Callable[[str], None]) -> "FakeOrchestrator":
        return FakeOrchestrator(self, log)


class FakeOrchestrator:
    def __init__(self, harness: Harness, log: Callable[[str], None]) -> None:
        self.harness = harness
        self.log = log

    def up(self, branch: str, *, uses: list[str] | None = None, **_: object) -> list[ServiceReport]:
        self.harness.note(f"up {branch} {','.join(uses or [])}")
        self.log(f"up {branch}")
        if self.harness.fail_up_on == branch:
            raise WtError(f"compose up failed for {branch}")
        return self.harness.reports.get(branch, [ServiceReport("web", "ok", "HTTP 200")])

    def remove(self, branch: str, *, force: bool = False) -> RemoveResult:
        self.harness.note(f"remove {branch} force={force}")
        return RemoveResult(True)

    def urls(self, branch: str) -> dict[str, str]:
        port = os.environ["WT_HTTPS_PORT"]
        suffix = "" if port == "443" else f":{port}"
        host = HOST_FOR[branch]
        return {"web": f"https://{host}{suffix}", "api": f"https://api.{host}{suffix}"}


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    h = Harness(state)
    monkeypatch.setattr(proc, "run", h.run)
    monkeypatch.setattr(compose, "down", h.compose_down)
    monkeypatch.setattr(doctor, "check_docker", lambda: h.docker_checks)
    monkeypatch.setattr(router, "is_running", lambda: h.traefik_running)
    monkeypatch.setattr(tls, "available", lambda: True)
    monkeypatch.setattr(paths, "example_dir", lambda name: tmp_path / "examples" / name)
    monkeypatch.setattr(demo, "load_project", lambda path: path)
    monkeypatch.setattr(demo, "Orchestrator", h.make_orchestrator)
    return h


def run_up(harness: Harness, log: Callable[[str], None] = lambda m: None) -> demo.DemoResult:
    return demo.up(log, harness.probe)


# ---- environment ------------------------------------------------------------------------


def test_environment_is_the_demos_and_hides_the_visitors(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WT_HOME", "/visitor/home")
    monkeypatch.setenv("WT_TERMINAL", "xterm {path}")
    monkeypatch.setenv("WT_POSTGRES_PORT", "1")
    with demo.demo_environment():
        seen = {k: v for k, v in os.environ.items() if k.startswith("WT_")}
    assert seen == {
        "WT_INSTANCE": "wtdemo",
        "WT_HOME": str(harness.home),
        "WT_CATALOG": str(harness.home / "shop" / "catalog.yaml"),
        "WT_POSTGRES_PORT": str(postgres.DEFAULT_PORT + 100),
        "WT_REDIS_PORT": str(redis.DEFAULT_PORT + 100),
        "WT_KAFKA_PORT": str(kafka.DEFAULT_PORT + 100),
        "WT_KEYCLOAK_PORT": str(keycloak.DEFAULT_PORT + 100),
        "WT_HTTP_PORT": "80",
        "WT_HTTPS_PORT": "443",
        "WT_NO_FETCH": "1",
    }
    assert os.environ["WT_HOME"] == "/visitor/home"
    assert os.environ["WT_TERMINAL"] == "xterm {path}"
    assert os.environ["WT_POSTGRES_PORT"] == "1"
    assert "WT_INSTANCE" not in os.environ


def test_default_home_is_under_the_user_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert demo.demo_home() == tmp_path / ".local" / "state" / "wt-demo"


def test_visitors_web_ports_are_kept(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_HTTP_PORT", "8080")
    monkeypatch.setenv("WT_HTTPS_PORT", "8443")
    with demo.demo_environment():
        assert (os.environ["WT_HTTP_PORT"], os.environ["WT_HTTPS_PORT"]) == ("8080", "8443")


@pytest.mark.parametrize("error", [WtError("boom"), KeyboardInterrupt()])
def test_environment_is_restored_after_an_error_and_ctrl_c(
    error: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WT_TERMINAL", "xterm")
    monkeypatch.setenv("WT_HOME", "/visitor/home")
    before = dict(os.environ)
    with pytest.raises(type(error)), demo.demo_environment():
        raise error
    assert dict(os.environ) == before


def test_environment_scope_applies_to_the_modules_that_read_it_and_gives_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No mocks: the real modules read the real `os.environ` inside and outside the scope."""
    visitor_home = tmp_path / "visitor-home"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("WT_HOME", str(visitor_home))
    monkeypatch.setenv("WT_INSTANCE", "visitor")
    monkeypatch.setenv("WT_HTTPS_PORT", "8443")
    monkeypatch.setenv("WT_POSTGRES_PORT", "1111")

    with demo.demo_environment():
        assert paths.wt_home() == demo.demo_home()
        assert paths.instance() == "wtdemo"
        assert router.container() == "wtdemo-traefik"
        assert compose.network_name() == "wtdemo"
        assert router.https_port() == 8443  # the visitor's choice survives
        assert postgres.host_port() == postgres.DEFAULT_PORT + 100
        assert redis.host_port() == redis.DEFAULT_PORT + 100
        assert kafka.host_port() == kafka.DEFAULT_PORT + 100
        assert keycloak.host_port() == keycloak.DEFAULT_PORT + 100
        assert StateStore("shop").path == demo.demo_home() / "shop" / "state.json"

    assert paths.wt_home() == visitor_home
    assert paths.instance() == "visitor"
    assert router.container() == "visitor-traefik"
    assert compose.network_name() == "visitor"
    assert router.https_port() == 8443
    assert postgres.host_port() == 1111
    assert StateStore("shop").path == visitor_home / "shop" / "state.json"


# ---- up ---------------------------------------------------------------------------------


def test_up_returns_the_urls_of_both_branches(harness: Harness) -> None:
    result = run_up(harness)
    assert result.home == harness.home
    assert not result.self_signed
    assert result.branches == (
        demo.DemoBranch(
            "main", "https://main.shop.localhost", "https://api.main.shop.localhost/items"
        ),
        demo.DemoBranch(
            "feat/cart",
            "https://feat-cart.shop.localhost",
            "https://api.feat-cart.shop.localhost/items",
        ),
    )


def test_up_starts_main_then_the_cart_branch_inside_the_demo_environment(
    harness: Harness,
) -> None:
    run_up(harness)
    assert harness.events == ["bootstrap", "up main web,worker", "up feat/cart web,worker"]
    assert harness.envs["up main web,worker"]["WT_INSTANCE"] == "wtdemo"
    assert harness.envs["bootstrap"]["WT_HOME"] == str(harness.home)


def test_up_streams_the_orchestrators_progress(harness: Harness) -> None:
    lines: list[str] = []
    run_up(harness, lines.append)
    assert "up main" in lines
    assert "up feat/cart" in lines


def test_up_says_when_the_certificate_is_self_signed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tls, "available", lambda: False)
    assert run_up(harness).self_signed


def test_https_port_shows_in_the_urls(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_HTTPS_PORT", "8443")
    result = run_up(harness)
    assert result.branches[0].web == "https://main.shop.localhost:8443"
    assert result.branches[1].api == "https://api.feat-cart.shop.localhost:8443/items"
    assert harness.probed == [80, 8443]


def test_first_run_is_known_before_anything_is_created(harness: Harness) -> None:
    assert demo.is_first_run()
    run_up(harness)
    assert not demo.is_first_run()


def test_bootstrap_runs_with_bash_when_there_is_no_catalog_and_drops_leftovers(
    harness: Harness, tmp_path: Path
) -> None:
    leftover = harness.home / "shop" / "shop-api"
    leftover.mkdir(parents=True)
    (leftover / "half-done").write_text("x")
    run_up(harness)
    assert harness.bootstrap_args == [
        "bash",
        str(tmp_path / "examples" / "shop" / "bootstrap.sh"),
        str(harness.home / "shop"),
    ]
    assert not (leftover / "half-done").exists()


def test_bootstrap_is_skipped_when_the_catalog_exists(harness: Harness) -> None:
    run_up(harness)
    harness.events.clear()
    again = run_up(harness)
    assert harness.events == ["up main web,worker", "up feat/cart web,worker"]
    assert again.branches[0].web == "https://main.shop.localhost"


# ---- up: what is checked before anything is created ----------------------------------


def test_busy_https_port_fails_before_anything_is_created(harness: Harness) -> None:
    harness.busy_ports = {443}
    with pytest.raises(WtError) as caught:
        run_up(harness)
    message = str(caught.value)
    assert message == (
        "port 443 is in use (another Traefik?). Set WT_HTTPS_PORT, e.g. WT_HTTPS_PORT=8443 wt demo"
    )
    assert "wt demo down" not in message
    assert harness.events == []
    assert not harness.home.exists()


def test_busy_http_port_names_its_own_variable(harness: Harness) -> None:
    harness.busy_ports = {80}
    with pytest.raises(WtError, match=r"port 80 .*Set WT_HTTP_PORT, e\.g\. WT_HTTP_PORT=8080"):
        run_up(harness)
    assert harness.events == []


def test_ports_held_by_the_demos_own_traefik_are_fine(harness: Harness) -> None:
    harness.busy_ports = {80, 443}
    harness.traefik_running = True
    assert len(run_up(harness).branches) == 2


def test_docker_down_gives_the_doctors_message_and_creates_nothing(harness: Harness) -> None:
    harness.docker_checks = [
        doctor.Check("docker", "fail", "daemon not reachable", "start Docker and retry")
    ]
    with pytest.raises(WtError) as caught:
        run_up(harness)
    assert str(caught.value) == "docker: daemon not reachable; start Docker and retry"
    assert harness.events == []
    assert harness.probed == []


# ---- up: failures -----------------------------------------------------------------------


def test_a_failing_step_ends_with_the_cleanup_hint(harness: Harness) -> None:
    harness.fail_up_on = "feat/cart"
    with pytest.raises(WtError) as caught:
        run_up(harness)
    assert str(caught.value) == ("compose up failed for feat/cart\nrun `wt demo down` to clean up")


def test_a_failing_bootstrap_ends_with_the_cleanup_hint(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(args: list[str], **kwargs: Any) -> proc.Result:
        raise WtError("bootstrap exploded")

    monkeypatch.setattr(proc, "run", fail)
    with pytest.raises(WtError, match=r"bootstrap exploded\nrun `wt demo down`"):
        run_up(harness)


def test_ctrl_c_after_the_checks_prints_the_cleanup_hint_and_propagates(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(self: FakeOrchestrator, branch: str, **_: object) -> list[ServiceReport]:
        raise KeyboardInterrupt

    monkeypatch.setattr(FakeOrchestrator, "up", interrupted)
    lines: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        run_up(harness, lines.append)
    assert lines[-1] == "interrupted; run `wt demo down` to clean up"


def test_ctrl_c_during_the_checks_has_nothing_to_clean_up(harness: Harness) -> None:
    def interrupted(port: int) -> bool:
        raise KeyboardInterrupt

    lines: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        demo.up(lines.append, interrupted)
    assert lines == []


def test_an_unhealthy_service_is_a_failure(harness: Harness) -> None:
    harness.reports["main"] = [ServiceReport("api", "fail", "HTTP 502")]
    with pytest.raises(WtError) as caught:
        run_up(harness)
    assert str(caught.value) == (
        "main is up but not healthy (api: HTTP 502)\nrun `wt demo down` to clean up"
    )
    assert "up feat/cart web,worker" not in harness.events


@pytest.mark.parametrize("error", [WtError("boom"), KeyboardInterrupt()])
def test_environment_is_restored_after_a_failed_or_interrupted_up(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    monkeypatch.setenv("WT_TERMINAL", "xterm")
    before = dict(os.environ)

    def explode(args: list[str], **kwargs: Any) -> proc.Result:
        raise error

    monkeypatch.setattr(proc, "run", explode)
    with pytest.raises(type(error)):
        run_up(harness)
    assert dict(os.environ) == before


def test_environment_is_restored_after_a_successful_up(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WT_HOME", "/visitor/home")
    before = dict(os.environ)
    run_up(harness)
    assert dict(os.environ) == before


# ---- down -------------------------------------------------------------------------------


def make_demo_home(harness: Harness, *, with_catalog: bool = True) -> None:
    shop = harness.home / "shop"
    shop.mkdir(parents=True)
    harness.network_present = True
    if with_catalog:
        (shop / "catalog.yaml").write_text("project: shop\n")


def test_down_without_a_demo_removes_nothing(harness: Harness) -> None:
    assert demo.down(lambda m: None) is False
    assert harness.events == []


def test_down_removes_branches_servers_network_and_the_home_in_order(harness: Harness) -> None:
    make_demo_home(harness)
    assert demo.down(lambda m: None) is True
    assert harness.events == [
        "remove main force=True",
        "remove feat/cart force=True",
        "compose down wtdemo-postgres",
        "compose down wtdemo-redis",
        "compose down wtdemo-kafka",
        "compose down wtdemo-keycloak",
        "compose down wtdemo-traefik",
        "network rm",
    ]
    assert harness.network_removals == ["wtdemo"]
    assert not harness.home.exists()
    assert all(env["WT_INSTANCE"] == "wtdemo" for env in harness.envs.values())


def test_down_without_a_home_still_tears_down_docker_when_the_network_exists(
    harness: Harness,
) -> None:
    harness.network_present = True
    assert demo.down(lambda m: None) is True
    assert harness.events == [
        "compose down wtdemo-postgres",
        "compose down wtdemo-redis",
        "compose down wtdemo-kafka",
        "compose down wtdemo-keycloak",
        "compose down wtdemo-traefik",
        "network rm",
    ]
    assert harness.network_removals == ["wtdemo"]
    assert all(env["WT_INSTANCE"] == "wtdemo" for env in harness.envs.values())


def test_down_without_a_home_or_a_network_is_no_demo(harness: Harness) -> None:
    assert demo.down(lambda m: None) is False
    assert harness.events == []


def test_down_twice_is_safe(harness: Harness) -> None:
    make_demo_home(harness)
    assert demo.down(lambda m: None) is True
    assert demo.down(lambda m: None) is False


def test_down_after_a_half_created_demo_still_cleans_docker_and_files(harness: Harness) -> None:
    make_demo_home(harness, with_catalog=False)
    assert demo.down(lambda m: None) is True
    assert not any(e.startswith("remove") for e in harness.events)
    assert harness.compose_downs[-1] == "wtdemo-traefik"
    assert not harness.home.exists()


def test_down_restores_the_environment(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_TERMINAL", "xterm")
    make_demo_home(harness)
    before = dict(os.environ)
    demo.down(lambda m: None)
    assert dict(os.environ) == before


def test_down_keeps_the_home_when_docker_fails(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_demo_home(harness)

    def broken(project: str) -> None:
        raise WtError("docker is not answering")

    monkeypatch.setattr(compose, "down", broken)
    with pytest.raises(WtError, match="not answering"):
        demo.down(lambda m: None)
    assert harness.home.exists()  # a second `wt demo down` can finish the job


def test_failing_to_delete_the_home_is_a_wterror(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_demo_home(harness)

    def denied(folder: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(shutil, "rmtree", denied)
    with pytest.raises(WtError, match=r"cannot delete .*wt-demo: denied"):
        demo.down(lambda m: None)
