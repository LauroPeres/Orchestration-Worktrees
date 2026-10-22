"""`wt doctor`: check everything wt depends on, and fix what can be fixed."""

import platform
import re
import socket
from dataclasses import dataclass
from typing import Literal

from wt import compose, proc, router, tls
from wt.config import LoadedConfig
from wt.state import file_lock

Level = Literal["ok", "info", "warn", "fail"]
MIN_COMPOSE = (2, 24)  # `!reset` in override files


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str
    hint: str = ""


def _version(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(g) for g in match.groups() if g is not None) if match else ()


def is_wsl() -> bool:
    return "microsoft" in platform.release().lower()


def _port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _traefik_ports() -> str:
    return proc.run(["docker", "port", router.container()], check=False).stdout


def check_git() -> Check:
    if not proc.which("git"):
        return Check("git", "fail", "not installed", "install git 2.20+")
    return Check("git", "ok", proc.run(["git", "--version"]).stdout.strip())


def check_docker() -> list[Check]:
    if not proc.which("docker"):
        return [
            Check(
                "docker",
                "fail",
                "not installed",
                "install Docker Desktop (macOS/Windows) or Docker Engine (Linux)",
            )
        ]
    info = proc.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False)
    if not info.ok:
        return [Check("docker", "fail", "daemon not reachable", "start Docker and retry")]
    checks = [Check("docker", "ok", f"engine {info.stdout.strip()}")]
    out = proc.run(["docker", "compose", "version", "--short"], check=False)
    version = _version(out.stdout) if out.ok else ()
    if not version:
        checks.append(
            Check(
                "compose",
                "fail",
                "docker compose v2 not found",
                "install the Docker Compose plugin",
            )
        )
    elif version < MIN_COMPOSE:
        checks.append(
            Check(
                "compose",
                "fail",
                f"{out.stdout.strip()} is too old",
                f"wt needs Docker Compose {'.'.join(map(str, MIN_COMPOSE))}+",
            )
        )
    else:
        checks.append(Check("compose", "ok", out.stdout.strip()))
    return checks


def check_tls(fix: bool) -> Check:
    if not tls.available():
        return Check(
            "mkcert",
            "warn",
            "not installed: branches get a self-signed certificate",
            "install mkcert (https://github.com/FiloSottile/mkcert), then `wt doctor --fix`",
        )
    if not tls.ca_ready() and fix:
        tls.install_ca()
    if not tls.ca_ready():
        return Check("mkcert", "warn", "local CA not created", "run `wt doctor --fix`")
    hint = ""
    if is_wsl():
        hint = (
            f"WSL2: browsers on Windows do not trust the Linux CA. Import "
            f"{tls.caroot()}/rootCA.pem into Windows 'Trusted Root Certification Authorities'"
        )
    return Check("mkcert", "ok", f"CA at {tls.caroot()}", hint)


def check_routing(fix: bool) -> list[Check]:
    checks: list[Check] = []
    exists = proc.run(["docker", "network", "inspect", compose.network_name()], check=False).ok
    if not exists and fix:
        compose.ensure_network()
        exists = True
    checks.append(
        Check(
            "network",
            "ok" if exists else "warn",
            f"docker network '{compose.network_name()}'" + ("" if exists else " missing"),
            "" if exists else "created on first `wt up` (or `wt doctor --fix`)",
        )
    )
    running = router.is_running()
    ports_taken = _port_in_use(router.http_port()) or _port_in_use(router.https_port())
    if not running and fix and not ports_taken:
        with file_lock("traefik"):
            router.ensure_running()
        running = router.is_running()
    checks.append(
        Check(
            "traefik",
            "ok" if running else "warn",
            f"{router.container()} ({router.image()})" if running else "not running",
            "" if running else "started on first `wt up` (or `wt doctor --fix`)",
        )
    )
    mapped = _traefik_ports() if running else ""
    for label, port, env in [
        ("http port", router.http_port(), "WT_HTTP_PORT"),
        ("https port", router.https_port(), "WT_HTTPS_PORT"),
    ]:
        if running and f":{port}" in mapped:
            checks.append(Check(label, "ok", f"{port} served by {router.container()}"))
        elif _port_in_use(port):
            checks.append(
                Check(
                    label,
                    "fail",
                    f"{port} is used by another process",
                    f"free it, or pick another port with {env}=<port>",
                )
            )
        else:
            checks.append(Check(label, "ok", f"{port} free"))
    return checks


def check_resolution(domain: str) -> Check:
    name = f"wt-doctor.{domain}"
    try:
        addrs = {str(info[4][0]) for info in socket.getaddrinfo(name, 443)}
    except OSError:
        addrs = set()
    if addrs and addrs <= {"127.0.0.1", "::1"}:
        return Check("resolver", "ok", f"*.{domain} -> loopback")
    found = ", ".join(sorted(addrs)) or "does not resolve"
    return Check(
        "resolver",
        "warn",
        f"*.{domain} {found} for the OS resolver",
        "browsers, curl and wt's containers are fine; only other local programs may fail "
        "(fix with /etc/hosts, dnsmasq or systemd-resolved)",
    )


def run_checks(loaded: LoadedConfig | None, config_error: str | None, *, fix: bool) -> list[Check]:
    checks = [check_git()]
    docker = check_docker()
    checks += docker
    docker_ok = all(c.level == "ok" for c in docker)
    checks.append(check_tls(fix))
    if docker_ok:
        checks += check_routing(fix)
    if config_error:
        checks.append(Check("config", "fail", config_error))
    elif loaded:
        checks.append(Check("config", "ok", f"{loaded.path} (project '{loaded.config.project}')"))
        checks.append(check_resolution(loaded.config.domain))
    else:
        checks.append(Check("config", "info", "no worktree.yaml here", "run `wt init`"))
    return checks
