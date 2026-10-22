"""Shared Traefik (file provider) and one route file per branch."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from wt import compose, proc
from wt.naming import Names
from wt.paths import env_port, instance, wt_home
from wt.state import atomic_write

DEFAULT_IMAGE = "traefik:v3.5"
CERTS_IN_CONTAINER = "/etc/traefik/certs"
HOST_GATEWAY_NAME = "host.docker.internal"  # how the Traefik container reaches host processes


def container() -> str:
    """Name of the shared Traefik container (also its compose project)."""
    return f"{instance()}-traefik"


def traefik_dir() -> Path:
    return wt_home() / "traefik"


def dynamic_dir() -> Path:
    return traefik_dir() / "dynamic"


def certs_dir() -> Path:
    return traefik_dir() / "certs"


def http_port() -> int:
    return env_port("WT_HTTP_PORT", 80)


def https_port() -> int:
    return env_port("WT_HTTPS_PORT", 443)


def image() -> str:
    return os.environ.get("WT_TRAEFIK_IMAGE", DEFAULT_IMAGE)


def compose_doc() -> dict[str, Any]:
    """Static config is passed as flags, so any change recreates the container on `up`."""
    redirect_to = "websecure" if https_port() == 443 else f":{https_port()}"
    return {
        "name": container(),
        "services": {
            "traefik": {
                "image": image(),
                "container_name": container(),
                "restart": "unless-stopped",
                "command": [
                    "--entrypoints.web.address=:80",
                    f"--entrypoints.web.http.redirections.entrypoint.to={redirect_to}",
                    "--entrypoints.web.http.redirections.entrypoint.scheme=https",
                    "--entrypoints.websecure.address=:443",
                    "--providers.file.directory=/etc/traefik/dynamic",
                    "--providers.file.watch=true",
                    "--ping=true",
                    "--log.level=INFO",
                ],
                "ports": [f"127.0.0.1:{http_port()}:80", f"127.0.0.1:{https_port()}:443"],
                "volumes": [
                    "./dynamic:/etc/traefik/dynamic:ro",
                    f"./certs:{CERTS_IN_CONTAINER}:ro",
                ],
                "extra_hosts": [f"{HOST_GATEWAY_NAME}:host-gateway"],
                "networks": [compose.NETWORK],
                "healthcheck": {
                    "test": ["CMD", "traefik", "healthcheck", "--ping"],
                    "interval": "2s",
                    "timeout": "2s",
                    "retries": 15,
                },
            }
        },
        "networks": {compose.NETWORK: {"external": True, "name": compose.network_name()}},
    }


def host_upstream(port: int) -> str:
    return f"http://{HOST_GATEWAY_NAME}:{port}"


def ensure_running(timeout: int = 60) -> None:
    """Start (or update) the shared Traefik. Safe to call on every `up`."""
    folder = traefik_dir()
    dynamic_dir().mkdir(parents=True, exist_ok=True)
    certs_dir().mkdir(parents=True, exist_ok=True)
    compose_file = folder / "compose.yml"
    atomic_write(compose_file, yaml.safe_dump(compose_doc(), sort_keys=False))
    compose.ensure_network()
    proc.run(
        [
            "docker",
            "compose",
            "-p",
            container(),
            "-f",
            str(compose_file),
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            str(timeout),
        ],
        cwd=folder,
    )


def is_running() -> bool:
    result = proc.run(["docker", "inspect", "-f", "{{.State.Running}}", container()], check=False)
    return result.ok and result.stdout.strip() == "true"


def has_host_gateway() -> bool:
    """True when the running container maps `host.docker.internal` to the host gateway."""
    result = proc.run(
        ["docker", "inspect", "-f", "{{json .HostConfig.ExtraHosts}}", container()], check=False
    )
    return result.ok and f"{HOST_GATEWAY_NAME}:host-gateway" in result.stdout


@dataclass(frozen=True)
class Route:
    service: str
    host: str
    upstream: str  # e.g. http://wt-shop-feat-cart-api:8000


def route_file(names: Names) -> Path:
    return dynamic_dir() / f"{names.key}.yml"


def route_doc(names: Names, routes: list[Route], cert: tuple[Path, Path] | None) -> dict[str, Any]:
    routers: dict[str, Any] = {}
    services: dict[str, Any] = {}
    for r in routes:
        key = names.router(r.service)
        routers[key] = {
            "rule": f"Host(`{r.host}`)",
            "entryPoints": ["websecure"],
            "service": key,
            "tls": {},
        }
        services[key] = {"loadBalancer": {"servers": [{"url": r.upstream}]}}
    doc: dict[str, Any] = {"http": {"routers": routers, "services": services}}
    if cert:
        cert_file, key_file = cert
        doc["tls"] = {
            "certificates": [
                {
                    "certFile": f"{CERTS_IN_CONTAINER}/{cert_file.name}",
                    "keyFile": f"{CERTS_IN_CONTAINER}/{key_file.name}",
                }
            ]
        }
    return doc


def write_routes(names: Names, routes: list[Route], cert: tuple[Path, Path] | None) -> Path:
    path = route_file(names)
    # atomic: Traefik watches the folder and only loads *.yml, never the temp file
    atomic_write(path, yaml.safe_dump(route_doc(names, routes, cert), sort_keys=False))
    return path


def remove_routes(path: Path) -> None:
    path.unlink(missing_ok=True)
