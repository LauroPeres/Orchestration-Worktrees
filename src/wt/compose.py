"""Docker Compose runtime: one compose project per (branch, repo)."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from wt import proc
from wt.paths import instance

NETWORK = "wt"  # key of the shared network inside compose files


def network_name() -> str:
    return instance()


class Reset:
    """Renders as a compose `!reset` tag: drops the value set in the user's compose file."""

    def __init__(self, value: list[Any] | None) -> None:
        self.value = value


class _Dumper(yaml.SafeDumper):
    pass


def _represent_reset(dumper: yaml.SafeDumper, data: Reset) -> yaml.Node:
    if data.value is None:
        return dumper.represent_scalar("!reset", "")
    return dumper.represent_sequence("!reset", data.value)


_Dumper.add_representer(Reset, _represent_reset)


def dump(doc: dict[str, Any]) -> str:
    return yaml.dump(doc, Dumper=_Dumper, sort_keys=False)


@dataclass
class ServicePatch:
    alias: str | None = None  # network alias on the shared `wt` network (routed services)
    env: dict[str, str] = field(default_factory=dict)
    network: bool = False  # join the shared `wt` network even without an alias


def build_override(all_services: list[str], patches: dict[str, ServicePatch]) -> dict[str, Any]:
    """Override file that makes a compose project safe to run once per branch.

    Every service loses its published host ports and fixed container_name (they would collide
    between branches). Routed services join the shared `wt` network so Traefik can reach them.
    """
    services: dict[str, Any] = {}
    for name in all_services:
        spec: dict[str, Any] = {"ports": Reset([]), "container_name": Reset(None)}
        patch = patches.get(name)
        if patch and patch.env:
            spec["environment"] = dict(patch.env)
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


def up(project: str, files: list[Path], *, cwd: Path, build: bool, timeout: int) -> None:
    args = [*_base(project, files), "up", "-d", "--remove-orphans", "--wait", "--quiet-pull"]
    args += ["--wait-timeout", str(timeout)]
    if build:
        args.append("--build")
    proc.run(args, cwd=cwd, capture=False)


def down(project: str) -> None:
    """Stop and remove the project's containers, networks and volumes. No-op if absent."""
    proc.run(["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"])


@dataclass(frozen=True)
class Container:
    service: str
    state: str  # running, exited, ...
    health: str  # healthy, unhealthy, starting, or "" when there is no healthcheck


def ps(project: str) -> list[Container]:
    out = proc.run(
        ["docker", "compose", "-p", project, "ps", "-a", "--format", "json"], check=False
    ).stdout.strip()
    if not out:
        return []
    rows: list[dict[str, Any]]
    if out.startswith("["):
        rows = json.loads(out)
    else:
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [Container(r.get("Service", ""), r.get("State", ""), r.get("Health", "")) for r in rows]


def logs(project: str, service: str, *, follow: bool, tail: int) -> None:
    args = ["docker", "compose", "-p", project, "logs", "--tail", str(tail)]
    if follow:
        args.append("-f")
    proc.run([*args, service], capture=False, check=False)


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
