import subprocess
from pathlib import Path

import pytest

from wt.catalog import load_project

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
