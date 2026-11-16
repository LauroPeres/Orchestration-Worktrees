"""`wt demo`: the shop example, `main` and `feat/cart`, in an environment of its own (SPEC §37).

The demo runs the existing use cases (`Orchestrator.up` and `remove`) unchanged. What isolates it
from a wt the visitor already runs is the environment applied by `demo_environment`: another
instance name, home, catalog, shared-server ports, and no inherited `WT_*` variable.
"""

import os
import shutil
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from wt import compose, doctor, paths, proc, router, tls
from wt.catalog import load_project
from wt.errors import WtError
from wt.orchestrator import Orchestrator, ServiceReport
from wt.providers import kafka, keycloak, postgres, redis

Log = Callable[[str], None]
PortInUse = Callable[[int], bool]

INSTANCE = "wtdemo"
STATE_DIR_NAME = "wt-demo"
EXAMPLE = "shop"
BRANCHES = ("main", "feat/cart")
USES = ["web", "worker"]
WEB_SERVICE = "web"
API_SERVICE = "api"
API_PATH = "/items"
ENV_PREFIX = "WT_"
# Shared servers listen next to the visitor's own (default + offset), so both can run at once.
SERVER_PORT_OFFSET = 100
SERVER_PORT_VARIABLES = {
    "WT_POSTGRES_PORT": postgres.DEFAULT_PORT,
    "WT_REDIS_PORT": redis.DEFAULT_PORT,
    "WT_KAFKA_PORT": kafka.DEFAULT_PORT,
    "WT_KEYCLOAK_PORT": keycloak.DEFAULT_PORT,
}
DEFAULT_HTTP_PORT = "80"
DEFAULT_HTTPS_PORT = "443"
# Where to move to when a port is taken: (variable, a port that is usually free).
HTTP_RETRY = ("WT_HTTP_PORT", 8080)
HTTPS_RETRY = ("WT_HTTPS_PORT", 8443)
CLEANUP_HINT = "run `wt demo down` to clean up"


@dataclass(frozen=True)
class DemoBranch:
    branch: str
    web: str  # https URL of the page
    api: str  # https URL of the items endpoint


@dataclass(frozen=True)
class DemoResult:
    branches: tuple[DemoBranch, ...]
    home: Path
    self_signed: bool  # mkcert is missing: the browser warns about the certificate


def demo_home() -> Path:
    """`$XDG_STATE_HOME/wt-demo`, else `~/.local/state/wt-demo`; never the visitor's `~/.wt`."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / STATE_DIR_NAME


def is_first_run() -> bool:
    """True before the demo ever ran: the images still have to be downloaded."""
    return not demo_home().exists()


def _demo_variables(home: Path, visitor: Mapping[str, str]) -> dict[str, str]:
    variables = {
        "WT_INSTANCE": INSTANCE,
        "WT_HOME": str(home),
        "WT_CATALOG": str(_catalog_file(home)),
        "WT_HTTP_PORT": visitor.get("WT_HTTP_PORT", DEFAULT_HTTP_PORT),
        "WT_HTTPS_PORT": visitor.get("WT_HTTPS_PORT", DEFAULT_HTTPS_PORT),
        "WT_NO_FETCH": "1",
    }
    for name, default in SERVER_PORT_VARIABLES.items():
        variables[name] = str(default + SERVER_PORT_OFFSET)
    return variables


@contextmanager
def demo_environment() -> Iterator[None]:
    """Apply the demo's `WT_*` variables and hide the visitor's; restore them on any exit."""
    visitor = {k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIX)}
    variables = _demo_variables(demo_home(), visitor)
    try:
        for name in visitor:
            del os.environ[name]
        os.environ.update(variables)
        yield
    finally:
        for name in [k for k in os.environ if k.startswith(ENV_PREFIX)]:
            del os.environ[name]
        os.environ.update(visitor)


def _catalog_file(home: Path) -> Path:
    return home / EXAMPLE / "catalog.yaml"


def _delete_folder(folder: Path) -> None:
    if not folder.exists():
        return
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        raise WtError(f"cannot delete {folder}: {exc}") from exc


def _require_docker() -> None:
    failed = [check for check in doctor.check_docker() if check.level == "fail"]
    if failed:
        first = failed[0]
        raise WtError(f"{first.name}: {first.detail}; {first.hint}")


def _require_free_ports(port_in_use: PortInUse) -> None:
    """Both ports are free, or held by the demo's own Traefik; nothing is created before this."""
    if router.is_running():
        return
    for port, (variable, example) in (
        (router.http_port(), HTTP_RETRY),
        (router.https_port(), HTTPS_RETRY),
    ):
        if port_in_use(port):
            raise WtError(
                f"port {port} is in use (another Traefik?). "
                f"Set {variable}, e.g. {variable}={example} wt demo"
            )


def _bootstrap(home: Path, log: Log) -> None:
    """Create the shop repos and catalog under `home`, unless they are already there."""
    shop = home / EXAMPLE
    if _catalog_file(home).is_file():
        return
    log(f"creating the {EXAMPLE} example in {shop}")
    _delete_folder(shop)  # a bootstrap that stopped halfway leaves a folder
    script = paths.example_dir(EXAMPLE) / "bootstrap.sh"
    proc.run(["bash", str(script), str(shop)])  # `bash`: the wheel does not keep the exec bit


def _unhealthy(branch: str, reports: list[ServiceReport]) -> WtError | None:
    failing = [f"{r.service}: {r.detail}" for r in reports if r.outcome == "fail"]
    if not failing:
        return None
    return WtError(f"{branch} is up but not healthy ({'; '.join(failing)})")


def _url(urls: Mapping[str, str], branch: str, service: str) -> str:
    try:
        return urls[service]
    except KeyError:
        raise WtError(f"{branch} has no {service} URL") from None


def _demo_branch(orch: Orchestrator, branch: str) -> DemoBranch:
    urls = orch.urls(branch)
    return DemoBranch(
        branch,
        _url(urls, branch, WEB_SERVICE),
        _url(urls, branch, API_SERVICE) + API_PATH,
    )


def _start(home: Path, log: Log) -> DemoResult:
    _bootstrap(home, log)
    orch = Orchestrator(load_project(_catalog_file(home)), log=log)
    for branch in BRANCHES:
        failure = _unhealthy(branch, orch.up(branch, uses=USES))
        if failure:
            raise failure
    return DemoResult(
        tuple(_demo_branch(orch, branch) for branch in BRANCHES), home, not tls.available()
    )


def up(log: Log = print, port_in_use: PortInUse = doctor.port_in_use) -> DemoResult:
    """Start the demo (or find it running) and return where to open it.

    `log` receives the same progress lines `wt up` prints. A failure after the first thing was
    created ends with a hint to run `wt demo down`.
    """
    with demo_environment():
        _require_docker()
        _require_free_ports(port_in_use)
        try:
            return _start(demo_home(), log)
        except WtError as exc:
            raise WtError(f"{exc}\n{CLEANUP_HINT}") from exc


def _remove_branches(home: Path, log: Log) -> None:
    """`wt remove --yes --force` of each demo branch; a half-created demo may have no catalog."""
    if not _catalog_file(home).is_file():
        return
    orch = Orchestrator(load_project(_catalog_file(home)), log=log)
    for branch in BRANCHES:
        orch.remove(branch, force=True)  # the home is deleted next: nothing to protect


def _remove_shared_objects() -> None:
    """Every Docker object the demo instance owns: its servers, Traefik and the network."""
    for project in (
        postgres.container(),
        redis.container(),
        kafka.container(),
        keycloak.container(),
        router.container(),
    ):
        compose.down(project)
    proc.run(["docker", "network", "rm", compose.network_name()], check=False)


def down(log: Log = print) -> bool:
    """Remove the demo: branches, shared servers, Traefik, network, files. False if none existed."""
    home = demo_home()
    if not home.exists():
        return False
    with demo_environment():
        _remove_branches(home, log)
        _remove_shared_objects()
    _delete_folder(home)
    return True
