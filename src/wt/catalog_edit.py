"""Edit the catalog file in place for `wt catalog add-*` / `remove-*` (SPEC section 23).

The file is read as a ruamel round-trip document (comments, key order and quoting stay), the
change is made, and the result is checked like `wt catalog --check` checks it before anything is
written. Every function raises `WtError`; a failure leaves the file untouched.
"""

import io
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from wt.catalog import load_catalog, load_project
from wt.errors import WtError
from wt.providers import provider_names, validate_infra
from wt.state import atomic_write

INFRA_LISTS = {"postgres": "databases", "kafka": "groups"}  # providers whose section needs a list
Edit = Callable[[CommentedMap, str], str]  # (parsed catalog, its text) -> the new text


@dataclass(frozen=True)
class EditResult:
    """What changed: the action (`add-service`, ...) and the name it applied to."""

    action: str
    name: str


@dataclass(frozen=True)
class ServiceSpec:
    """What `wt catalog add-service` was asked for, already parsed."""

    name: str
    repo: str
    run: str | None = None
    compose: str | None = None
    setup: str | None = None
    port: int | str | None = None
    subdomain: str | None = None
    health: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    consumes: dict[str, str] = field(default_factory=dict)
    infra: dict[str, list[str]] = field(default_factory=dict)


# ---- the four edits -----------------------------------------------------------------------


def add_repo(target: Path, name: str, folder: Path) -> EditResult:
    # stored absolute: the catalog resolves a relative path from its own folder, not the cwd
    stored = str(folder.expanduser().absolute())

    def edit(document: CommentedMap, text: str) -> str:
        repos = _section(document, "repos")
        if name in repos:
            raise WtError(f"repo {name!r} already exists")
        repos[name] = stored
        return _dump(document)

    return _apply(target, edit, EditResult("add-repo", name))


def add_service(target: Path, spec: ServiceSpec) -> EditResult:
    def edit(document: CommentedMap, text: str) -> str:
        services = _section(document, "services")
        _check_spec(spec, services, _existing(document, "repos"))
        services[spec.name] = _service_node(spec)
        return _dump(document)

    return _apply(target, edit, EditResult("add-service", spec.name))


def remove_service(target: Path, name: str) -> EditResult:
    def edit(document: CommentedMap, text: str) -> str:
        services = _existing(document, "services")
        if name not in services:
            raise WtError(f"unknown service {name!r} (services: {', '.join(services)})")
        consumers = [
            other
            for other, node in services.items()
            if isinstance(node, CommentedMap) and name in (node.get("consumes") or {})
        ]
        if consumers:
            raise WtError(f"service {name!r} is consumed by {_quoted(consumers)}")
        return _drop_node(text, *services.lc.key(name))

    return _apply(target, edit, EditResult("remove-service", name))


def remove_repo(target: Path, name: str) -> EditResult:
    def edit(document: CommentedMap, text: str) -> str:
        repos = _existing(document, "repos")
        if name not in repos:
            raise WtError(f"unknown repo {name!r} (repos: {', '.join(repos)})")
        users = [
            service
            for service, node in _existing(document, "services").items()
            if isinstance(node, CommentedMap) and node.get("repo") == name
        ]
        if users:
            noun = "service" if len(users) == 1 else "services"
            raise WtError(f"repo {name!r} is used by {noun} {_quoted(users)}")
        return _drop_node(text, *repos.lc.key(name))

    return _apply(target, edit, EditResult("remove-repo", name))


# ---- read, check, write -------------------------------------------------------------------


def service_failures(catalog_file: Path, names: list[str]) -> dict[str, str]:
    """Why each service fails to resolve on its own (services that resolve are left out).

    A service also fails when its `infra` keys do not pass the check `up` runs (SPEC section 23).
    """
    failures: dict[str, str] = {}
    for name in names:
        try:
            validate_infra(load_project(catalog_file, uses=[name]).config)
        except WtError as exc:
            failures[name] = str(exc)
    return failures


def _apply(target: Path, edit: Edit, result: EditResult) -> EditResult:
    """Apply `edit`; write the result at `target` only if the catalog and each service load."""
    text = _render(target, edit)
    with tempfile.TemporaryDirectory() as folder:
        candidate = Path(folder) / target.name  # same name: errors say `invalid catalog.yaml`
        candidate.write_text(text)
        load_project(candidate)
        failures = service_failures(candidate, list(load_catalog(candidate).catalog.services))
    for name, message in failures.items():
        raise WtError(f"service {name!r}: {message}")
    atomic_write(target, text)
    return result


def _render(path: Path, edit: Edit) -> str:
    """The catalog at `path` with `edit` applied, as text. Nothing is written."""
    if not path.is_file():
        raise WtError(f"no catalog at {path}. Run wt init")
    text = path.read_text()
    try:
        document = _round_trip_yaml().load(text)
    except YAMLError as exc:
        raise WtError(f"{path} is not valid YAML:\n{exc}") from exc
    if not isinstance(document, CommentedMap):
        raise WtError(f"{path} must contain a YAML mapping")
    return edit(document, text)


def _round_trip_yaml() -> YAML:
    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    yaml_rt.width = 4096  # never re-wrap a long command
    return yaml_rt


def _dump(document: CommentedMap) -> str:
    out = io.StringIO()
    _round_trip_yaml().dump(document, out)
    return out.getvalue()


def _drop_node(text: str, key_line: int, key_column: int) -> str:
    """`text` without the node whose key is at `key_line`/`key_column` (0-based).

    Deleting lines keeps every other byte as written, including the blank lines and comments
    around the node (the round-trip dump would drop the ones attached to it).
    """
    lines = text.splitlines(keepends=True)
    last = key_line
    for index in range(key_line + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue  # blank and comment lines belong to the node only if its content goes on
        if len(line) - len(line.lstrip()) <= key_column:
            break
        last = index
    return "".join(lines[:key_line] + lines[last + 1 :])


def _quoted(names: Iterable[str]) -> str:
    return ", ".join(repr(name) for name in names)


def _section(document: CommentedMap, key: str) -> CommentedMap:
    """The mapping under `key`; created (empty) when the file has none."""
    section = document.get(key)
    if not isinstance(section, CommentedMap):
        section = document[key] = CommentedMap()
    return section


def _existing(document: CommentedMap, key: str) -> CommentedMap:
    section = document.get(key)
    return section if isinstance(section, CommentedMap) else CommentedMap()


# ---- the new service as a document node ---------------------------------------------------


def _flow_list(items: list[str]) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()  # `databases: [a, b]`, as in the starter catalog
    return seq


def _infra_node(infra: dict[str, list[str]]) -> CommentedMap:
    node = CommentedMap()
    for provider, items in infra.items():
        section = CommentedMap()
        if items:
            section[INFRA_LISTS[provider]] = _flow_list(items)
        node[provider] = section
    return node


def _service_node(spec: ServiceSpec) -> CommentedMap:
    """The service as a mapping, keys in the order the starter catalog writes them."""
    node = CommentedMap(repo=spec.repo)
    for key in ("compose", "run", "setup", "port"):
        if (value := getattr(spec, key)) is not None:
            node[key] = value
    if spec.subdomain is not None:
        # `""` (the branch root) must stay visible: an empty plain value would read as null
        node["subdomain"] = spec.subdomain or DoubleQuotedScalarString("")
    if spec.health is not None:
        node["health"] = spec.health
    for key in ("env", "consumes"):
        if value := getattr(spec, key):
            node[key] = CommentedMap(value)
    if spec.infra:
        node["infra"] = _infra_node(spec.infra)
    return node


def _check_spec(spec: ServiceSpec, services: CommentedMap, repos: CommentedMap) -> None:
    if (spec.run is None) == (spec.compose is None):
        raise WtError("choose exactly one of --run or --compose")
    if spec.setup is not None and spec.run is None:
        raise WtError("--setup needs --run")
    if spec.name in services:
        raise WtError(f"service {spec.name!r} already exists")
    if spec.repo not in repos:
        raise WtError(f"unknown repo {spec.repo!r} (repos: {', '.join(repos)})")
    for dep in spec.consumes:
        if dep not in services:
            raise WtError(f"unknown service {dep!r} (services: {', '.join(services)})")


# ---- option parsing (the CLI strings become the values of a ServiceSpec) ------------------


def split_names(value: str | None) -> list[str]:
    """`a, b,a` -> ['a', 'b']: trimmed, without blanks or repeats, in the order given."""
    return list(dict.fromkeys(name.strip() for name in (value or "").split(",") if name.strip()))


def check_providers(names: list[str]) -> None:
    """Fail on the first name that is not a provider wt can use (built in or installed)."""
    known = provider_names()
    for name in names:
        if name not in known:
            raise WtError(f"unknown provider {name!r} (providers: {', '.join(known)})")


def parse_pairs(items: list[str], option: str, separator: str) -> dict[str, str]:
    """`K=V` (or `DEP:VAR`) items as a dict; the first separator splits."""
    pairs: dict[str, str] = {}
    for item in items:
        key, found, value = item.partition(separator)
        if not found or not key or not value:
            raise WtError(f"{option} {item!r}: expected KEY{separator}VALUE")
        pairs[key] = value
    return pairs


def parse_port(value: str | None) -> int | str | None:
    if value is None or value == "auto":
        return value
    try:
        return int(value)
    except ValueError:
        raise WtError(f"--port {value!r}: expected a number or 'auto'") from None


def parse_infra(items: list[str]) -> dict[str, list[str]]:
    """`postgres:db1,db2` -> {"postgres": ["db1", "db2"]}; `redis` -> {"redis": []}."""
    infra: dict[str, list[str]] = {}
    for item in items:
        provider, _, listed = item.partition(":")
        check_providers([provider])
        names = split_names(listed)
        if names and provider not in INFRA_LISTS:
            raise WtError(
                f"--infra {item!r}: only {' and '.join(INFRA_LISTS)} take a list; "
                f"use --infra {provider}"
            )
        infra[provider] = names
    return infra
