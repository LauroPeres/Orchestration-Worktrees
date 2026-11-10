"""Docker Compose runtime: one compose project per (branch, repo)."""

import json
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from wt import host, proc
from wt.errors import WtError
from wt.paths import instance

NETWORK = "wt"  # key of the shared network inside compose files


def network_name() -> str:
    return instance()


class Reset:
    """Renders as a compose `!reset` tag: drops the value set in the user's compose file."""

    def __init__(self, value: list[Any] | None) -> None:
        self.value = value


class Override:
    """Renders as a compose `!override` tag: replaces the list from the user's compose file
    (`!reset` with a value would just drop it)."""

    def __init__(self, value: list[Any]) -> None:
        self.value = value


class _Dumper(yaml.SafeDumper):
    pass


def _represent_reset(dumper: yaml.SafeDumper, data: Reset) -> yaml.Node:
    if data.value is None:
        return dumper.represent_scalar("!reset", "")
    return dumper.represent_sequence("!reset", data.value)


def _represent_override(dumper: yaml.SafeDumper, data: Override) -> yaml.Node:
    return dumper.represent_sequence("!override", data.value)


_Dumper.add_representer(Reset, _represent_reset)
_Dumper.add_representer(Override, _represent_override)


def dump(doc: dict[str, Any]) -> str:
    return yaml.dump(doc, Dumper=_Dumper, sort_keys=False)


@dataclass
class ServicePatch:
    alias: str | None = None  # network alias on the shared `wt` network (routed services)
    env: dict[str, str] = field(default_factory=dict)
    network: bool = False  # join the shared `wt` network even without an alias
    publish: tuple[int, int] | None = None  # (host port, container port) bound on 127.0.0.1


def build_override(all_services: list[str], patches: dict[str, ServicePatch]) -> dict[str, Any]:
    """Override file that makes a compose project safe to run once per branch.

    Every service loses its published host ports and fixed container_name (they would collide
    between branches); a patch with `publish` gets one loopback host port back.
    Routed services join the shared `wt` network so Traefik can reach them.
    """
    services: dict[str, Any] = {}
    for name in all_services:
        patch = patches.get(name)
        ports: Reset | Override = Reset([])
        if patch and patch.publish:
            ports = Override([f"127.0.0.1:{patch.publish[0]}:{patch.publish[1]}"])
        spec: dict[str, Any] = {"ports": ports, "container_name": Reset(None)}
        if patch and patch.env:
            # Compose interpolates `$` in values; `$$` makes it pass them literally.
            spec["environment"] = {k: v.replace("$", "$$") for k, v in patch.env.items()}
        if patch and (patch.alias or patch.network):
            joined: dict[str, Any] = {"aliases": [patch.alias]} if patch.alias else {}
            spec["networks"] = {"default": {}, NETWORK: joined}
        services[name] = spec
    doc: dict[str, Any] = {"services": services}
    if any(p.alias or p.network for p in patches.values()):
        doc["networks"] = {NETWORK: {"external": True, "name": network_name()}}
    return doc


def _base(project: str, files: list[Path]) -> list[str]:
    args = ["docker", "compose", "-p", project]
    for f in files:
        args += ["-f", str(f)]
    return args


def list_services(compose_file: Path) -> list[str]:
    out = proc.run(
        [*_base("wt-probe", [compose_file]), "config", "--services"], cwd=compose_file.parent
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def up(  # noqa: PLR0913
    project: str,
    files: list[Path],
    *,
    cwd: Path,
    build: bool,
    timeout: int,
    on_line: Callable[[str], None] | None = None,
) -> None:
    args = [*_base(project, files), "up", "-d", "--remove-orphans", "--wait", "--quiet-pull"]
    args += ["--wait-timeout", str(timeout)]
    if build:
        args.append("--build")
    proc.run(args, cwd=cwd, capture=False, on_line=on_line)


def down(project: str) -> None:
    """Remove the project's containers, networks, volumes and locally built images.

    No-op if absent.
    """
    proc.run(
        ["docker", "compose", "-p", project, "down", "-v", "--remove-orphans", "--rmi", "local"]
    )


def down_keep_volumes(project: str) -> None:
    """Stop and remove the project's containers and networks; volumes stay. No-op if absent."""
    proc.run(["docker", "compose", "-p", project, "down", "--remove-orphans"])


PROJECT_LABEL = "com.docker.compose.project"


def _image_ids(*label_filters: str) -> list[str]:
    """Distinct ids of the images carrying every label filter (`key` or `key=value`)."""
    args = ["docker", "image", "ls", "-q"]
    for label in label_filters:
        args += ["--filter", f"label={label}"]
    return list(dict.fromkeys(proc.run(args, check=False).stdout.split()))


@dataclass(frozen=True)
class _TaggedImage:
    id: str
    repository: str
    tag: str


def _images_named_after(project: str) -> list[_TaggedImage]:
    """Tagged images whose repository is `<project>-<service>`, Compose's default name for the
    image of a `build:` service. A pulled image (`postgres:16`) never has that name."""
    image_format = "{{.ID}} {{.Repository}} {{.Tag}}"
    reference = f"reference={project}-*"
    args = ["docker", "image", "ls", "--filter", reference, "--format", image_format]
    images: list[_TaggedImage] = []
    for line in proc.run(args, check=False).stdout.splitlines():
        image_id, _, rest = line.strip().partition(" ")
        repository, _, tag = rest.partition(" ")
        if repository.startswith(f"{project}-") and tag and tag != "<none>":
            images.append(_TaggedImage(image_id, repository, tag))
    return images


def _named_by_a_longer_project(repository: str, project: str, others: set[str]) -> bool:
    """`wt-x-notes-back-api` is not `wt-x-notes`'s image when `wt-x-notes-back` is a project."""
    return any(
        repository.startswith(f"{other}-") for other in others if other.startswith(f"{project}-")
    )


def remove_images(project: str) -> list[str]:
    """Remove the images of the compose project; returns their ids.

    An image is the project's when it is labelled `com.docker.compose.project=<project>` or
    named `<project>-<service>`: some Compose versions do not label the images they build, and
    `down -p <project> --rmi local` (without the compose files) leaves those behind. Named
    images go by `repository:tag`, since an image tagged for two services cannot be removed
    by id; the labelled ones that have no such name go by id.
    """
    labelled = _image_ids(f"{PROJECT_LABEL}={project}")
    named = _images_named_after(project)
    if named:
        others = _project_names() - {project}
        named = [i for i in named if not _named_by_a_longer_project(i.repository, project, others)]
    named_ids = {image.id for image in named}
    targets = [f"{image.repository}:{image.tag}" for image in named]
    targets += [image_id for image_id in labelled if image_id not in named_ids]
    if targets:
        proc.run(["docker", "image", "rm", *targets])
    return list(dict.fromkeys([*(image.id for image in named), *labelled]))


def _lines(args: list[str]) -> set[str]:
    return {line.strip() for line in proc.run(args, check=False).stdout.splitlines()}


def _project_names() -> set[str]:
    """Every compose project name on the labels of containers, volumes and images."""
    label_value = f'{{{{.Label "{PROJECT_LABEL}"}}}}'
    names = _lines(
        ["docker", "ps", "-a", "--filter", f"label={PROJECT_LABEL}", "--format", label_value]
    )
    names |= _lines(
        ["docker", "volume", "ls", "--filter", f"label={PROJECT_LABEL}", "--format", label_value]
    )
    # `docker image ls` cannot print labels: inspect the ids it finds.
    if image_ids := _image_ids(PROJECT_LABEL):
        inspect_label = f'{{{{index .Config.Labels "{PROJECT_LABEL}"}}}}'
        names |= _lines(["docker", "image", "inspect", "--format", inspect_label, *image_ids])
    return {name for name in names if name}


def wt_projects(prefix: str) -> set[str]:
    """Compose project names starting with `prefix`, from the labels of containers, volumes
    and images (so a project with only a volume or a labelled image left is found too).

    Image names are not used: `<project>-<service>` cannot be split when both hold hyphens.
    """
    return {name for name in _project_names() if name.startswith(prefix)}


def remove_service(project: str, service: str) -> None:
    """Stop and remove the containers of one compose service; the rest of the project stays."""
    found = proc.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ],
        check=False,
    ).stdout.split()
    if found:
        proc.run(["docker", "rm", "-f", *found])


@dataclass(frozen=True)
class Container:
    service: str
    state: str  # running, exited, ...
    health: str  # healthy, unhealthy, starting, or "" when there is no healthcheck


def ps(project: str) -> list[Container]:
    result = proc.run(
        ["docker", "compose", "-p", project, "ps", "-a", "--format", "json"], check=False
    )
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise WtError(f"docker is not reachable: {detail[-1] if detail else 'compose ps failed'}")
    out = result.stdout.strip()
    if not out:
        return []
    rows: list[dict[str, Any]]
    if out.startswith("["):
        rows = json.loads(out)
    else:
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [Container(r.get("Service", ""), r.get("State", ""), r.get("Health", "")) for r in rows]


def logs_command(project: str, service: str, *, follow: bool, tail: int) -> list[str]:
    args = ["docker", "compose", "-p", project, "logs", "--tail", str(tail)]
    if follow:
        args.append("-f")
    return [*args, service]


def ensure_network() -> None:
    inspect = ["docker", "network", "inspect", network_name()]
    if proc.run(inspect, check=False).ok:
        return
    try:
        proc.run(["docker", "network", "create", network_name()])
    except proc.CommandError:
        # Another wt process may have created it between inspect and create.
        if not proc.run(inspect, check=False).ok:
            raise


def published_host_ports(container: str) -> set[int]:
    """Host ports the container publishes, read from `docker port` (empty when it has none).

    A stopped or missing container makes `docker port` fail or print nothing: both mean
    "no published ports". Lines look like `5432/tcp -> 127.0.0.1:55432` or `443/tcp -> [::]:443`.
    """
    result = proc.run(["docker", "port", container], check=False)
    if not result.ok:
        return set()
    ports: set[int] = set()
    for line in result.stdout.splitlines():
        _, arrow, bound = line.partition("->")
        _, colon, port = bound.strip().rpartition(":")
        if arrow and colon and port.isdecimal():
            ports.add(int(port))
    return ports


def ensure_published(
    container: str, host_ports: Collection[int], recreate: Callable[[], None]
) -> None:
    """Make sure the container publishes every host port; recreate it once if it does not.

    After a start that failed on a taken host port, Docker's next start runs the container
    with no network and no published ports while it still reports healthy.
    """
    if not host_ports:
        return
    missing = set(host_ports) - published_host_ports(container)
    if not missing:
        return
    try:
        recreate()
    except WtError as exc:
        # A recreate that fails on the bind surfaces Docker's raw error; name the port instead.
        # Only unpublished ports are probed: the ones the container holds look busy.
        busy = _first_busy_port(missing)
        if busy is None:
            raise
        raise WtError(f"port {busy} is already in use by another program") from exc
    still_missing = sorted(set(host_ports) - published_host_ports(container))
    if not still_missing:
        return
    port = still_missing[0]
    if not host.port_free(port):
        raise WtError(f"port {port} is already in use by another program")
    raise WtError(f"{container} does not publish port {port}")


def _first_busy_port(host_ports: Collection[int]) -> int | None:
    """The lowest of `host_ports` that another program listens on, or None when all are free."""
    return next((port for port in sorted(host_ports) if not host.port_free(port)), None)
