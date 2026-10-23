"""`wt doctor`: check everything wt depends on, and fix what can be fixed."""

import platform
import re
import socket
from dataclasses import dataclass
from typing import Literal

from wt import compose, host, proc, router, tls
from wt.config import LoadedConfig
from wt.errors import WtError
from wt.providers import postgres
from wt.state import StateStore, file_lock

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
            router.ensure_running(force_recreate=True)  # an Exited one may hold a stale mount
        running = router.is_running()
    stale = running and not router.has_host_gateway()
    if stale and fix:
        with file_lock("traefik"):
            router.ensure_running()
        stale = not router.has_host_gateway()
    if stale:
        checks.append(
            Check(
                "traefik",
                "warn",
                f"{router.container()} lacks {router.HOST_GATEWAY_NAME} (older wt container)",
                "recreated by `wt doctor --fix` or the next `wt up`",
            )
        )
    else:
        checks.append(
            Check(
                "traefik",
                "ok" if running else "warn",
                f"{router.container()} ({router.image()})" if running else "not running",
                "" if running else "started on first `wt up` (or `wt doctor --fix`)",
            )
        )
    if running:
        checks.append(_check_mounts(fix))
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


_MOUNTS_HINT = "run `wt doctor --fix` (recreates the container)"


def _check_mounts(fix: bool) -> Check:
    missing = router.missing_in_container()
    if missing and fix:
        try:
            with file_lock("traefik"):
                router.restart()
        except WtError as exc:
            return Check(
                "traefik mounts",
                "fail",
                f"recreating {router.container()} failed: {exc}",
                _MOUNTS_HINT,
            )
        missing = router.missing_in_container()
    if not missing:
        return Check("traefik mounts", "ok", f"{router.container()} sees the routes and certs")
    shown = ", ".join(missing[:3]) + (f" (+{len(missing) - 3} more)" if len(missing) > 3 else "")
    return Check(
        "traefik mounts",
        "fail",
        f"{router.container()} does not see: {shown} (stale bind mount)",
        _MOUNTS_HINT,
    )


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


def check_postgres(databases: list[str], *, fix: bool) -> list[Check]:
    """Shared Postgres: container, password file mode, closed databases (no secrets shown)."""
    checks: list[Check] = []
    name = postgres.container()
    running = _container_running(name)
    if not running and fix:
        try:
            with file_lock("postgres"):
                postgres.ensure_server(databases)
        except WtError as exc:
            checks.append(Check("postgres", "fail", str(exc).splitlines()[0]))
        running = _container_running(name)
    if running:
        checks.append(Check("postgres", "ok", f"{name} running"))
    elif not any(c.name == "postgres" for c in checks):
        checks.append(
            Check(
                "postgres",
                "warn",
                f"{name} not running",
                "started on first `wt up` (or `wt doctor --fix`)",
            )
        )
    checks.append(_check_password(fix))
    if running:
        checks.append(_check_connections(databases, fix))
    return checks


def _container_running(name: str) -> bool:
    result = proc.run(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False)
    return result.ok and result.stdout.strip() == "true"


def _check_password(fix: bool) -> Check:
    path = postgres.password_file()
    if not path.exists():
        return Check(
            "postgres password", "info", "not created yet", "created when the server first starts"
        )
    mode = path.stat().st_mode & 0o777
    if mode != 0o600 and fix:
        path.chmod(0o600)
        mode = 0o600
    if mode == 0o600:
        return Check("postgres password", "ok", "file exists, mode 0600")
    return Check(
        "postgres password",
        "warn",
        f"file mode is {mode:04o}, expected 0600",
        "run `wt doctor --fix`",
    )


def _check_connections(databases: list[str], fix: bool) -> Check:
    try:
        closed = postgres.closed_databases(databases)
        if closed and fix:
            postgres.repair_connections(databases)
            closed = postgres.closed_databases(databases)
    except WtError as exc:
        return Check("postgres databases", "fail", str(exc).splitlines()[0])
    if not closed:
        return Check("postgres databases", "ok", "all accept connections")
    return Check(
        "postgres databases",
        "fail",
        f"connections disabled: {', '.join(closed)}",
        "run `wt doctor --fix`",
    )


def check_ports(project: str) -> list[Check]:
    """Recorded ports of `run:` services whose process is gone must be free to restart."""
    checks: list[Check] = []
    state = StateStore(project).load()
    for branch in state.branches.values():
        for service, ps in branch.processes.items():
            if ps.port is None or host.alive(ps):
                continue
            label = f"port {ps.port}"
            where = f"{branch.branch}/{service}"
            if host.port_free(ps.port):
                checks.append(Check(label, "ok", f"{where} (stopped) port free"))
            else:
                checks.append(
                    Check(
                        label,
                        "fail",
                        f"{where} (stopped) port is used by another process",
                        "free it, then `wt up` picks a port again",
                    )
                )
    return checks


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
        if docker_ok and loaded.config.shared.postgres is not None:
            checks += check_postgres(loaded.config.shared.postgres.databases, fix=fix)
        checks += check_ports(loaded.config.project)
    else:
        checks.append(Check("config", "info", "no worktree.yaml here", "run `wt init`"))
    return checks
