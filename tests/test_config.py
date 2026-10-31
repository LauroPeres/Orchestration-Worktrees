from pathlib import Path
from typing import Any

import pytest
import yaml

from wt.config import STARTER, Config, find_config, load_config
from wt.errors import WtError

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
        },
        "web": {
            "repo": "web",
            "run": "npm run dev -- --port {{ port }}",
            "port": "auto",
            "subdomain": "",
        },
    },
    "shared": {"postgres": {"databases": ["shop"]}, "kafka": {"groups": ["orders"]}},
}


def write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "worktree.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def with_service(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(yaml.safe_dump(BASE))
    data["services"]["api"].update(changes)
    return data


def test_loads_spec_example(tmp_path: Path) -> None:
    loaded = load_config(write(tmp_path, BASE))
    cfg = loaded.config
    assert cfg.project == "shop"
    assert cfg.services["web"].port == "auto"
    assert cfg.shared.configured() == ["postgres", "kafka"]
    assert loaded.repo_path("api") == (tmp_path / "../shop-api").resolve()


def test_starter_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "worktree.yaml"
    path.write_text(STARTER.format(project="demo"))
    assert load_config(path).config.services["app"].env["PUBLIC_URL"] == "{{ url }}"


def test_starter_examples_validate_when_uncommented(tmp_path: Path) -> None:
    text = STARTER.format(project="demo")
    base, marker, rest = text.partition("# --- examples")
    assert marker
    examples = rest.splitlines()[1:]  # drop the rest of the marker line
    assert all(line.startswith("# ") for line in examples)
    assert "#   web:" in examples
    assert "#   postgres:" in examples
    uncommented = "\n".join(line.removeprefix("# ") for line in examples)
    path = tmp_path / "worktree.yaml"
    path.write_text(base + uncommented + "\n")
    cfg = load_config(path).config
    web = cfg.services["web"]
    assert web.run == "npm run dev -- --host 0.0.0.0 --port {{ port }}"
    assert web.port == "auto"
    assert web.env["DATABASE_URL"] == "{{ postgres.url('app') }}"
    assert cfg.shared.postgres is not None
    assert cfg.shared.postgres.databases == ["app"]
    assert cfg.shared.postgres.base_branch == "main"


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
        ({**BASE, "project": "Shop"}, "project"),
        ({**BASE, "domain": "localhost"}, "domain"),
        ({**BASE, "shared": {"keycloak": {"realm": "a/b", "client": "web"}}}, "realm"),
        ({**BASE, "shared": {"keycloak": {"realm": "shop", "client": "we b"}}}, "client"),
        ({**BASE, "shared": {"keycloak": {"realm": "", "client": "web"}}}, "realm"),
    ],
)
def test_rejects_invalid(tmp_path: Path, data: dict[str, Any], message: str) -> None:
    with pytest.raises(WtError, match=message):
        load_config(write(tmp_path, data))


def test_one_compose_file_per_repo(tmp_path: Path) -> None:
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["services"]["worker"] = {"repo": "api", "compose": "other.yml"}
    with pytest.raises(WtError, match="two compose files"):
        load_config(write(tmp_path, data))


def test_bad_yaml(tmp_path: Path) -> None:
    path = tmp_path / "worktree.yaml"
    path.write_text("project: [unclosed")
    with pytest.raises(WtError, match="not valid YAML"):
        load_config(path)


def test_find_config_walks_up(tmp_path: Path) -> None:
    path = write(tmp_path, BASE)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert find_config(deep) == path


def test_postgres_base_branch_default_and_override(tmp_path: Path) -> None:
    pg = load_config(write(tmp_path, BASE)).config.shared.postgres
    assert pg is not None and pg.base_branch == "main"
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["shared"]["postgres"]["base_branch"] = "develop"
    pg = load_config(write(tmp_path, data)).config.shared.postgres
    assert pg is not None and pg.base_branch == "develop"


def test_postgres_base_branch_must_be_a_string(tmp_path: Path) -> None:
    data = yaml.safe_load(yaml.safe_dump(BASE))
    data["shared"]["postgres"]["base_branch"] = ["main"]
    with pytest.raises(WtError, match="base_branch"):
        load_config(write(tmp_path, data))


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
        data = {**BASE, "shared": {"keycloak": keycloak}}
        with pytest.raises(WtError, match=key):
            load_config(write(tmp_path, data))


@pytest.mark.parametrize("key", ["realm", "client"])
def test_keycloak_name_accepts_64_characters(tmp_path: Path, key: str) -> None:
    keycloak = {"realm": "shop", "client": "web", key: "a" * 64}
    cfg = load_config(write(tmp_path, {**BASE, "shared": {"keycloak": keycloak}})).config
    assert cfg.shared.keycloak is not None
    assert getattr(cfg.shared.keycloak, key) == "a" * 64


@pytest.mark.parametrize("key", ["realm", "client"])
def test_keycloak_name_rejects_65_characters_naming_the_field(tmp_path: Path, key: str) -> None:
    keycloak = {"realm": "shop", "client": "web", key: "a" * 65}
    with pytest.raises(WtError, match=rf"shared\.keycloak\.{key}: .*at most 64 characters"):
        load_config(write(tmp_path, {**BASE, "shared": {"keycloak": keycloak}}))


def test_keycloak_realm_and_client_accept_dots_dashes_and_case(tmp_path: Path) -> None:
    data = {**BASE, "shared": {"keycloak": {"realm": "Shop.v1_x-2", "client": "Web-App"}}}
    cfg = load_config(write(tmp_path, data)).config
    assert cfg.shared.keycloak is not None
    assert cfg.shared.keycloak.realm == "Shop.v1_x-2"
