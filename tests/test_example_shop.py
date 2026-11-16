import subprocess
from pathlib import Path

import pytest
import yaml

from wt.catalog import load_catalog, load_project, render_check
from wt.naming import Names
from wt.providers.base import BranchContext
from wt.providers.kafka import KafkaProvider
from wt.state import StateStore

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "shop"


@pytest.fixture
def target(tmp_path: Path) -> Path:
    stack = tmp_path / "shop"
    subprocess.run([str(EXAMPLE / "bootstrap.sh"), str(stack)], check=True, capture_output=True)
    return stack


def test_bootstrap_writes_a_catalog_and_a_legacy_file(target: Path) -> None:
    assert (target / "catalog.yaml").is_file()
    assert (target / "legacy" / "worktree.yaml").is_file()
    assert not (target / "worktree.yaml").exists()
    legacy = (target / "legacy" / "worktree.yaml").read_text()
    assert "{ api: ../shop-api, web: ../shop-web }" in legacy  # relative to its own folder


@pytest.mark.parametrize(
    ("uses", "stack"),
    [
        (["api"], {"api"}),
        (["worker"], {"worker"}),
        (["web"], {"web", "api"}),  # web consumes api
        (["api", "worker", "web"], {"api", "worker", "web"}),
    ],
)
def test_every_selection_of_the_catalog_loads(
    target: Path, uses: list[str], stack: set[str]
) -> None:
    loaded = load_project(target / "catalog.yaml", uses=uses)

    assert set(loaded.config.services) == stack
    assert (loaded.config.project, loaded.config.domain) == ("shop", "shop.localhost")
    assert loaded.repo_path("api") == (target / "shop-api").resolve()


def test_the_full_stack_keeps_the_old_shared_servers(target: Path) -> None:
    cfg = load_project(target / "catalog.yaml", uses=["api", "worker", "web"]).config

    assert cfg.shared.postgres is not None and cfg.shared.postgres.databases == ["shop"]
    assert cfg.shared.kafka is not None
    assert (cfg.shared.kafka.groups, cfg.shared.kafka.topic_prefix) == (["worker"], True)
    assert cfg.shared.redis is not None
    assert cfg.shared.keycloak is not None
    assert (cfg.shared.keycloak.realm, cfg.shared.keycloak.client) == ("shop", "web")
    assert cfg.services["web"].needs == ["api"]
    assert cfg.services["web"].env["API_URL"] == "{{ services['api'].url }}"
    assert cfg.services["api"].env["WEB_ORIGIN"] == "{{ services.web.url }}"


def test_the_api_selection_alone_carries_the_info_page_variables(target: Path) -> None:
    loaded = load_project(target / "catalog.yaml", uses=["api"])
    api = loaded.config.services["api"]

    assert set(api.env) >= {"REDIS_URL", "REDIS_PREFIX", "WORKER_KAFKA_GROUP", "KAFKA_TOPIC"}
    assert loaded.config.shared.redis is not None
    assert loaded.config.shared.kafka is not None
    assert loaded.config.shared.kafka.groups == ["worker"]


def test_every_template_of_the_catalog_renders(target: Path) -> None:
    assert render_check(load_catalog(target / "catalog.yaml")) == {}


def test_the_api_can_name_the_workers_group_without_a_worker(target: Path) -> None:
    # kafka.group() raises for a group the selection does not list, which is why the api lists it.
    config = load_project(target / "catalog.yaml", uses=["api"]).config
    branch = BranchContext(
        Names("shop", "feat/cart", "shop.localhost"), "feat/cart", "main", StateStore("shop")
    )

    assert KafkaProvider(config, lambda message: None).group("worker", branch) == (
        "worker-shop.feat-cart"
    )


def test_the_api_and_the_worker_agree_on_the_groups_and_redis_prefix(target: Path) -> None:
    services = load_project(target / "catalog.yaml", uses=["api", "worker"]).config.services

    assert services["api"].env["WORKER_KAFKA_GROUP"] == services["worker"].env["KAFKA_GROUP"]
    assert services["api"].env["REDIS_PREFIX"] == services["worker"].env["REDIS_PREFIX"]
    assert services["api"].env["KAFKA_TOPIC"] == services["worker"].env["KAFKA_TOPIC"]


def test_the_legacy_file_gives_the_api_the_same_environment(target: Path) -> None:
    catalog = load_project(target / "catalog.yaml", uses=["api"]).config.services["api"].env
    legacy = yaml.safe_load((target / "legacy" / "worktree.yaml").read_text())

    assert legacy["services"]["api"]["env"] == catalog


@pytest.mark.parametrize("service", ["api", "feat-cart"])
def test_both_api_versions_serve_info_and_the_worker_keeps_a_recent_feed(service: str) -> None:
    source = (EXAMPLE / ("api" if service == "api" else "feat-cart") / "app.py").read_text()
    worker = (EXAMPLE / "api" / "worker.py").read_text()

    assert '@app.get("/info")' in source
    assert "{REDIS_PREFIX}recent" in source
    assert 'r.lpush(f"{REDIS_PREFIX}recent", item)' in worker
    assert 'r.ltrim(f"{REDIS_PREFIX}recent", 0, RECENT_ITEMS - 1)' in worker
