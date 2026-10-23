from pathlib import Path

import pytest

from wt.catalog import catalog_path, load_catalog
from wt.errors import WtError

GOOD = """\
repos:
  notes-back: ./back
  notes-front: ~/code/front
services:
  back:
    repo: notes-back
    compose: docker-compose.yml
    port: 8000
    subdomain: api
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


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(text)
    return path


def test_path_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("WT_CATALOG", raising=False)
    assert catalog_path(None, None) == tmp_path / "home" / "catalog.yaml"
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "env.yaml"))
    assert catalog_path(None, None) == tmp_path / "env.yaml"
    assert catalog_path("./c.yaml", tmp_path / "proj") == tmp_path / "proj" / "c.yaml"
    assert catalog_path("/abs/c.yaml", tmp_path) == Path("/abs/c.yaml")


def test_path_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/h")
    assert catalog_path("~/c.yaml", Path("/proj")) == Path("/h/c.yaml")


def test_load_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/h")
    loaded = load_catalog(write(tmp_path, GOOD))
    assert loaded.repos["notes-back"] == tmp_path / "back"
    assert loaded.repos["notes-front"] == Path("/h/code/front")
    assert all(p.is_absolute() for p in loaded.repos.values())
    back = loaded.catalog.services["back"]
    assert back.infra.postgres is not None
    assert back.infra.postgres.databases == ["notes"]
    assert loaded.catalog.services["front"].consumes == {"back": "API_URL"}


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(WtError, match="catalog not found"):
        load_catalog(tmp_path / "nope.yaml")


def test_bad_yaml_and_not_mapping(tmp_path: Path) -> None:
    with pytest.raises(WtError, match="not valid YAML"):
        load_catalog(write(tmp_path, "a: [\n"))
    with pytest.raises(WtError, match="must contain a YAML mapping"):
        load_catalog(write(tmp_path, "- a\n"))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("repo: notes-back", "repo: nope", "unknown repo 'nope'"),
        ("  back:\n", "  Back:\n", "must match"),
        ("notes-front: ~/code/front", "Bad: ~/x", "must match"),
        ("      back: API_URL", "      ghost: API_URL", "consumes unknown service 'ghost'"),
        ("      back: API_URL", "      front: API_URL", "cannot consume itself"),
        ("      back: API_URL", "      back: api_url", "environment variable"),
        ("    compose: docker-compose.yml\n", "", "exactly one of 'compose' or 'run'"),
        ("    port: 8000\n", "", "needs a 'port'"),
        ("    port: 8000\n", "    port: 8000\n    colour: red\n", "colour"),
        ("postgres:", "mysql:", "mysql"),
        ("notes-back: ./back", 'notes-back: " "', "empty path"),
    ],
)
def test_validation_errors(tmp_path: Path, old: str, new: str, message: str) -> None:
    assert old in GOOD
    with pytest.raises(WtError, match=message) as err:
        load_catalog(write(tmp_path, GOOD.replace(old, new, 1)))
    assert "invalid catalog.yaml:" in str(err.value)


def test_extra_top_level_key(tmp_path: Path) -> None:
    with pytest.raises(WtError, match=r"invalid catalog\.yaml"):
        load_catalog(write(tmp_path, GOOD + "other: 1\n"))


def test_two_consumed_services_cannot_share_an_env_var() -> None:
    from pydantic import ValidationError

    from wt.catalog import CatalogService

    with pytest.raises(ValidationError, match="same environment variable"):
        CatalogService.model_validate(
            {"repo": "r", "run": "x", "consumes": {"a": "API_URL", "b": "API_URL"}}
        )
