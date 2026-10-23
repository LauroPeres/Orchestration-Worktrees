from pathlib import Path

import pytest

from wt.catalog import catalog_path, load_catalog
from wt.config import load_config
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


# --- resolve (uses:) ---
def project(tmp_path: Path, catalog: str, extra: str = "uses: [front]\n") -> Path:
    (tmp_path / "catalog.yaml").write_text(catalog)
    cfg = tmp_path / "worktree.yaml"
    cfg.write_text(f"project: notes\ndomain: notes.localhost\ncatalog: ./catalog.yaml\n{extra}")
    return cfg


def test_uses_resolves(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, GOOD))
    cfg = loaded.config
    assert loaded.catalog == tmp_path / "catalog.yaml"
    assert list(cfg.services) == ["front", "back"]  # back comes in through consumes
    assert cfg.services["front"].needs == ["back"]
    assert cfg.services["front"].env == {"API_URL": "{{ services.back.url }}"}
    assert cfg.repos["notes-back"] == str(tmp_path / "back")
    assert cfg.shared.postgres is not None and cfg.shared.postgres.databases == ["notes"]
    assert loaded.repo_path("notes-back") == tmp_path / "back"


def test_uses_only_closure(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, GOOD, "uses: [back]\n"))
    assert list(loaded.config.services) == ["back"]
    assert list(loaded.config.repos) == ["notes-back"]


@pytest.mark.parametrize(
    ("extra", "msg"),
    [
        ("uses: [front]\nservices: {}\n", "exactly one"),
        ("uses: [front]\nshared: {}\n", "'shared'"),
        ("uses: [nope]\n", "unknown service 'nope'"),
        ("uses: []\n", "non-empty"),
        ("uses: [front]\nrepos: {x: .}\n", "repos"),
    ],
)
def test_uses_errors(tmp_path: Path, extra: str, msg: str) -> None:
    with pytest.raises(WtError, match=msg):
        load_config(project(tmp_path, GOOD, extra))


def test_consumes_dep_without_route(tmp_path: Path) -> None:
    text = GOOD.replace("    subdomain: api\n", "")
    with pytest.raises(WtError, match="no 'subdomain'"):
        load_config(project(tmp_path, text))


def test_consumes_var_already_in_env(tmp_path: Path) -> None:
    text = GOOD.replace("    consumes:", "    env: {API_URL: x}\n    consumes:")
    with pytest.raises(WtError, match="API_URL"):
        load_config(project(tmp_path, text))


def two_infra(a: str, b: str) -> str:
    return f"""\
repos: {{r: ./r}}
services:
  one:
    repo: r
    compose: c.yml
    port: 80
    subdomain: one
    infra:
      postgres: {a}
  two:
    repo: r
    compose: c.yml
    consumes: {{one: ONE_URL}}
    infra:
      postgres: {b}
"""


def test_infra_union_and_conflict(tmp_path: Path) -> None:
    ok = two_infra("{databases: [a]}", "{databases: [b, a], strategy: dump-restore}")
    shared = load_config(project(tmp_path, ok, "uses: [two]\n")).config.shared
    assert shared.postgres is not None
    assert shared.postgres.databases == ["b", "a"]  # discovery order: two, then one
    assert shared.postgres.strategy == "dump-restore"
    bad = two_infra(
        "{databases: [a], strategy: template}", "{databases: [b], strategy: dump-restore}"
    )
    with pytest.raises(WtError, match=r"'one'.*'two'|'two'.*'one'"):
        load_config(project(tmp_path, bad, "uses: [two]\n"))


def test_neither_services_nor_uses(tmp_path: Path) -> None:
    cfg = project(tmp_path, GOOD, "")
    with pytest.raises(WtError, match="set exactly one of 'services' or 'uses'"):
        load_config(cfg)


def test_consumes_cycle_message(tmp_path: Path) -> None:
    text = GOOD.replace(
        "    subdomain: api\n", "    subdomain: api\n    consumes: {front: FRONT_URL}\n"
    )
    with pytest.raises(WtError, match="cycle through catalog consumes: front -> back -> front"):
        load_config(project(tmp_path, text))


DIAMOND = """\
repos: {r: ./r}
services:
  top:
    repo: r
    compose: c.yml
    consumes: {left: LEFT, right: RIGHT}
  left:
    repo: r
    compose: c.yml
    port: 80
    subdomain: left
    consumes: {base: BASE}
  right:
    repo: r
    compose: c.yml
    port: 81
    subdomain: right
    consumes: {base: BASE}
  base:
    repo: r
    compose: c.yml
    port: 82
    subdomain: base
"""


def test_diamond_consumes_has_no_duplicates(tmp_path: Path) -> None:
    cfg = load_config(project(tmp_path, DIAMOND, "uses: [top, base]\n")).config
    assert sorted(cfg.services) == ["base", "left", "right", "top"]
    assert cfg.services["top"].needs == ["left", "right"]
    assert cfg.services["left"].needs == ["base"]
    assert list(cfg.repos) == ["r"]


def test_rows(tmp_path: Path) -> None:
    from wt.catalog import rows

    (tmp_path / "catalog.yaml").write_text(GOOD)
    result = rows(load_catalog(tmp_path / "catalog.yaml").catalog)
    assert [(r.service, r.repo, r.kind, r.infra, r.consumes, r.consumed_by) for r in result] == [
        ("back", "notes-back", "compose", ["postgres"], [], ["front"]),
        ("front", "notes-front", "run", [], ["back"], []),
    ]
