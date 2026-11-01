from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import write_global_catalog
from wt.catalog import load_project
from wt.config import Config, LoadedConfig, Shared
from wt.errors import WtError

SHARED = {"postgres": {"databases": ["shop"]}, "kafka": {"groups": ["orders"]}}

BASE: dict[str, Any] = {
    "project": "shop",
    "domain": "shop.localhost",
    "repos": {"api": "../shop-api", "web": "../shop-web"},
    "services": {
        "api": {
            "repo": "api",
            "compose": "docker-compose.yml",
            "port": 8000,
            "subdomain": "api",
            "env": {"X": "{{ url }}"},
            "infra": SHARED,
        },
        "web": {
            "repo": "web",
            "run": "npm run dev -- --port {{ port }}",
            "port": "auto",
            "subdomain": "",
        },
    },
}


def load(tmp_path: Path, data: dict[str, Any]) -> LoadedConfig:
    """Write `data` as the catalog and load it with every service selected."""
    return write_global_catalog(tmp_path, data, uses=list(data["services"]))


def with_service(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(yaml.safe_dump(BASE))
    data["services"]["api"].update(changes)
    return data


def with_run_service(**changes: Any) -> dict[str, Any]:
    data = with_service()
    data["services"]["web"].update(changes)
    return data


def with_infra(infra: dict[str, Any]) -> dict[str, Any]:
    """BASE with `infra` as the shared servers `api` needs."""
    return with_service(infra=infra)


def test_loads_spec_example(tmp_path: Path) -> None:
    loaded = load(tmp_path, BASE)
    cfg = loaded.config
    assert cfg.project == "shop"
    assert cfg.services["web"].port == "auto"
    assert cfg.shared.configured() == ["postgres", "kafka"]
    assert loaded.repo_path("api") == (tmp_path / "../shop-api").resolve()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (with_service(repo="nope"), "unknown repo"),
        (with_service(run="x"), "exactly one of"),
        (with_service(port="auto"), "only for 'run'"),
        (with_service(port=None), "needs a 'port'"),
        (with_service(port=70000), "between 1 and 65535"),
        (with_service(subdomain=""), "same subdomain"),
        (with_service(subdomain="Bad_Label"), "subdomain"),
        (with_service(health="healthz"), "starting with '/'"),
        (with_service(typo=1), "typo"),
        (with_service(setup="make deps"), "setup: needs run:"),
        (with_service(setup_timeout=30), "setup_timeout: needs run:"),
        (with_run_service(setup_timeout=0), "setup_timeout"),
        (with_run_service(setup_timeout=3601), "setup_timeout"),
        ({**BASE, "project": "Shop"}, "project"),
        ({**BASE, "domain": "localhost"}, "domain"),
        (with_infra({"keycloak": {"realm": "a/b", "client": "web"}}), "realm"),
        (with_infra({"keycloak": {"realm": "shop", "client": "we b"}}), "client"),
        (with_infra({"keycloak": {"realm": "", "client": "web"}}), "realm"),
    ],
)
def test_rejects_invalid(tmp_path: Path, data: dict[str, Any], message: str) -> None:
    with pytest.raises(WtError, match=message):
        load(tmp_path, data)


def test_one_compose_file_per_repo(tmp_path: Path) -> None:
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["services"]["worker"] = {"repo": "api", "compose": "other.yml"}
    with pytest.raises(WtError, match="two compose files"):
        load(tmp_path, data)


def test_bad_yaml(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text("project: [unclosed")
    with pytest.raises(WtError, match="not valid YAML"):
        load_project(path)


def test_postgres_base_branch_default_and_override(tmp_path: Path) -> None:
    pg = load(tmp_path, BASE).config.shared.postgres
    assert pg is not None and pg.base_branch == "main"
    data = with_infra({"postgres": {"databases": ["shop"], "base_branch": "develop"}})
    pg = load(tmp_path, data).config.shared.postgres
    assert pg is not None and pg.base_branch == "develop"


def test_postgres_base_branch_must_be_a_string(tmp_path: Path) -> None:
    data = with_infra({"postgres": {"databases": ["shop"], "base_branch": ["main"]}})
    with pytest.raises(WtError, match="base_branch"):
        load(tmp_path, data)


def _needs(**needs: list[str]) -> dict:  # type: ignore[type-arg]
    return {
        "project": "p",
        "domain": "p.localhost",
        "repos": {"r": "."},
        "services": {n: {"repo": "r", "compose": "c.yml", "needs": v} for n, v in needs.items()},
    }


def test_needs_ok() -> None:
    assert Config.model_validate(_needs(a=["b"], b=[])).services["a"].needs == ["b"]


def test_needs_unknown() -> None:
    with pytest.raises(ValueError, match="unknown service 'x'"):
        Config.model_validate(_needs(a=["x"]))


def test_needs_cycle_message() -> None:
    with pytest.raises(ValueError, match="a -> b -> a"):
        Config.model_validate(_needs(a=["b"], b=["a"]))


@pytest.mark.parametrize("bad", ["-x", ".", "..", ".hidden"])
def test_keycloak_realm_and_client_reject_dash_or_dot_first(tmp_path: Path, bad: str) -> None:
    for key in ("realm", "client"):
        keycloak = {"realm": "shop", "client": "web", key: bad}
        data = with_infra({"keycloak": keycloak})
        with pytest.raises(WtError, match=key):
            load(tmp_path, data)


@pytest.mark.parametrize("key", ["realm", "client"])
def test_keycloak_name_accepts_64_characters(tmp_path: Path, key: str) -> None:
    keycloak = {"realm": "shop", "client": "web", key: "a" * 64}
    cfg = load(tmp_path, with_infra({"keycloak": keycloak})).config
    assert cfg.shared.keycloak is not None
    assert getattr(cfg.shared.keycloak, key) == "a" * 64


@pytest.mark.parametrize("key", ["realm", "client"])
def test_keycloak_name_rejects_65_characters_naming_the_field(tmp_path: Path, key: str) -> None:
    keycloak = {"realm": "shop", "client": "web", key: "a" * 65}
    with pytest.raises(WtError, match=rf"infra\.keycloak\.{key}: .*at most 64 characters"):
        load(tmp_path, with_infra({"keycloak": keycloak}))


def test_keycloak_realm_and_client_accept_dots_dashes_and_case(tmp_path: Path) -> None:
    data = with_infra({"keycloak": {"realm": "Shop.v1_x-2", "client": "Web-App"}})
    cfg = load(tmp_path, data).config
    assert cfg.shared.keycloak is not None
    assert cfg.shared.keycloak.realm == "Shop.v1_x-2"


# --- LoadedConfig.with_uses for a project catalog ---
CATALOG = """\
project: notes
domain: notes.localhost
repos: {r: .}
services:
  back: {repo: r, run: x, port: auto, subdomain: api}
  front: {repo: r, run: y, port: auto, subdomain: "", consumes: {back: API_URL}}
  admin: {repo: r, run: z, port: auto, subdomain: admin}
"""


def test_with_uses_on_a_project_catalog_re_resolves_it(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text(CATALOG)
    loaded = load_project(path, ["front"])
    other = loaded.with_uses(["admin"])
    assert other.uses == ("admin",)
    assert list(other.config.services) == ["admin"]
    assert other.path == other.catalog == path
    assert other.service_order == ("back", "front", "admin")
    assert loaded.uses == ("front",)  # the original is untouched
    assert list(loaded.with_uses(["front"]).config.services) == ["front", "back"]


def test_with_uses_on_a_project_catalog_errors(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text(CATALOG)
    loaded = load_project(path)
    with pytest.raises(WtError, match="select at least one service"):
        loaded.with_uses([])
    with pytest.raises(WtError, match="unknown service 'nope'"):
        loaded.with_uses(["nope"])


def test_setup_defaults_keep_old_files_valid(tmp_path: Path) -> None:
    web = load(tmp_path, BASE).config.services["web"]
    assert web.setup is None
    assert web.setup_timeout == 600


def test_setup_flows_into_the_resolved_service(tmp_path: Path) -> None:
    data = with_run_service(setup="npm ci", setup_timeout=3600)
    loaded = load(tmp_path, data)
    web = loaded.config.services["web"]
    assert (web.setup, web.setup_timeout) == ("npm ci", 3600)
    api = loaded.config.services["api"]
    assert (api.setup, api.setup_timeout) == (None, 600)


def test_setup_timeout_lower_bound(tmp_path: Path) -> None:
    web = load(tmp_path, with_run_service(setup="x", setup_timeout=1)).config.services["web"]
    assert web.setup_timeout == 1


# --- plugin infra keys: everything that is not a built-in section lands in Shared.extra ---


def test_a_plugin_key_lands_in_extra() -> None:
    shared = Shared.model_validate({"redis": {}, "echo": {"file": "/tmp/e.log"}})
    assert shared.extra == {"echo": {"file": "/tmp/e.log"}}
    assert shared.redis is not None


def test_no_plugin_key_leaves_extra_empty() -> None:
    assert Shared.model_validate({"redis": {}}).extra == {}
    assert Shared().extra == {}


def test_a_plugin_key_must_be_a_mapping() -> None:
    with pytest.raises(ValueError, match=r"infra\.echo: must be a mapping"):
        Shared.model_validate({"echo": ["a"]})
    with pytest.raises(ValueError, match=r"infra\.echo: must be a mapping"):
        Shared.model_validate({"echo": None})


def test_extra_given_explicitly_is_kept_and_merged_with_loose_keys() -> None:
    shared = Shared.model_validate({"extra": {"a": {"x": 1}}, "b": {"y": 2}})
    assert shared.extra == {"a": {"x": 1}, "b": {"y": 2}}


def test_extra_must_be_a_mapping_of_mappings() -> None:
    with pytest.raises(ValueError, match="extra"):
        Shared.model_validate({"extra": ["a"]})


def test_built_in_sections_keep_their_validation() -> None:
    with pytest.raises(ValueError, match="databases"):
        Shared.model_validate({"postgres": {"databases": "not-a-list"}})


def test_configured_lists_built_ins_then_sorted_extra_keys() -> None:
    shared = Shared.model_validate(
        {"zeta": {}, "keycloak": {"realm": "r", "client": "c"}, "alpha": {}, "redis": {}}
    )
    assert shared.configured() == ["redis", "keycloak", "alpha", "zeta"]


def test_extra_round_trips_through_model_dump() -> None:
    shared = Shared.model_validate({"redis": {}, "echo": {"file": "f"}, "other": {}})
    assert Shared.model_validate(shared.model_dump()) == shared


def test_a_plugin_key_survives_load_project(tmp_path: Path) -> None:
    cfg = load(tmp_path, with_infra({**SHARED, "echo": {"file": "/tmp/e.log"}})).config
    assert cfg.shared.extra == {"echo": {"file": "/tmp/e.log"}}
    assert cfg.shared.configured() == ["postgres", "kafka", "echo"]


def test_a_non_mapping_plugin_key_is_a_load_error(tmp_path: Path) -> None:
    with pytest.raises(WtError, match=r"infra\.echo: must be a mapping"):
        load(tmp_path, with_infra({"echo": "yes"}))
