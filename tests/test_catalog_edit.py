"""catalog_edit: the in-place edits behind `wt catalog add-*` / `remove-*` (SPEC section 23)."""

import difflib
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.conftest import FakePluginFactory, FakeProvider, InstallPlugins
from wt import catalog_edit
from wt.catalog import load_catalog, load_project
from wt.catalog_edit import ServiceSpec
from wt.config import LoadedConfig
from wt.errors import WtError

EDIT_CATALOG = """\
# notes - the stack of the notes app
project: notes          # the project
domain: notes.localhost

repos:
  notes-back: /srv/notes-back     # the API
  notes-front: /srv/notes-front
  # spare: nobody uses it yet
  spare: /srv/spare

services:
  # the API
  back:
    repo: notes-back
    compose: docker-compose.yml
    port: 8000
    subdomain: api       # https://api.<branch>.notes.localhost
    health: /health
    infra:
      postgres:
        databases:
          - notes
  front:                 # the web app
    repo: notes-front
    run: npm run dev -- --port {{ port }}
    port: auto
    subdomain: ""
    consumes:
      back: API_URL
  worker:
    repo: notes-back
    run: ./worker
"""


@pytest.fixture
def catalog_file(tmp_path: Path) -> Path:
    path = tmp_path / "c.yaml"
    path.write_text(EDIT_CATALOG)
    return path


def changes(before: str, after: str) -> tuple[list[str], list[str]]:
    """(lines removed, lines added): everything not listed is byte-for-byte as it was."""
    diff = list(difflib.ndiff(before.splitlines(), after.splitlines()))
    removed = [line[2:] for line in diff if line.startswith("- ")]
    added = [line[2:] for line in diff if line.startswith("+ ")]
    return removed, added


def refused(path: Path, call: Callable[[], object]) -> str:
    """The message of the WtError `call` raises; the file must be exactly as it was."""
    before = path.read_text()
    with pytest.raises(WtError) as raised:
        call()
    assert path.read_text() == before
    assert not list(path.parent.glob(".c.yaml.*"))  # no temp file left behind
    return str(raised.value)


# --- add_repo ---------------------------------------------------------------------------------


def test_add_repo_adds_a_line_and_nothing_else_changes(catalog_file: Path) -> None:
    result = catalog_edit.add_repo(catalog_file, "docs", Path("/srv/docs"))
    assert (result.action, result.name) == ("add-repo", "docs")
    removed, added = changes(EDIT_CATALOG, catalog_file.read_text())
    assert removed == [] and added == ["  docs: /srv/docs"]
    assert load_project(catalog_file).config.project == "notes"


def test_add_repo_stores_a_relative_path_absolute_from_the_cwd(
    catalog_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    catalog_edit.add_repo(catalog_file, "docs", Path("sub/docs"))
    assert f"  docs: {tmp_path / 'sub' / 'docs'}" in catalog_file.read_text()


def test_add_repo_refuses_an_existing_name(catalog_file: Path) -> None:
    message = refused(
        catalog_file, lambda: catalog_edit.add_repo(catalog_file, "spare", Path("/x"))
    )
    assert message == "repo 'spare' already exists"


def test_a_block_sequence_keeps_its_style_when_another_node_changes(catalog_file: Path) -> None:
    catalog_edit.add_repo(catalog_file, "docs", Path("/srv/docs"))
    assert "        databases:\n          - notes\n" in catalog_file.read_text()


# --- add_service ------------------------------------------------------------------------------


def test_add_service_run_with_every_option(catalog_file: Path) -> None:
    spec = ServiceSpec(
        name="admin",
        repo="notes-front",
        run="npm start -- {{ port }}",
        setup="npm ci",
        port="auto",
        subdomain="admin",
        health="/ping",
        env={"MODE": "dev", "NAME": "a b"},
        consumes={"back": "API_URL"},
        infra={"postgres": ["notes", "audit"], "kafka": ["g1"], "redis": []},
    )
    assert catalog_edit.add_service(catalog_file, spec).action == "add-service"
    text = catalog_file.read_text()
    removed, added = changes(EDIT_CATALOG, text)
    assert removed == [] and added[0] == "  admin:"  # one new node, at the end of services
    assert text.startswith(EDIT_CATALOG)
    loaded = load_project(catalog_file, uses=["admin"]).config
    svc = loaded.services["admin"]
    assert (svc.repo, svc.run, svc.setup) == ("notes-front", "npm start -- {{ port }}", "npm ci")
    assert (svc.port, svc.subdomain, svc.health) == ("auto", "admin", "/ping")
    assert {k: v for k, v in svc.env.items() if k != "API_URL"} == {"MODE": "dev", "NAME": "a b"}
    assert loaded.shared.postgres is not None
    assert loaded.shared.postgres.databases == ["notes", "audit"]
    assert loaded.shared.kafka is not None and loaded.shared.kafka.groups == ["g1"]
    assert loaded.shared.redis is not None
    assert load_catalog(catalog_file).catalog.services["admin"].consumes == {"back": "API_URL"}
    assert "databases: [notes, audit]" in text  # lists are written like the starter catalog's


def test_add_service_compose_with_a_numeric_port(catalog_file: Path) -> None:
    spec = ServiceSpec(name="ui", repo="notes-back", compose="ui.yml", port=9000)
    catalog_edit.add_service(catalog_file, spec)
    svc = load_project(catalog_file, uses=["ui"]).config.services["ui"]
    assert (svc.compose, svc.port) == ("ui.yml", 9000)


def test_add_service_writes_the_branch_root_subdomain_as_quoted_empty(catalog_file: Path) -> None:
    spec = ServiceSpec(name="root", repo="notes-front", run="serve", port=8123, subdomain="")
    catalog_edit.add_service(catalog_file, spec)
    assert '    subdomain: ""' in catalog_file.read_text().splitlines()
    assert load_project(catalog_file, uses=["root"]).config.services["root"].subdomain == ""


ADD_SERVICE_REFUSALS = [
    (ServiceSpec("x", "nope", run="r"), "unknown repo 'nope' (repos: "),
    (ServiceSpec("back", "notes-back", run="r"), "service 'back' already exists"),
    (ServiceSpec("x", "notes-back"), "choose exactly one of --run or --compose"),
    (
        ServiceSpec("x", "notes-back", run="r", compose="c.yml"),
        "choose exactly one of --run or --compose",
    ),
    (ServiceSpec("x", "notes-back", compose="c", setup="s"), "--setup needs --run"),
    (
        ServiceSpec("x", "notes-back", run="r", consumes={"ghost": "G"}),
        "unknown service 'ghost' (services: ",
    ),
    (ServiceSpec("x", "notes-back", run="r", subdomain="x"), "needs a 'port'"),
    (ServiceSpec("Bad Name", "notes-back", run="r"), "Bad Name"),
]


@pytest.mark.parametrize(("spec", "message"), ADD_SERVICE_REFUSALS)
def test_add_service_refusals_leave_the_file_untouched(
    catalog_file: Path, spec: ServiceSpec, message: str
) -> None:
    assert message in refused(catalog_file, lambda: catalog_edit.add_service(catalog_file, spec))


# --- remove_service ---------------------------------------------------------------------------


def test_remove_service_deletes_only_its_node(catalog_file: Path) -> None:
    result = catalog_edit.remove_service(catalog_file, "worker")
    assert (result.action, result.name) == ("remove-service", "worker")
    removed, added = changes(EDIT_CATALOG, catalog_file.read_text())
    assert added == [] and removed == ["  worker:", "    repo: notes-back", "    run: ./worker"]


def test_remove_service_in_the_middle_keeps_the_nodes_around_it(catalog_file: Path) -> None:
    catalog_edit.remove_service(catalog_file, "front")
    after = catalog_file.read_text()
    removed, added = changes(EDIT_CATALOG, after)
    assert added == [] and "  worker:" not in removed and "  back:" not in removed
    assert "  # the API\n  back:" in after and "  worker:\n    repo: notes-back" in after
    assert "\n  front:" not in after


def test_remove_service_is_refused_while_another_one_consumes_it(catalog_file: Path) -> None:
    message = refused(catalog_file, lambda: catalog_edit.remove_service(catalog_file, "back"))
    assert message == "service 'back' is consumed by 'front'"


def test_remove_unknown_service_lists_the_known_ones(catalog_file: Path) -> None:
    message = refused(catalog_file, lambda: catalog_edit.remove_service(catalog_file, "ghost"))
    assert message == "unknown service 'ghost' (services: back, front, worker)"


def test_removing_the_last_service_is_refused_by_validation(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "project: p\ndomain: p.localhost\nrepos:\n  r: /r\nservices:\n  only:\n    repo: r\n"
        "    run: x\n"
    )
    refused(path, lambda: catalog_edit.remove_service(path, "only"))


# --- remove_repo ------------------------------------------------------------------------------


def test_remove_repo_deletes_only_its_line(catalog_file: Path) -> None:
    result = catalog_edit.remove_repo(catalog_file, "spare")
    assert (result.action, result.name) == ("remove-repo", "spare")
    text = catalog_file.read_text()
    removed, added = changes(EDIT_CATALOG, text)
    assert added == [] and removed == ["  spare: /srv/spare"]
    assert "  # spare: nobody uses it yet" in text  # the comment above, and the blank line below
    assert "\n\nservices:" in text


def test_remove_repo_is_refused_while_a_service_uses_it(catalog_file: Path) -> None:
    message = refused(catalog_file, lambda: catalog_edit.remove_repo(catalog_file, "notes-front"))
    assert message == "repo 'notes-front' is used by service 'front'"


def test_remove_repo_names_every_service_that_uses_it(catalog_file: Path) -> None:
    message = refused(catalog_file, lambda: catalog_edit.remove_repo(catalog_file, "notes-back"))
    assert message == "repo 'notes-back' is used by services 'back', 'worker'"


def test_remove_unknown_repo_lists_the_known_ones(catalog_file: Path) -> None:
    message = refused(catalog_file, lambda: catalog_edit.remove_repo(catalog_file, "ghost"))
    assert message == "unknown repo 'ghost' (repos: notes-back, notes-front, spare)"


# --- the file -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda p: catalog_edit.add_repo(p, "a", Path("/a")),
        lambda p: catalog_edit.add_service(p, ServiceSpec(name="a", repo="r", run="x")),
        lambda p: catalog_edit.remove_service(p, "a"),
        lambda p: catalog_edit.remove_repo(p, "a"),
    ],
)
def test_every_edit_needs_a_catalog(tmp_path: Path, call) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(WtError) as raised:
        call(missing)
    assert str(raised.value) == f"no catalog at {missing}. Run wt init"
    assert not missing.exists()


def test_a_file_that_is_not_yaml_is_reported_and_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("services: [unclosed\n")
    assert "is not valid YAML" in refused(path, lambda: catalog_edit.remove_repo(path, "a"))


def test_a_catalog_that_is_not_a_mapping_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("- a\n- b\n")
    assert "must contain a YAML mapping" in refused(
        path, lambda: catalog_edit.remove_repo(path, "a")
    )


def test_every_service_is_checked_on_its_own_before_writing(
    catalog_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[list[str] | None] = []
    real = load_project

    def spy(path: Path, uses: list[str] | None = None) -> LoadedConfig:
        asked.append(uses)
        return real(path, uses=uses)

    monkeypatch.setattr(catalog_edit, "load_project", spy)
    catalog_edit.add_repo(catalog_file, "docs", Path("/srv/docs"))
    assert asked == [None, ["back"], ["front"], ["worker"]]  # the anchor, then each one alone


# --- infra keys are checked like `up` checks them -----------------------------------------


def one_service_catalog(path: Path, infra: str) -> Path:
    path.write_text(
        "project: p\ndomain: p.localhost\nrepos:\n  r: /r\nservices:\n  s:\n    repo: r\n"
        f"    run: x\n    infra:\n      {infra}\n"
    )
    return path


def test_service_failures_reports_an_unknown_infra_key(tmp_path: Path) -> None:
    path = one_service_catalog(tmp_path / "c.yaml", "nope: {}")
    failures = catalog_edit.service_failures(path, ["s"])
    assert failures == {
        "s": "unknown infra 'nope' (installed providers: kafka, keycloak, postgres, redis)"
    }


def test_service_failures_accepts_an_installed_plugin_and_checks_its_settings(
    tmp_path: Path, install_plugins: InstallPlugins
) -> None:
    install_plugins({"echo": FakePluginFactory(FakeProvider([], name="echo"))})
    good = one_service_catalog(tmp_path / "good.yaml", "echo: {prefix: z}")
    bad = one_service_catalog(tmp_path / "bad.yaml", "echo: {nope: 1}")
    assert catalog_edit.service_failures(good, ["s"]) == {}
    assert "infra echo:" in catalog_edit.service_failures(bad, ["s"])["s"]


def test_add_service_with_an_unknown_infra_key_is_refused(catalog_file: Path) -> None:
    spec = ServiceSpec("x", "notes-back", run="r", infra={"nope": []})
    message = refused(catalog_file, lambda: catalog_edit.add_service(catalog_file, spec))
    assert message.startswith("service 'x': unknown infra 'nope'")


def test_add_service_with_an_installed_plugin_is_written(
    catalog_file: Path, install_plugins: InstallPlugins
) -> None:
    install_plugins({"echo": FakePluginFactory(FakeProvider([], name="echo"))})
    spec = ServiceSpec("x", "notes-back", run="r", infra=catalog_edit.parse_infra(["echo"]))
    catalog_edit.add_service(catalog_file, spec)
    assert "      echo: {}" in catalog_file.read_text()


# --- option parsing ---------------------------------------------------------------------------


def test_parse_pairs() -> None:
    assert catalog_edit.parse_pairs(["A=1", "B=x=y"], "--env", "=") == {"A": "1", "B": "x=y"}
    assert catalog_edit.parse_pairs(["back:API_URL"], "--consumes", ":") == {"back": "API_URL"}


@pytest.mark.parametrize("item", ["NOEQUALS", "=v", "K="])
def test_parse_pairs_refuses_a_malformed_item(item: str) -> None:
    with pytest.raises(WtError, match=r"--env .*: expected KEY=VALUE"):
        catalog_edit.parse_pairs([item], "--env", "=")


def test_parse_port() -> None:
    assert [catalog_edit.parse_port(v) for v in (None, "auto", "8080")] == [None, "auto", 8080]
    with pytest.raises(WtError, match="--port 'http': expected a number or 'auto'"):
        catalog_edit.parse_port("http")


def test_parse_infra() -> None:
    assert catalog_edit.parse_infra(["postgres:a, b", "kafka:g", "redis"]) == {
        "postgres": ["a", "b"],
        "kafka": ["g"],
        "redis": [],
    }


def test_parse_infra_refuses_a_list_for_a_provider_that_takes_none() -> None:
    with pytest.raises(WtError, match="--infra 'redis:a': only postgres and kafka take a list"):
        catalog_edit.parse_infra(["redis:a"])


def test_check_providers_lists_the_known_ones() -> None:
    catalog_edit.check_providers(["redis"])
    with pytest.raises(WtError) as raised:
        catalog_edit.check_providers(["redis", "mongo"])
    assert str(raised.value) == (
        "unknown provider 'mongo' (providers: kafka, keycloak, postgres, redis)"
    )


def test_split_names() -> None:
    assert catalog_edit.split_names(" a, b,,a ") == ["a", "b"]
    assert catalog_edit.split_names(None) == []
