from pathlib import Path

import pytest

from wt.catalog import Catalog, catalog_path, expand_uses, load_catalog
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
    assert cfg.services["front"].env == {"API_URL": "{{ services.back.url }}"}
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
    assert cfg.services["front"].env == {"API_URL": "{{ services.back.url }}"}
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
    assert loaded.config.services["front"].env == {"API_URL": "{{ services.back.url }}"}
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
    assert loaded.config.services["b"].env == {"C_URL": "{{ services.c.url }}"}


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
        assert cfg.services["b"].env == {"C_URL": "{{ services.c.url }}"}
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
