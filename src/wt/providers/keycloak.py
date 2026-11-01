"""Shared Keycloak server and one OIDC client per branch (docs/SPEC.md §19)."""

import json
import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wt import compose, proc
from wt.config import Config
from wt.config import Keycloak as KeycloakConfig
from wt.errors import WtError
from wt.paths import env_port, instance, wt_home
from wt.providers import server
from wt.providers.base import BranchContext, Check, Kind, Resources, branch_scope, recorded_names
from wt.state import atomic_write, file_lock

DEFAULT_IMAGE = "quay.io/keycloak/keycloak:26.8.1"
DEFAULT_PORT = 58080
ADMIN = "admin"
INTERNAL_PORT = 8080
SCOPE_SEPARATOR = "."

# One fixed script per kcadm call. It logs in with the container's own bootstrap admin
# variables (the password never reaches the host argv) into a unique config file that a trap
# removes, then runs the caller's arguments, which arrive only as "$@".
SCRIPT = (
    "CFG=$(mktemp /tmp/kcadm-XXXXXXXX.config); trap 'rm -f \"$CFG\"' EXIT; "
    '/opt/keycloak/bin/kcadm.sh config credentials --config "$CFG" '
    f"--server http://localhost:{INTERNAL_PORT} --realm master "
    '--user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" '
    '>/dev/null && /opt/keycloak/bin/kcadm.sh "$@" --config "$CFG"'
)

# What kcadm 26.8.1 prints (stderr, exit 1) when an admin REST path answers 404, which is how a
# missing realm is reported. A missing client is not an error: the list comes back empty (`[ ]`).
# Any other failure (login refused, server unreachable, container stopped) has other wording.
NOT_FOUND = re.compile(r"Resource not found for url: \S*/admin/realms/")
# kcadm prints this progress line to stderr on every call, even a successful one.
LOGIN_NOTICE = "Logging into"
# A line that opens a JSON or HTML body, not an error message.
FRAGMENT = re.compile(r"[\[\]{}<]")
# `down` shows the first ~120 characters of the message: 10 for the prefix, 26 for the call name.
REASON_MAX = 80
CALL_WORDS = 2


def container() -> str:
    """Name of the shared Keycloak container (also its compose project)."""
    return f"{instance()}-keycloak"


def volume() -> str:
    return f"{instance()}-keycloak-data"


def image() -> str:
    return os.environ.get("WT_KEYCLOAK_IMAGE", DEFAULT_IMAGE)


def host_port() -> int:
    return env_port("WT_KEYCLOAK_PORT", DEFAULT_PORT)


def server_dir() -> Path:
    return wt_home() / "keycloak"


def password_file() -> Path:
    return server_dir() / "password"


def password() -> str:
    """Generated once; the file is created with mode 0600 the first time it is needed."""
    path = password_file()
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            path.chmod(0o600)
            return existing
    with file_lock("keycloak-password"):
        existing = path.read_text().strip() if path.exists() else ""
        if not existing:
            existing = secrets.token_urlsafe(24)
            atomic_write(path, existing + "\n")
        path.chmod(0o600)
        return existing


def url() -> str:
    return f"http://localhost:{host_port()}"


def compose_doc() -> dict[str, Any]:
    return {
        "name": container(),
        "services": {
            "keycloak": {
                "image": image(),
                "container_name": container(),
                "command": ["start-dev"],
                "restart": "unless-stopped",
                "environment": {
                    "KC_BOOTSTRAP_ADMIN_USERNAME": ADMIN,
                    "KC_BOOTSTRAP_ADMIN_PASSWORD": password(),
                    "KC_HOSTNAME": url(),
                    "KC_HOSTNAME_BACKCHANNEL_DYNAMIC": "true",
                    "KC_HTTP_ENABLED": "true",
                },
                "ports": [f"127.0.0.1:{host_port()}:{INTERNAL_PORT}"],
                "volumes": [f"{volume()}:/opt/keycloak/data"],
                "networks": [compose.NETWORK],
            }
        },
        "networks": {compose.NETWORK: {"external": True, "name": compose.network_name()}},
        "volumes": {volume(): {"name": volume()}},
    }


def _failure_reason(res: proc.Result) -> str:
    """The first meaningful line kcadm (or docker exec) wrote: stderr first, then stdout.

    Skips the login notice and lines that are only a JSON or HTML fragment (`[`, `{`, `<html>`),
    which say nothing about the cause.
    """
    for stream in (res.stderr, res.stdout):
        for line in stream.splitlines():
            line = line.strip()
            if line and not line.startswith(LOGIN_NOTICE) and not FRAGMENT.match(line):
                return line
    return f"exit code {res.returncode}"


def _short(reason: str) -> str:
    """Cap the reason but keep its end, where kcadm puts the cause ("... Connection refused")."""
    if len(reason) <= REASON_MAX:
        return reason
    return "..." + reason[-(REASON_MAX - len("...")) :]


def _call_name(args: tuple[str, ...]) -> str:
    """`kcadm.sh <verb> <resource>`: never ids, realm or client names, the script or a password."""
    words = [arg.split("/")[0] for arg in args if not arg.startswith("-")]
    return " ".join(["kcadm.sh", *words[:CALL_WORDS]])


def _failure(args: tuple[str, ...], reason: str) -> WtError:
    """One line, cause first, so a caller that shows only the start of it still shows the cause."""
    return WtError(f"keycloak: {_short(reason)} ({_call_name(args)})")


def _kcadm_error(args: tuple[str, ...], res: proc.Result) -> WtError:
    return _failure(args, _failure_reason(res))


def kcadm(*args: str, check: bool = True) -> proc.Result:
    """Run one `kcadm.sh` command inside the server container (arguments stay positional)."""
    res = proc.run(
        ["docker", "exec", container(), "bash", "-c", SCRIPT, "kcadm", *args], check=False
    )
    if check and not res.ok:
        raise _kcadm_error(args, res)
    return res


def wait_ready(timeout: float = 120.0, total: float | None = None) -> None:
    server.wait_until(
        lambda: kcadm("get", "realms/master", check=False).ok,
        timeout,
        total,
        lambda waited: (
            f"Keycloak did not accept the admin login in {waited:.0f}s ({container()}). "
            f"If {password_file()} was deleted while the volume {volume()} stayed, the "
            "stored admin password no longer matches: restore the file or remove the volume."
        ),
    )


def ensure_realm(realm: str) -> None:
    args = (
        "create", "realms", "-s", f"realm={realm}", "-s", "enabled=true", "-s", "sslRequired=none",
    )  # fmt: skip
    res = kcadm(*args, check=False)
    if not res.ok and "already exists" not in f"{res.stdout}{res.stderr}":
        raise _kcadm_error(args, res)


def ensure_server(realm: str | None = None, timeout: int = 120) -> None:
    server.start(container(), server_dir(), compose_doc(), timeout, wait_ready)
    if realm is not None:
        ensure_realm(realm)


def find_client(realm: str, client_id: str) -> dict[str, Any] | None:
    """The client whose clientId is exactly `client_id` (the server filter is a substring).

    None only when the realm or the client does not exist; every other failure raises, so a
    stopped server is never read as "nothing to do".
    """
    args = (
        "get", "clients", "-r", realm, "-q", f"clientId={client_id}",
        "--fields", "id,clientId,redirectUris",
    )  # fmt: skip
    res = kcadm(*args, check=False)
    if not res.ok:
        if NOT_FOUND.search(res.stderr):
            return None
        raise _kcadm_error(args, res)
    try:
        rows = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise _failure(args, "did not return JSON") from e
    if not isinstance(rows, list):
        raise _failure(args, "did not return a list")
    for row in rows:
        if row.get("clientId") == client_id:
            return dict(row)
    return None


class KeycloakProvider:
    name = "keycloak"

    def __init__(self, cfg: Config, log: Callable[[str], None] = lambda message: None) -> None:
        # No I/O here: providers are built on every render. `cfg` may lack the keycloak section
        # when `down` runs after the section was removed.
        self.cfg: KeycloakConfig | None = cfg.shared.keycloak
        self.log = log

    def _cfg(self) -> KeycloakConfig:
        if self.cfg is None:
            raise WtError("the keycloak provider needs infra.keycloak in the catalog")
        return self.cfg

    def client_id(self, ctx: BranchContext) -> str:
        """`feat/cart` in project `shop` with client `web` -> `web-shop.feat-cart`."""
        return f"{self._cfg().client}-{branch_scope(ctx, SCOPE_SEPARATOR)}"

    @staticmethod
    def redirect_uris(ctx: BranchContext) -> list[str]:
        return [u + "/*" for u in ctx.urls]

    def ensure_server(self) -> None:
        # Without the section (`down` after it left the config) there is no realm to create,
        # but the server must still be up so `destroy` can delete the recorded clients.
        ensure_server(self.cfg.realm if self.cfg is not None else None)

    def preflight(self, ctx: BranchContext) -> None:
        pass

    def create(self, ctx: BranchContext) -> Resources:
        realm, client_id = self._cfg().realm, self.client_id(ctx)
        sets = [
            f"clientId={client_id}",
            "publicClient=true",
            "standardFlowEnabled=true",
            f"redirectUris={json.dumps(self.redirect_uris(ctx))}",
            f"webOrigins={json.dumps(list(ctx.urls))}",
        ]
        flags = [part for s in sets for part in ("-s", s)]
        existing = find_client(realm, client_id)
        if existing is None:
            kcadm("create", "clients", "-r", realm, *flags)
        else:
            kcadm("update", f"clients/{existing['id']}", "-r", realm, *flags)
        return Resources([f"{realm}/{client_id}"])

    def verify(self, ctx: BranchContext) -> Check:
        realm, client_id = self._cfg().realm, self.client_id(ctx)
        row = find_client(realm, client_id)
        if row is None:
            return Check(False, f"client {client_id!r} not found in realm {realm!r}")
        if set(row.get("redirectUris", [])) != set(self.redirect_uris(ctx)):
            return Check(False, f"client {client_id!r} has unexpected redirect URIs")
        warnings = [] if ctx.urls else ["no routed services: the client has no redirect URIs"]
        return Check(True, f"client {client_id!r} in realm {realm!r} is ready", warnings)

    def destroy(self, ctx: BranchContext) -> None:
        wanted = recorded_names(ctx, self.name)
        if self.cfg is not None:
            wanted.append(f"{self.cfg.realm}/{self.client_id(ctx)}")
        for entry in dict.fromkeys(wanted):
            realm, sep, client_id = entry.partition("/")
            if not sep or not realm or not client_id:
                continue
            row = find_client(realm, client_id)
            if row is not None:
                kcadm("delete", f"clients/{row['id']}", "-r", realm)

    def template_vars(self, ctx: BranchContext, kind: Kind) -> dict[str, Any]:
        realm = self._cfg().realm
        public = url()
        internal = f"http://{container()}:{INTERNAL_PORT}" if kind == "compose" else public
        return {
            "keycloak": {
                "url": public,
                "issuer": f"{public}/realms/{realm}",
                "realm": realm,
                "client_id": self.client_id(ctx),
                "internal_url": internal,
            }
        }
