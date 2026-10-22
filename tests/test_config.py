from pathlib import Path
from typing import Any

import pytest
import yaml

from wt.config import STARTER, find_config, load_config
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
