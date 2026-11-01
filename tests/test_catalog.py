from pathlib import Path
from typing import Any

import pytest
import yaml

from wt.catalog import (
    Catalog,
    MergeResult,
    catalog_path,
    expand_uses,
    load_catalog,
    load_project,
    merge_legacy,
    read_legacy,
)
from wt.config import load_config
from wt.errors import WtError
from wt.templating import render

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
    remote: https://api.staging.example
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
    loaded = load_config(project(tmp_path, GOOD, "uses: [front, back]\n"))
    cfg = loaded.config
    assert loaded.catalog == tmp_path / "catalog.yaml"
    assert list(cfg.services) == ["front", "back"]  # selection order
    assert cfg.services["front"].needs == ["back"]
    assert cfg.services["front"].env == {"API_URL": "{{ services['back'].url }}"}
    assert cfg.repos["notes-back"] == str(tmp_path / "back")
    assert cfg.shared.postgres is not None and cfg.shared.postgres.databases == ["notes"]
    assert loaded.repo_path("notes-back") == tmp_path / "back"
    assert loaded.remotes == {}


IGNORED_BACK = "remote of 'back' is ignored: 'front' needs it, so it runs locally"


def test_uses_pulls_in_the_dependency_and_ignores_its_remote(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, GOOD, "uses: [front]\n"))
    cfg = loaded.config
    assert list(cfg.services) == ["front", "back"]  # the selection, then its dependencies
    assert sorted(cfg.repos) == ["notes-back", "notes-front"]
    assert cfg.services["front"].needs == ["back"]
    assert cfg.services["front"].env == {"API_URL": "{{ services['back'].url }}"}
    assert cfg.shared.postgres is not None and cfg.shared.postgres.databases == ["notes"]
    assert loaded.remotes == {}
    assert loaded.warnings == (IGNORED_BACK,)


def test_uses_only_dep(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, GOOD, "uses: [back]\n"))
    assert list(loaded.config.services) == ["back"]
    assert list(loaded.config.repos) == ["notes-back"]
    assert loaded.remotes == {"front": ""}  # nothing consumes front, so it has no remote
    assert loaded.warnings == ()  # front is not in the stack: back's remote is not ignored


def test_consumed_service_needs_no_remote(tmp_path: Path) -> None:
    text = GOOD.replace("    remote: https://api.staging.example\n", "")
    loaded = load_config(project(tmp_path, text, "uses: [front]\n"))
    assert list(loaded.config.services) == ["front", "back"]
    assert loaded.config.services["front"].env == {"API_URL": "{{ services['back'].url }}"}
    assert loaded.warnings == ()
    assert load_catalog(write(tmp_path, text)).catalog.services["back"].remote is None


def test_remote_must_be_http_url(tmp_path: Path) -> None:
    text = GOOD.replace("https://api.staging.example", "api.staging.example")
    with pytest.raises(WtError, match="remote"):
        load_catalog(write(tmp_path, text))


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
        load_config(project(tmp_path, text, "uses: [front]\n"))


def test_consumes_var_already_in_env(tmp_path: Path) -> None:
    text = GOOD.replace("    consumes:", "    env: {API_URL: x}\n    consumes:")
    with pytest.raises(WtError, match="API_URL"):
        load_config(project(tmp_path, text, "uses: [front]\n"))


def two_infra(a: str, b: str) -> str:
    return f"""\
repos: {{r: ./r}}
services:
  one:
    repo: r
    compose: c.yml
    port: 80
    subdomain: one
    remote: https://one.example
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
    shared = load_config(project(tmp_path, ok, "uses: [two, one]\n")).config.shared
    assert shared.postgres is not None
    assert shared.postgres.databases == ["b", "a"]  # selection order: two, then one
    assert shared.postgres.strategy == "dump-restore"
    bad = two_infra(
        "{databases: [a], strategy: template}", "{databases: [b], strategy: dump-restore}"
    )
    with pytest.raises(WtError, match=r"'one'.*'two'|'two'.*'one'"):
        load_config(project(tmp_path, bad, "uses: [two, one]\n"))
    with pytest.raises(WtError, match=r"'one'.*'two'|'two'.*'one'"):
        load_config(project(tmp_path, bad, "uses: [two]\n"))  # one comes in as a dependency


def test_infra_is_merged_from_the_whole_stack(tmp_path: Path) -> None:
    text = two_infra("{databases: [a]}", "{databases: [b]}")
    loaded = load_config(project(tmp_path, text, "uses: [two]\n"))
    assert list(loaded.config.services) == ["two", "one"]
    assert loaded.config.shared.postgres is not None
    assert loaded.config.shared.postgres.databases == ["b", "a"]
    assert loaded.uses == ("two",)


def test_neither_services_nor_uses(tmp_path: Path) -> None:
    cfg = project(tmp_path, GOOD, "")
    with pytest.raises(WtError, match="set exactly one of 'services' or 'uses'"):
        load_config(cfg)


def test_consumes_cycle_message(tmp_path: Path) -> None:
    text = GOOD.replace(
        "    subdomain: api\n", "    subdomain: api\n    consumes: {front: FRONT_URL}\n"
    ).replace("    consumes:\n", "    remote: https://front.example\n    consumes:\n")
    with pytest.raises(WtError, match="cycle through catalog consumes: front -> back -> front"):
        load_config(project(tmp_path, text, "uses: [front, back]\n"))
    with pytest.raises(WtError, match="cycle through catalog consumes: front -> back -> front"):
        load_config(project(tmp_path, text, "uses: [front]\n"))


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
    remote: https://left.example
    consumes: {base: BASE}
  right:
    repo: r
    compose: c.yml
    port: 81
    subdomain: right
    remote: https://right.example
    consumes: {base: BASE}
  base:
    repo: r
    compose: c.yml
    port: 82
    subdomain: base
    remote: https://base.example
"""


def test_diamond_consumes_has_no_duplicates(tmp_path: Path) -> None:
    cfg = load_config(project(tmp_path, DIAMOND, "uses: [top]\n")).config
    assert list(cfg.services) == ["top", "left", "right", "base"]
    assert cfg.services["top"].needs == ["left", "right"]
    assert cfg.services["left"].needs == ["base"]
    assert list(cfg.repos) == ["r"]


def test_rows(tmp_path: Path) -> None:
    from wt.catalog import rows

    (tmp_path / "catalog.yaml").write_text(GOOD)
    result = rows(load_catalog(tmp_path / "catalog.yaml").catalog)
    assert [
        (r.service, r.repo, r.kind, r.infra, r.consumes, r.consumed_by, r.remote) for r in result
    ] == [
        (
            "back",
            "notes-back",
            "compose",
            ["postgres"],
            [],
            ["front"],
            "https://api.staging.example",
        ),
        ("front", "notes-front", "run", [], ["back"], [], None),
    ]


# --- selection: service_order, service_index, with_uses ---
def test_service_order_follows_the_catalog_not_the_selection(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, GOOD, "uses: [front, back]\n"))
    assert loaded.uses == ("front", "back")
    assert loaded.service_order == ("back", "front")  # catalog order
    assert loaded.service_index("back") == 0
    assert loaded.service_index("front") == 1
    only = loaded.with_uses(["front"])
    assert only.service_index("front") == 1  # unchanged by the selection
    assert only.service_index("back") == 0
    with pytest.raises(WtError, match="unknown service 'nope'"):
        only.service_index("nope")


def test_with_uses_re_resolves(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, GOOD, "uses: [front, back]\n"))
    only = loaded.with_uses(["back"])
    assert only.uses == ("back",)
    assert list(only.config.services) == ["back"]
    assert only.remotes == {"front": ""}
    assert loaded.uses == ("front", "back")  # the original is untouched
    assert only.path == loaded.path


def test_with_uses_errors(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, GOOD, "uses: [front]\n"))
    with pytest.raises(WtError, match="unknown service 'nope'"):
        loaded.with_uses(["nope"])
    with pytest.raises(WtError, match="select at least one service"):
        loaded.with_uses([])


def test_with_uses_reports_infra_conflicts(tmp_path: Path) -> None:
    text = GOOD.replace(
        "    consumes:\n      back: API_URL\n",
        "    consumes:\n      back: API_URL\n"
        "    infra:\n      postgres: {databases: [other], base_branch: dev}\n",
    ).replace("postgres: {databases: [notes]}", "postgres: {databases: [notes], base_branch: main}")
    loaded = load_config(project(tmp_path, text, "uses: [back]\n"))
    with pytest.raises(WtError, match="conflicts"):
        loaded.with_uses(["front"])  # back comes in as a dependency and conflicts


def test_services_project_has_no_selection(tmp_path: Path) -> None:
    path = tmp_path / "worktree.yaml"
    path.write_text(
        "project: p\ndomain: p.localhost\nrepos: {a: .}\nservices:\n"
        "  one: {repo: a, run: x}\n  two: {repo: a, run: y}\n"
    )
    loaded = load_config(path)
    assert loaded.uses is None
    assert loaded.warnings == ()
    assert loaded.remotes == {}
    assert loaded.service_order == ("one", "two")
    assert loaded.service_index("two") == 1
    with pytest.raises(WtError, match="catalog"):
        loaded.with_uses(["one"])


def test_more_than_100_services_rejected(tmp_path: Path) -> None:
    body = "".join(f"  s{i}:\n    repo: r\n    run: x\n" for i in range(101))
    catalog = f"repos:\n  r: ./r\nservices:\n{body}"
    with pytest.raises(WtError, match="at most 100 services"):
        load_config(project(tmp_path, catalog, "uses: [s0]\n"))
    body = "".join(f"  s{i}:\n    repo: a\n    run: x\n" for i in range(101))
    path = tmp_path / "w2.yaml"
    path.write_text(f"project: p\ndomain: p.localhost\nrepos: {{a: .}}\nservices:\n{body}")
    with pytest.raises(WtError, match="at most 100 services"):
        load_config(path)


# --- the stack is the closure of the selection (SPEC section 20) ---
CHAIN = """\
repos: {r: ./r}
services:
  a:
    repo: r
    compose: c.yml
    port: 80
    subdomain: a
    consumes: {b: B_URL}
  b:
    repo: r
    compose: c.yml
    port: 81
    subdomain: b
    consumes: {c: C_URL}
  c:
    repo: r
    compose: c.yml
    port: 82
    subdomain: c
  d:
    repo: r
    compose: c.yml
    port: 83
    subdomain: d
"""


def chain_catalog(tmp_path: Path, text: str = CHAIN) -> Catalog:
    return load_catalog(write(tmp_path, text)).catalog


@pytest.mark.parametrize(
    ("uses", "expected"),
    [
        (["a"], ["a", "b", "c"]),  # transitive
        (["b"], ["b", "c"]),
        (["c"], ["c"]),
        (["d"], ["d"]),  # nothing consumed
        (["a", "d"], ["a", "d", "b", "c"]),  # the selection first, then dependencies
        (["b", "a"], ["b", "a", "c"]),  # an explicit dependency keeps its own position
        (["a", "c"], ["a", "c", "b"]),
        (["a", "a", "b"], ["a", "b", "c"]),  # one entry per service
    ],
)
def test_expand_uses(tmp_path: Path, uses: list[str], expected: list[str]) -> None:
    assert expand_uses(chain_catalog(tmp_path), uses) == expected


def test_expand_uses_diamond_lists_each_dependency_once(tmp_path: Path) -> None:
    catalog = chain_catalog(tmp_path, DIAMOND)
    assert expand_uses(catalog, ["top"]) == ["top", "left", "right", "base"]


def test_expand_uses_unknown_name(tmp_path: Path) -> None:
    with pytest.raises(WtError, match=r"uses: unknown service 'nope' \(not in the catalog\)"):
        expand_uses(chain_catalog(tmp_path), ["a", "nope"])


def test_expand_uses_unknown_name_lists_the_known_services(tmp_path: Path) -> None:
    with pytest.raises(WtError, match=r"Known: a, b, c, d"):
        expand_uses(chain_catalog(tmp_path), ["nope"])


def test_expand_uses_cycle(tmp_path: Path) -> None:
    text = CHAIN.replace("    subdomain: c\n", "    subdomain: c\n    consumes: {a: A_URL}\n")
    with pytest.raises(WtError, match="cycle through catalog consumes: a -> b -> c -> a"):
        expand_uses(chain_catalog(tmp_path, text), ["a"])


def test_closure_is_the_stack_and_uses_stays_explicit(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, CHAIN, "uses: [a]\n"))
    assert list(loaded.config.services) == ["a", "b", "c"]
    assert loaded.uses == ("a",)  # the explicit selection, not the closure
    assert loaded.service_order == ("a", "b", "c", "d")  # catalog position
    assert [loaded.service_index(n) for n in "abcd"] == [0, 1, 2, 3]
    assert loaded.remotes == {"d": ""}
    assert loaded.config.services["a"].needs == ["b"]
    assert loaded.config.services["b"].needs == ["c"]
    assert loaded.config.services["b"].env == {"C_URL": "{{ services['c'].url }}"}


def test_service_index_ignores_the_closure_order(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, CHAIN, "uses: [d, a]\n"))
    assert list(loaded.config.services) == ["d", "a", "b", "c"]
    assert loaded.service_index("d") == 3
    assert loaded.service_index("b") == 1


def test_with_uses_drops_a_dependency_that_was_not_explicit(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, CHAIN, "uses: [a, d]\n"))
    assert list(loaded.config.services) == ["a", "d", "b", "c"]
    dropped = loaded.with_uses(["d"])
    assert list(dropped.config.services) == ["d"]
    assert dropped.uses == ("d",)
    assert dropped.remotes == {"a": "", "b": "", "c": ""}


def test_with_uses_keeps_a_dependency_that_is_explicit(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, CHAIN, "uses: [a, b]\n"))
    kept = loaded.with_uses(["b"])
    assert list(kept.config.services) == ["b", "c"]
    assert kept.uses == ("b",)
    only_a = loaded.with_uses(["a"])
    assert list(only_a.config.services) == ["a", "b", "c"]
    assert only_a.uses == ("a",)


def test_with_uses_unknown_name_after_the_closure(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, CHAIN, "uses: [a]\n"))
    with pytest.raises(WtError, match="unknown service 'nope'"):
        loaded.with_uses(["a", "nope"])


def test_consumes_always_sets_the_local_url_template(tmp_path: Path) -> None:
    for remote in ("", "    remote: https://c.example\n"):
        text = CHAIN.replace("    subdomain: c\n", f"    subdomain: c\n{remote}")
        cfg = load_config(project(tmp_path, text, "uses: [b]\n")).config
        assert cfg.services["b"].env == {"C_URL": "{{ services['c'].url }}"}
        assert cfg.services["b"].needs == ["c"]


def test_remote_of_a_dependency_warns_once_naming_the_first_consumer(tmp_path: Path) -> None:
    text = DIAMOND
    loaded = load_config(project(tmp_path, text, "uses: [top]\n"))
    assert loaded.warnings == (
        "remote of 'left' is ignored: 'top' needs it, so it runs locally",
        "remote of 'right' is ignored: 'top' needs it, so it runs locally",
        "remote of 'base' is ignored: 'left' needs it, so it runs locally",
    )
    assert loaded.remotes == {}


def test_remote_of_a_service_outside_the_stack_is_kept(tmp_path: Path) -> None:
    text = CHAIN.replace("    subdomain: d\n", "    subdomain: d\n    remote: https://d.example\n")
    loaded = load_config(project(tmp_path, text, "uses: [a]\n"))
    assert loaded.remotes == {"d": "https://d.example"}
    assert loaded.warnings == ()


def test_remote_of_an_explicit_dependency_is_not_a_warning(tmp_path: Path) -> None:
    loaded = load_config(project(tmp_path, GOOD, "uses: [front, back]\n"))
    assert loaded.warnings == ()


def test_remote_is_still_validated_in_a_dependency(tmp_path: Path) -> None:
    text = CHAIN.replace("    subdomain: c\n", "    subdomain: c\n    remote: not-a-url\n")
    with pytest.raises(WtError, match="remote: must be an http:// or https:// URL"):
        load_catalog(write(tmp_path, text))


HYPHENATED = """\
repos:
  music: ./music
services:
  music-back:
    repo: music
    compose: docker-compose.yml
    port: 8000
    subdomain: api
  music-front:
    repo: music
    run: serve --port {{ port }}
    port: auto
    subdomain: ""
    consumes:
      music-back: VITE_API_URL
"""


def test_hyphenated_services_render_the_consumer_env_to_the_producer_url(tmp_path: Path) -> None:
    cfg = load_config(project(tmp_path, HYPHENATED, "uses: [music-front]\n")).config
    env = cfg.services["music-front"].env
    assert env == {"VITE_API_URL": "{{ services['music-back'].url }}"}
    assert cfg.services["music-front"].needs == ["music-back"]
    context = {"services": {"music-back": {"url": "https://api.music.localhost"}}}
    assert render(env["VITE_API_URL"], context, "env.VITE_API_URL") == "https://api.music.localhost"


def test_services_project_with_hyphenated_names_renders_both_forms_of_access(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worktree.yaml"
    path.write_text(
        "project: music\ndomain: music.localhost\nrepos: {music: ./music}\nservices:\n"
        "  music-back: {repo: music, run: serve, port: 8000, subdomain: api}\n"
        "  music-front:\n    repo: music\n    run: serve\n    port: 8001\n"
        "    needs: [music-back]\n"
        "    env: {API: \"{{ services['music-back'].url }}\"}\n"
    )
    cfg = load_config(path).config
    context = {"services": {"music-back": {"url": "https://api.music.localhost"}}}
    assert render(cfg.services["music-front"].env["API"], context, "env.API") == (
        "https://api.music.localhost"
    )


# --- the project catalog: project/domain in the catalog, load_project ---
PROJECT_HEADER = "project: notes\ndomain: notes.localhost\n"


def project_catalog(tmp_path: Path, text: str = GOOD, header: str = PROJECT_HEADER) -> Path:
    return write(tmp_path, header + text)


def test_catalog_project_and_domain_are_optional() -> None:
    old = Catalog.model_validate(yaml.safe_load(GOOD))
    assert old.project is None
    assert old.domain is None
    new = Catalog.model_validate({**yaml.safe_load(GOOD), "project": "notes", "domain": "n.dev"})
    assert (new.project, new.domain) == ("notes", "n.dev")


@pytest.mark.parametrize(
    ("key", "value"), [("project", "Notes"), ("project", "1x"), ("domain", "nodot"), ("domain", "")]
)
def test_catalog_project_and_domain_are_validated_when_set(key: str, value: str) -> None:
    with pytest.raises(ValueError, match=key):
        Catalog.model_validate({**yaml.safe_load(GOOD), key: value})


def test_old_catalog_without_project_still_loads_through_the_old_path(tmp_path: Path) -> None:
    assert load_catalog(write(tmp_path, GOOD)).catalog.project is None
    assert list(load_config(project(tmp_path, GOOD, "uses: [back]\n")).config.services) == ["back"]


def test_load_project_matches_a_uses_project(tmp_path: Path) -> None:
    path = project_catalog(tmp_path)
    old = load_config(project(tmp_path, PROJECT_HEADER + GOOD, "uses: [front]\n"))
    new = load_project(path, ["front"])
    assert new.config == old.config
    assert new.path == path == new.catalog
    assert new.uses == ("front",)
    assert new.service_order == ("back", "front")
    assert new.remotes == old.remotes == {}
    assert new.warnings == old.warnings == (IGNORED_BACK,)
    assert new.config.repos["notes-back"] == str(tmp_path / "back")
    assert new.config.project == "notes"
    assert new.config.domain == "notes.localhost"


def test_load_project_without_uses_is_the_anchor(tmp_path: Path) -> None:
    anchor = load_project(project_catalog(tmp_path))
    assert anchor.uses == ("back",)  # the first service in catalog order
    assert list(anchor.config.services) == ["back"]
    assert anchor.remotes == {"front": ""}


def test_the_anchor_brings_its_closure(tmp_path: Path) -> None:
    consumer_first = GOOD.replace(
        "services:\n",
        "services:\n  front0:\n    repo: notes-front\n"
        "    run: y\n    port: 1\n    consumes:\n      back: B_URL\n",
        1,
    )
    anchor = load_project(project_catalog(tmp_path, consumer_first))
    assert anchor.uses == ("front0",)
    assert list(anchor.config.services) == ["front0", "back"]


def test_load_project_explicit_selection_keeps_its_order(tmp_path: Path) -> None:
    loaded = load_project(project_catalog(tmp_path), ["front", "back"])
    assert loaded.uses == ("front", "back")
    assert list(loaded.config.services) == ["front", "back"]


def test_load_project_empty_selection_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WtError, match=r"choose at least one service \(wt add <b> --uses a,b\)"):
        load_project(project_catalog(tmp_path), [])


def test_load_project_unknown_service_lists_the_catalog_names(tmp_path: Path) -> None:
    with pytest.raises(WtError, match=r"unknown service 'nope'.*Known: back, front"):
        load_project(project_catalog(tmp_path), ["nope"])


def test_load_project_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "nope.yaml"
    expected = f"no catalog at {path}. Run wt init, or wt migrate <worktree.yaml>"
    with pytest.raises(WtError) as info:
        load_project(path)
    assert str(info.value) == expected


@pytest.mark.parametrize(
    ("header", "missing"),
    [("", "project, domain"), ("project: notes\n", "domain"), ("domain: n.dev\n", "project")],
)
def test_load_project_requires_project_and_domain(
    tmp_path: Path, header: str, missing: str
) -> None:
    path = project_catalog(tmp_path, header=header)
    with pytest.raises(WtError, match=rf"{path.name}.*missing.*{missing}"):
        load_project(path)


def test_load_project_reports_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(WtError, match="not valid YAML"):
        load_project(write(tmp_path, "a: [\n"))
    bad = project_catalog(tmp_path, GOOD.replace("compose: docker-compose.yml", "port: 1"))
    with pytest.raises(WtError, match=r"invalid catalog\.yaml"):
        load_project(bad)


def test_load_project_reports_infra_conflicts(tmp_path: Path) -> None:
    text = GOOD.replace(
        "    consumes:\n      back: API_URL\n",
        "    consumes:\n      back: API_URL\n"
        "    infra:\n      postgres: {databases: [other], base_branch: dev}\n",
    ).replace("postgres: {databases: [notes]}", "postgres: {databases: [notes], base_branch: main}")
    with pytest.raises(WtError, match="conflicts"):
        load_project(project_catalog(tmp_path, text), ["front"])


def test_load_project_rejects_more_than_100_services(tmp_path: Path) -> None:
    services = "".join(f"  s{i}:\n    repo: r\n    run: x\n" for i in range(101))
    path = project_catalog(tmp_path, f"repos: {{r: .}}\nservices:\n{services}")
    with pytest.raises(WtError, match="at most 100 services"):
        load_project(path)


# --- wt migrate: read_legacy and merge_legacy (pure) ---
SHOP = """\
project: shop
domain: shop.localhost
repos: { api: ./shop-api, web: ./shop-web }
services:
  api:
    repo: api
    compose: docker-compose.yml
    port: 8000
    subdomain: api
    health: /healthz
    env:
      DATABASE_URL: "{{ postgres.url('shop') }}"
      WEB_ORIGIN: "{{ services.web.url }}"
      KAFKA_BOOTSTRAP: "{{kafka.bootstrap}}"
  worker:
    repo: api
    compose: docker-compose.yml
    compose_service: worker
    env:
      KAFKA_GROUP: "{{ kafka.group('worker') }}"
      REDIS_URL: "{{ redis.url }}"
  web:
    repo: web
    run: python3 server.py --port {{ port }}
    port: auto
    subdomain: ""
    env:
      KEYCLOAK_URL: "{{ keycloak.url }}"
shared:
  postgres: {databases: [shop], strategy: template, verify: true}
  kafka: {groups: [worker], topic_prefix: true}
  redis: {}
  keycloak: {realm: shop, client: web}
"""


def legacy_dict(text: str = SHOP) -> dict[str, Any]:
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict)
    return raw


def test_read_legacy(tmp_path: Path) -> None:
    path = tmp_path / "worktree.yaml"
    path.write_text(SHOP)
    assert read_legacy(path) == legacy_dict()
    with pytest.raises(WtError, match="not found"):
        read_legacy(tmp_path / "nope.yaml")
    path.write_text("a: [\n")
    with pytest.raises(WtError, match="not valid YAML"):
        read_legacy(path)
    path.write_text("- a\n")
    with pytest.raises(WtError, match="must contain a YAML mapping"):
        read_legacy(path)


def test_merge_services_style_into_a_new_catalog(tmp_path: Path) -> None:
    result = merge_legacy(None, legacy_dict(), tmp_path)
    cat = result.catalog
    assert list(cat) == ["project", "domain", "repos", "services"]
    assert (cat["project"], cat["domain"]) == ("shop", "shop.localhost")
    assert cat["repos"] == {"api": str(tmp_path / "shop-api"), "web": str(tmp_path / "shop-web")}
    assert result.added == ("api", "worker", "web")
    assert result.skipped == ()
    assert result.uses == ("api", "worker", "web")
    assert "shared" not in cat
    Catalog.model_validate(cat)


def test_merge_gives_each_service_the_shared_sections_it_mentions(tmp_path: Path) -> None:
    services = merge_legacy(None, legacy_dict(), tmp_path).catalog["services"]
    assert list(services["api"]["infra"]) == ["postgres", "kafka"]  # "{{kafka." without spaces too
    assert list(services["worker"]["infra"]) == ["kafka", "redis"]
    assert list(services["web"]["infra"]) == ["keycloak"]
    assert services["api"]["infra"]["postgres"] == {
        "databases": ["shop"],
        "strategy": "template",
        "verify": True,
    }
    assert services["worker"]["infra"]["redis"] == {}


def test_merge_gives_an_unmentioned_shared_section_to_the_first_service(tmp_path: Path) -> None:
    legacy = legacy_dict()
    legacy["shared"]["redis"] = {"prefix": False}
    legacy["services"]["worker"]["env"].pop("REDIS_URL")
    services = merge_legacy(None, legacy, tmp_path).catalog["services"]
    assert services["api"]["infra"]["redis"] == {"prefix": False}
    assert "redis" not in services["worker"]["infra"]


def test_merge_looks_in_run_and_health_text_too(tmp_path: Path) -> None:
    legacy = {
        "project": "p",
        "domain": "p.dev",
        "repos": {"r": "."},
        "services": {
            "one": {"repo": "r", "run": "x"},
            "two": {"repo": "r", "run": "serve {{ redis.url }}"},
            "three": {"repo": "r", "run": "y", "port": 1, "health": "/{{postgres.name}}"},
        },
        "shared": {"redis": {}, "postgres": {"databases": ["d"]}},
    }
    services = merge_legacy(None, legacy, tmp_path).catalog["services"]
    assert "infra" not in services["one"]
    assert list(services["two"]["infra"]) == ["redis"]
    assert list(services["three"]["infra"]) == ["postgres"]


def test_merge_does_not_change_its_inputs(tmp_path: Path) -> None:
    legacy = legacy_dict()
    existing = {"project": "shop", "domain": "shop.localhost", "repos": {}, "services": {}}
    merge_legacy(existing, legacy, tmp_path)
    assert legacy == legacy_dict()
    assert existing["services"] == {}


def test_merge_keeps_absolute_and_home_repos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/h")
    legacy = legacy_dict()
    legacy["repos"] = {"api": "/srv/api", "web": "~/web"}
    assert merge_legacy(None, legacy, tmp_path).catalog["repos"] == {
        "api": "/srv/api",
        "web": "/h/web",
    }


def test_merge_is_idempotent(tmp_path: Path) -> None:
    first = merge_legacy(None, legacy_dict(), tmp_path)
    second = merge_legacy(first.catalog, legacy_dict(), tmp_path)
    assert second.catalog == first.catalog
    assert second.added == ()
    assert second.skipped == ("api", "worker", "web")
    assert second.uses == first.uses


def test_merge_appends_new_services_and_keeps_the_existing_ones_first(tmp_path: Path) -> None:
    existing = merge_legacy(None, legacy_dict(), tmp_path).catalog
    other = {
        "project": "shop",
        "domain": "shop.localhost",
        "repos": {"cron": "/srv/cron"},
        "services": {"cron": {"repo": "cron", "run": "cron"}},
    }
    result = merge_legacy(existing, other, tmp_path)
    assert result.added == ("cron",)
    assert list(result.catalog["services"]) == ["api", "worker", "web", "cron"]
    assert list(result.catalog["repos"]) == ["api", "web", "cron"]
    assert result.uses == ("cron",)


@pytest.mark.parametrize("key", ["project", "domain"])
def test_merge_into_another_project_or_domain_is_an_error(tmp_path: Path, key: str) -> None:
    existing = merge_legacy(None, legacy_dict(), tmp_path).catalog
    other = {**legacy_dict(), key: "other" if key == "project" else "other.dev"}
    new = other[key]
    with pytest.raises(WtError) as info:
        merge_legacy(existing, other, tmp_path)
    assert str(info.value) == f"catalog is {key} {existing[key]}, worktree.yaml is {key} {new}"


def test_merge_into_a_catalog_without_project_sets_it(tmp_path: Path) -> None:
    old = yaml.safe_load(GOOD)
    old["repos"] = {"notes-back": str(tmp_path / "back"), "notes-front": "~/code/front"}
    legacy = {
        "project": "notes",
        "domain": "notes.localhost",
        "uses": ["back"],
        "catalog": "c.yaml",
    }
    (tmp_path / "c.yaml").write_text(GOOD)
    result = merge_legacy(old, legacy, tmp_path)
    assert result.catalog["project"] == "notes"
    assert result.added == ()
    assert result.skipped == ("back",)


@pytest.mark.parametrize("key", ["project", "domain"])
def test_merge_requires_project_and_domain_in_the_legacy_file(tmp_path: Path, key: str) -> None:
    legacy = legacy_dict()
    del legacy[key]
    with pytest.raises(WtError, match=f"worktree.yaml.*{key}"):
        merge_legacy(None, legacy, tmp_path)


def test_merge_conflict_lists_every_differing_service_in_one_error(tmp_path: Path) -> None:
    existing = merge_legacy(None, legacy_dict(), tmp_path).catalog
    existing["services"]["api"]["port"] = 9000
    existing["services"]["web"]["port"] = 9001
    with pytest.raises(WtError) as info:
        merge_legacy(existing, legacy_dict(), tmp_path)
    message = str(info.value)
    assert "service 'api' differs from the catalog's" in message
    assert "service 'web' differs from the catalog's" in message
    assert "worker" not in message


def test_merge_repo_name_clash_with_another_path_is_an_error(tmp_path: Path) -> None:
    existing = merge_legacy(None, legacy_dict(), tmp_path).catalog
    existing["repos"]["web"] = "/elsewhere/web"
    with pytest.raises(WtError, match=r"repo 'web'.*/elsewhere/web.*shop-web"):
        merge_legacy(existing, legacy_dict(), tmp_path)


def test_merge_validates_the_result(tmp_path: Path) -> None:
    legacy = legacy_dict()
    legacy["services"]["api"]["repo"] = "ghost"
    with pytest.raises(WtError, match="unknown repo 'ghost'"):
        merge_legacy(None, legacy, tmp_path)
    legacy = legacy_dict()
    legacy["project"] = "Bad Name"
    with pytest.raises(WtError, match="project"):
        merge_legacy(None, legacy, tmp_path)


def test_merge_needs_exactly_one_of_services_or_uses(tmp_path: Path) -> None:
    legacy = legacy_dict()
    legacy["uses"] = ["x"]
    with pytest.raises(WtError, match="exactly one of 'services' or 'uses'"):
        merge_legacy(None, legacy, tmp_path)
    del legacy["services"], legacy["uses"]
    with pytest.raises(WtError, match="exactly one of 'services' or 'uses'"):
        merge_legacy(None, legacy, tmp_path)


USES_LEGACY = {"project": "notes", "domain": "notes.localhost", "uses": ["front"]}


def test_merge_uses_style_copies_the_closure_with_its_repos(tmp_path: Path) -> None:
    (tmp_path / "catalog.yaml").write_text(GOOD)
    legacy = {**USES_LEGACY, "catalog": "./catalog.yaml"}
    result = merge_legacy(None, legacy, tmp_path)
    assert result.added == ("back", "front")  # catalog order
    assert result.uses == ("front", "back")  # the closure, selection first
    cat = result.catalog
    assert list(cat) == ["project", "domain", "repos", "services"]
    assert cat["repos"]["notes-back"] == str(tmp_path / "back")
    assert cat["services"]["front"]["consumes"] == {"back": "API_URL"}
    assert cat["services"]["back"]["remote"] == "https://api.staging.example"
    assert cat["services"]["back"]["infra"] == {"postgres": {"databases": ["notes"]}}
    loaded = load_project(_dump(tmp_path, cat), ["front"])
    assert set(loaded.config.services) == {"front", "back"}


def _dump(tmp_path: Path, catalog: dict[str, Any]) -> Path:
    path = tmp_path / "merged.yaml"
    path.write_text(yaml.safe_dump(catalog, sort_keys=False))
    return path


def test_merge_uses_style_only_copies_what_the_stack_reaches(tmp_path: Path) -> None:
    (tmp_path / "catalog.yaml").write_text(GOOD)
    result = merge_legacy(
        None, {**USES_LEGACY, "uses": ["back"], "catalog": "catalog.yaml"}, tmp_path
    )
    assert result.added == ("back",)
    assert list(result.catalog["repos"]) == ["notes-back"]
    assert result.uses == ("back",)


def test_merge_uses_style_finds_the_catalog_through_wt_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "env.yaml").write_text(GOOD)
    monkeypatch.setenv("WT_CATALOG", str(tmp_path / "env.yaml"))
    assert merge_legacy(None, USES_LEGACY, tmp_path).added == ("back", "front")


def test_merge_uses_style_finds_the_default_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WT_CATALOG", raising=False)
    monkeypatch.setenv("WT_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "catalog.yaml").write_text(GOOD)
    assert merge_legacy(None, USES_LEGACY, tmp_path).added == ("back", "front")


def test_merge_uses_style_is_idempotent_and_skips_identical_services(tmp_path: Path) -> None:
    (tmp_path / "catalog.yaml").write_text(GOOD)
    legacy = {**USES_LEGACY, "catalog": "catalog.yaml"}
    first = merge_legacy(None, legacy, tmp_path)
    second = merge_legacy(first.catalog, legacy, tmp_path)
    assert second.catalog == first.catalog
    assert (second.added, second.skipped) == ((), ("back", "front"))


def test_merge_uses_style_conflict_names_the_service(tmp_path: Path) -> None:
    (tmp_path / "catalog.yaml").write_text(GOOD)
    legacy = {**USES_LEGACY, "catalog": "catalog.yaml"}
    existing = merge_legacy(None, legacy, tmp_path).catalog
    existing["services"]["back"]["port"] = 1234
    with pytest.raises(WtError, match="service 'back' differs from the catalog's"):
        merge_legacy(existing, legacy, tmp_path)


def test_merge_uses_style_missing_catalog_and_unknown_service(tmp_path: Path) -> None:
    with pytest.raises(WtError, match="catalog not found"):
        merge_legacy(None, {**USES_LEGACY, "catalog": "nope.yaml"}, tmp_path)
    (tmp_path / "catalog.yaml").write_text(GOOD)
    with pytest.raises(WtError, match="unknown service 'ghost'"):
        merge_legacy(None, {**USES_LEGACY, "uses": ["ghost"], "catalog": "catalog.yaml"}, tmp_path)
    with pytest.raises(WtError, match="non-empty list"):
        merge_legacy(None, {**USES_LEGACY, "uses": [], "catalog": "catalog.yaml"}, tmp_path)


def test_a_merged_catalog_loads_with_load_project(tmp_path: Path) -> None:
    result = merge_legacy(None, legacy_dict(), tmp_path)
    loaded = load_project(_dump(tmp_path, result.catalog), list(result.uses))
    assert list(loaded.config.services) == ["api", "worker", "web"]
    assert loaded.config.shared.kafka is not None
    assert loaded.config.shared.kafka.groups == ["worker"]
    assert isinstance(result, MergeResult)


def test_merge_reads_relative_catalog_repos_against_catalog_dir(tmp_path: Path) -> None:
    catalog_dir = tmp_path / "home"
    legacy_dir = tmp_path / "proj"
    existing = merge_legacy(None, legacy_dict(), legacy_dir).catalog
    existing["repos"] = {"api": "../proj/shop-api", "web": "../proj/./shop-web"}
    result = merge_legacy(existing, legacy_dict(), legacy_dir, catalog_dir=catalog_dir)
    assert result.catalog["repos"] == existing["repos"]  # kept as written
    assert result.added == ()


def test_merge_without_catalog_dir_compares_relative_repos_as_given(tmp_path: Path) -> None:
    existing = merge_legacy(None, legacy_dict(), tmp_path).catalog
    existing["repos"]["web"] = "./shop-web"
    with pytest.raises(WtError, match=r"repo 'web' is \./shop-web in the catalog"):
        merge_legacy(existing, legacy_dict(), tmp_path)


def test_merge_relative_catalog_repo_pointing_elsewhere_is_still_a_clash(tmp_path: Path) -> None:
    existing = merge_legacy(None, legacy_dict(), tmp_path).catalog
    existing["repos"]["web"] = "./other"
    with pytest.raises(WtError, match="repo 'web'"):
        merge_legacy(existing, legacy_dict(), tmp_path, catalog_dir=tmp_path)


def test_merge_home_relative_catalog_repo_with_the_same_path_is_no_clash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    existing = merge_legacy(None, legacy_dict(), tmp_path).catalog
    existing["repos"]["api"] = "~/shop-api"
    result = merge_legacy(existing, legacy_dict(), tmp_path)
    assert result.catalog["repos"]["api"] == "~/shop-api"
    assert result.added == ()
