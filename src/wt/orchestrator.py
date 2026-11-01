"""Brings a branch up and down. Each step is recorded in state as soon as it exists, so
`down` can clean a half-created branch, and every step is verified before `up` reports success."""

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from wt import compose, git, health, host, naming, proc, router, tls
from wt import providers as registry
from wt.catalog import CatalogService, load_catalog
from wt.config import LoadedConfig, Service, find_cycle
from wt.errors import WtError
from wt.naming import Names
from wt.paths import project_dir
from wt.providers.base import BranchContext, Kind, Provider
from wt.state import (
    BranchState,
    ProcessState,
    RepoState,
    StateStore,
    allocate_index,
    atomic_write,
    file_lock,
    now,
    ports_lock,
    release_index,
)
from wt.templating import render

_REASON_MAX_CHARS = 120
_REASON_ELLIPSIS = "..."
_REASON_HEAD_CHARS = 50
_REASON_TAIL_CHARS = _REASON_MAX_CHARS - _REASON_HEAD_CHARS - len(_REASON_ELLIPSIS)
_NOT_RUNNING_STATUSES = ("stopped", "removing")

Log = Callable[[str], None]
Outcome = Literal["ok", "fail", "skipped"]


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _accumulate(recorded: list[str], created: list[str]) -> list[str]:
    """Recorded names first, then the new ones. A provider that renames its resources between
    versions must not orphan what an older version created: `down` destroys the whole union."""
    return list(dict.fromkeys([*recorded, *created]))


def _short_reason(exc: Exception) -> str:
    """One short line saying why `exc` happened (no command dumps).

    A failed command's first line is only its argv; docker and kcadm put the cause at the end of
    the output, so that line wins when there is one. A long line keeps its start and its end:
    path errors put the cause first (`not a git repository: <path>`), docker puts it last
    (`address already in use`).
    """
    lines = _non_empty_lines(str(exc))
    if isinstance(exc, proc.CommandError):
        lines = _non_empty_lines(exc.result.stderr or exc.result.stdout)[-1:] or lines
    reason = lines[0] if lines else type(exc).__name__
    if len(reason) > _REASON_MAX_CHARS:
        return reason[:_REASON_HEAD_CHARS] + _REASON_ELLIPSIS + reason[-_REASON_TAIL_CHARS:]
    return reason


@dataclass(frozen=True)
class ServiceReport:
    service: str
    outcome: Outcome
    detail: str
    url: str | None = None
    kind: Literal["compose", "run"] = "compose"


@dataclass
class BranchStatus:
    branch: str
    status: str
    running: int
    total: int
    services: list[ServiceReport] = field(default_factory=list)
    worktrees: dict[str, str] = field(default_factory=dict)
    databases: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    docker_ok: bool = True
    # True when wt created none of its worktrees: the repos' own checkout (e.g. main)
    main_checkout: bool = False


def _kind(svc: Service) -> Literal["compose", "run"]:
    return "run" if svc.run is not None else "compose"


@dataclass(frozen=True)
class ComposeGroup:
    repo: str
    file: str  # compose file, relative to the repo root
    services: list[str]  # worktree.yaml service names in this group


@dataclass(frozen=True)
class Step:
    """One unit of the start plan: a compose group (`group`) or a `run:` service (`service`)."""

    name: str
    group: ComposeGroup | None = None
    service: str | None = None


@dataclass(frozen=True)
class RepoRow:
    """One repo of a branch's selected services, as shown in the services view."""

    name: str
    status: Literal["healthy", "unhealthy", "starting", "stopped"]
    port: int | None
    url: str | None


@dataclass(frozen=True)
class Choice:
    """One catalog service in the configure list."""

    service: str
    repo: str
    selected: bool  # in the stack: chosen, or pulled in by a chosen consumer
    required_by: tuple[str, ...] = ()  # the stack services that consume it; () when chosen


class Orchestrator:
    def __init__(
        self, loaded: LoadedConfig, log: Log = print, out: Callable[[str], None] | None = None
    ) -> None:
        self.loaded = loaded
        self.cfg = loaded.config
        self.store = StateStore(self.cfg.project)
        self.log = log
        self.out = out

    def _for(self, branch: str, uses: list[str] | None = None) -> "Orchestrator":
        """This orchestrator, or one built on the branch's selection (§17 D-a, §21).

        The stored selection always wins. `uses` (an explicit choice when creating) only counts
        for a catalog project whose branch has none yet; worktree.yaml projects ignore it.
        """
        bs = self.store.load().branches.get(branch)
        stored = None if bs is None else bs.uses
        if stored is not None:
            if uses is not None and self.loaded.from_catalog:
                self.log(
                    f"branch {branch} already has services {','.join(stored)}; "
                    "keeping them (use configure to change)"
                )
            return self._with_selection(stored)
        if uses is not None and self.loaded.from_catalog:
            return self._with_selection(uses)
        return self

    def _with_selection(self, uses: list[str]) -> "Orchestrator":
        if self.loaded.uses == tuple(dict.fromkeys(uses)):
            return self
        return Orchestrator(self.loaded.with_uses(uses), self.log, self.out)

    def _for_creating(self, branch: str, uses: list[str] | None) -> "Orchestrator":
        """`_for` for `up` and `add`: a catalog branch must come with a selection (§21)."""
        if self.loaded.from_catalog:
            bs = self.store.load().branches.get(branch)
            if bs is None or bs.uses is None:
                if uses is None:
                    raise WtError(
                        f"branch {branch} has no services yet; run wt add {branch} --uses a,b"
                    )
                if not uses:
                    raise WtError("choose at least one service (wt add <b> --uses a,b)")
        return self._for(branch, uses)

    # ---- naming helpers -------------------------------------------------------------------

    def names(self, branch: str) -> Names:
        return Names(self.cfg.project, branch, self.cfg.domain)

    def worktree_target(self, names: Names, repo: str) -> Path:
        """Where the worktree of `repo` lives: the recorded path wins, so a branch never moves."""
        bs = self.store.load().branches.get(names.branch)
        if bs is not None and repo in bs.repos:
            return Path(bs.repos[repo].path)
        return self._slug_dir(names) / repo

    def _slug_dir(self, names: Names) -> Path:
        """The folder that groups the worktrees of one branch (§21 "Worktree location")."""
        root = self.loaded.worktrees_dir or project_dir(self.cfg.project) / "worktrees"
        return root / names.slug

    def branch_dir(self, names: Names) -> Path:
        return project_dir(self.cfg.project) / "branches" / names.slug

    def url(self, host: str) -> str:
        port = router.https_port()
        return f"https://{host}" if port == 443 else f"https://{host}:{port}"

    def service_url(self, names: Names, svc: Service) -> str | None:
        return None if svc.subdomain is None else self.url(names.host(svc.subdomain))

    def compose_groups(self) -> list[ComposeGroup]:
        groups: dict[str, ComposeGroup] = {}
        for name, svc in self.cfg.services.items():
            if svc.compose is None:
                continue
            group = groups.setdefault(svc.repo, ComposeGroup(svc.repo, svc.compose, []))
            group.services.append(name)
        return list(groups.values())

    def start_plan(self) -> tuple[list[Step], dict[str, set[str]]]:
        """Steps in start order, plus the step names each step waits for (from `needs`).

        Without `needs` the order is compose groups, then run services. A cycle across steps
        (e.g. through groups) is a WtError.
        """
        steps = [Step(f"compose {g.repo}", group=g) for g in self.compose_groups()]
        steps += [Step(f"run {n}", service=n) for n, s in self.cfg.services.items() if s.run]
        owner: dict[str, str] = {}
        for step in steps:
            for member in step.group.services if step.group else [step.service or ""]:
                owner[member] = step.name
        deps: dict[str, set[str]] = {step.name: set() for step in steps}
        for name, svc in self.cfg.services.items():
            for need in svc.needs:
                if owner[need] != owner[name]:
                    deps[owner[name]].add(owner[need])
        if cycle := find_cycle({n: sorted(d) for n, d in deps.items()}):
            raise WtError("services need each other in a cycle: " + " -> ".join(cycle))
        ordered: list[Step] = []
        done: set[str] = set()
        pending = list(steps)
        while pending:  # stable: the first pending step whose needs are all started
            step = next(s for s in pending if deps[s.name] <= done)
            pending.remove(step)
            ordered.append(step)
            done.add(step.name)
        return ordered, deps

    def providers(self) -> list[Provider]:
        return registry.active(self.cfg, self.log)

    def provider_context(self, names: Names) -> BranchContext:
        pg = self.cfg.shared.postgres
        base = pg.base_branch if pg is not None else "main"
        urls = tuple(u for s in self.cfg.services.values() if (u := self.service_url(names, s)))
        return BranchContext(names, names.branch, base, self.store, urls)

    def template_context(
        self, names: Names, svc: Service, ports: dict[str, int] | None = None
    ) -> dict[str, Any]:
        """`ports` holds the resolved ports of `run:` services (`port: auto` has no number yet)."""
        ports = ports or {}
        services = {
            n: {
                "url": self.service_url(names, s) or "",
                "port": ports.get(n, s.port),
                "host": names.host(s.subdomain) if s.subdomain is not None else "",
            }
            for n, s in self.cfg.services.items()
        }
        for n, remote in self.loaded.remotes.items():  # catalog services that are not selected
            services[n] = {"url": remote, "port": None, "host": ""}
        context: dict[str, Any] = {
            "project": self.cfg.project,
            "branch": names.branch,
            "slug": names.slug,
            "ident": names.ident,
            "domain": self.cfg.domain,
            "branch_domain": names.branch_domain,
            "port": ports.get(self._name_of(svc), svc.port),
            "url": self.service_url(names, svc) or "",
            "host": names.host(svc.subdomain) if svc.subdomain is not None else "",
            "services": services,
        }
        kind: Kind = "compose" if svc.compose is not None else "run"
        ctx = self.provider_context(names)
        for provider in self.providers():
            context.update(provider.template_vars(ctx, kind))
        return context

    def _name_of(self, svc: Service) -> str:
        return next(n for n, s in self.cfg.services.items() if s is svc)

    def log_path(self, names: Names, service: str) -> Path:
        return self.branch_dir(names) / "logs" / f"{service}.log"

    def _branch_lock(self, names: Names) -> Any:
        return file_lock(f"branch-{self.cfg.project}-{names.slug}")

    def _record(self, branch: str, change: Callable[[BranchState], None]) -> None:
        with self.store.edit() as state:
            bs = state.branches[branch]
            change(bs)
            bs.updated_at = now()

    def _log_warnings(self) -> None:
        """The notes `load_config` produced, e.g. a `remote:` the stack makes irrelevant."""
        for warning in self.loaded.warnings:
            self.log(f"warning: {warning}")

    # ---- up -------------------------------------------------------------------------------

    def up(
        self,
        branch: str,
        *,
        uses: list[str] | None = None,
        build: bool = False,
        timeout: int = 120,
    ) -> list[ServiceReport]:
        git.check_branch_name(branch)
        return self._for_creating(branch, uses)._up(branch, build=build, timeout=timeout)

    def _up(self, branch: str, *, build: bool, timeout: int) -> list[ServiceReport]:
        """`up` on the orchestrator that owns the branch's selection."""
        names = self.names(branch)
        plan, deps = self.start_plan()  # a cycle fails here, before anything is created
        self._log_warnings()
        with self._branch_lock(names):
            self._check_worktree_targets(names)
            self._check_hosts_free(names)
            envs, commands, ports = self._reserve(branch, names)

            try:
                if ignored := ", ".join(
                    n for n in self.cfg.shared.configured() if n not in registry.REGISTRY
                ):
                    self.log(
                        f"[yellow]note:[/] shared providers ({ignored}) are not available "
                        "yet; ignored"
                    )

                with file_lock("traefik"):
                    if not router.is_running():
                        self.log("starting shared Traefik")
                    router.ensure_running(on_line=self.out)

                worktrees = self._up_worktrees(branch, names)
                self._up_providers(branch, names)

                cert = tls.ensure_cert(names)
                if cert is None:
                    self.log(
                        "[yellow]warning:[/] mkcert not found; Traefik serves its default "
                        "self-signed certificate (see `wt doctor`)"
                    )
                else:
                    if not tls.ca_ready():
                        self.log(
                            "[yellow]warning:[/] the mkcert CA is not trusted yet; browsers will "
                            "warn about the certificates; run `wt doctor --fix`"
                        )
                    self._record(
                        branch, lambda bs: setattr(bs, "cert_files", [str(p) for p in cert])
                    )

                routes = self._start_steps(
                    branch,
                    names,
                    plan,
                    deps,
                    worktrees,
                    commands,
                    envs,
                    ports,
                    build=build,
                    timeout=timeout,
                )

                if routes:
                    path = router.write_routes(names, routes, cert)
                    self._ensure_route_visible(path)
                    self._record(branch, lambda bs: setattr(bs, "route_file", str(path)))
                else:
                    self._drop_routes(branch, names)

                reports = self._verify(branch, names, timeout=timeout)
                failed = any(r.outcome == "fail" for r in reports)
                self._record(
                    branch, lambda bs: setattr(bs, "status", "degraded" if failed else "up")
                )
                return reports
            except Exception:
                self._record(branch, lambda bs: setattr(bs, "status", "failed"))
                raise

    def _check_worktree_targets(self, names: Names) -> None:
        """Fail `up` and `add` before anything is created when a target folder is not ours.

        A folder that already holds something other than a worktree of the repo (a file, or a
        non-empty folder) is never touched. A branch that has its worktree recorded is exempt.
        """
        recorded = self.store.load().branches.get(names.branch)
        for repo in self.cfg.repos:
            if recorded is not None and repo in recorded.repos:
                continue
            target = self.worktree_target(names, repo)
            if _holds_foreign_content(target) and not _is_worktree_of(
                self.loaded.repo_path(repo), target
            ):
                raise WtError(
                    f"{target} already exists and was not created by wt; move it, or choose "
                    "another name or worktrees folder"
                )

    def _check_hosts_free(self, names: Names) -> None:
        """Fail `up` before anything is created when another route file already routes a host.

        The branch's own route file is ignored, so a re-run or a `stopped` branch never conflicts
        with itself. Two `up` at the very same moment can still race: this is a safeguard.
        """
        in_use = router.hosts_in_use(exclude=router.route_file(names))
        for svc in self.cfg.services.values():
            if svc.subdomain is None:
                continue
            route_host = names.host(svc.subdomain)
            if (path := in_use.get(route_host.lower())) is not None:
                raise WtError(
                    f"host {route_host} is already routed by {router.route_file_owner(path)}; "
                    "give one of the projects another domain"
                )

    def _reserve(
        self, branch: str, names: Names
    ) -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, int]]:
        """Pre-flight for `up` and `add`; the caller holds the branch lock.

        Ports are picked, templates rendered (a typo fails here, before anything is created)
        and the ports recorded under one lock, so two callers never pick the same one.
        """
        with ports_lock():
            index = allocate_index(self.cfg.project, branch)
            try:
                ports = self._resolve_ports(branch, index)
                envs, commands = self._render(
                    names, {n: p for n, p in ports.items() if self.cfg.services[n].run is not None}
                )
                for provider in self.providers():
                    provider.preflight(self.provider_context(names))
                with self.store.edit() as state:
                    for other, bs in state.branches.items():
                        if other != branch and bs.slug == names.slug:
                            raise WtError(
                                f"branch {branch!r} and the existing branch {other!r} map to "
                                f"the same name {names.slug!r}; run `wt down {other}` first"
                            )
                    state.config_path = str(self.loaded.path)
                    current = state.branches.setdefault(
                        branch, BranchState(branch=branch, slug=names.slug)
                    )
                    if self.loaded.from_catalog and current.uses is None:
                        # The explicit choice, not the closure: later operations rebuild the
                        # same stack from it through `_for` (like `configure`).
                        current.uses = list(self.loaded.uses or ())
                    current.status = "creating"
                    current.index = index
                    current.ports = dict(ports)
            except Exception:
                if branch not in self.store.load().branches:
                    release_index(self.cfg.project, branch)
                raise
        return envs, commands, ports

    def add(self, branch: str, uses: list[str] | None = None) -> bool:
        """Worktrees and reserved ports only, status `stopped`. False when already known."""
        git.check_branch_name(branch)
        return self._for_creating(branch, uses)._add(branch)

    def _add(self, branch: str) -> bool:
        """`add` on the orchestrator that owns the branch's selection."""
        names = self.names(branch)
        self.start_plan()  # a cycle fails here, before anything is created
        self._log_warnings()
        with self._branch_lock(names):
            if branch in self.store.load().branches:
                self.log(f"branch {branch} is already known; nothing to add")
                return False
            self._check_worktree_targets(names)
            self._reserve(branch, names)
            try:
                self._up_worktrees(branch, names)
            except Exception:
                self._record(branch, lambda bs: setattr(bs, "status", "failed"))
                raise
            self._record(branch, lambda bs: setattr(bs, "status", "stopped"))
            return True

    def stop(self, branch: str) -> None:
        """Stop processes and compose projects (no volumes) and drop the route; keep the rest."""
        if (other := self._for(branch)) is not self:
            return other.stop(branch)
        names = self.names(branch)
        with self._branch_lock(names):
            bs = self.store.load().branches.get(branch)
            if bs is None:
                raise WtError(f"branch {branch!r} not found (see `wt ls`)")
            self._stop_services(branch, names, bs, volumes=False)

            def stopped(b: BranchState) -> None:
                b.status = "stopped"
                b.route_file = None

            self._record(branch, stopped)

    def _stop_services(
        self, branch: str, names: Names, bs: BranchState | None, *, volumes: bool
    ) -> None:
        """Processes, containers and routes. `volumes=False` (stop) keeps the compose volumes."""
        self._down_processes(branch, bs)
        projects = {names.compose_project(g.repo) for g in self.compose_groups()}
        projects |= set(bs.compose_projects.values()) if bs else set()
        for project in sorted(projects):
            self.log(f"compose down [bold]{project}[/]")
            if volumes:
                compose.down(project)
            else:
                compose.down_keep_volumes(project)
        router.remove_routes(router.route_file(names))
        if bs and bs.route_file:
            router.remove_routes(Path(bs.route_file))

    def _up_providers(self, branch: str, names: Names) -> None:
        ctx = self.provider_context(names)
        for provider in self.providers():
            self.log(f"provider [bold]{provider.name}[/]")
            # One server, shared by all branches: serialize ensure/create (incl. the clone).
            with file_lock(provider.name):
                provider.ensure_server()
                # Name first, resources after: `down` then cleans a create that died halfway.

                def started(bs: BranchState, n: str = provider.name) -> None:
                    bs.providers.setdefault(n, [])

                self._record(branch, started)
                created = provider.create(ctx)

            def finished(
                bs: BranchState, n: str = provider.name, r: list[str] = created.names
            ) -> None:
                bs.providers[n] = _accumulate(bs.providers.get(n, []), r)

            self._record(branch, finished)
            check = provider.verify(ctx)
            for warning in check.warnings:
                self.log(f"[yellow]warning:[/] {provider.name}: {warning}")
            if not check.ok:
                raise WtError(f"{provider.name}: {check.detail}")

    def _render(
        self, names: Names, ports: dict[str, int]
    ) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
        contexts = {
            name: self.template_context(names, svc, ports)
            for name, svc in self.cfg.services.items()
        }
        envs = {
            name: {
                k: render(v, contexts[name], f"services.{name}.env.{k}") for k, v in svc.env.items()
            }
            for name, svc in self.cfg.services.items()
        }
        commands = {
            name: render(svc.run, contexts[name], f"services.{name}.run", quote=True)
            for name, svc in self.cfg.services.items()
            if svc.run is not None
        }
        return envs, commands

    def _resolve_ports(self, branch: str, index: int) -> dict[str, int]:
        """Host ports (§17): `port: auto` run services and compose services with a `port` get
        `host_port(index, service_index)`; an explicit run port is used as is. No probing: a
        port that is taken fails."""
        state = self.store.load()
        mine = state.branches.get(branch)
        ports: dict[str, int] = {}
        for name, svc in self.cfg.services.items():
            if svc.port is None or (svc.run is None and svc.compose is None):
                continue
            taken = set(ports.values())
            for other, bs in state.branches.items():
                for n, p in bs.processes.items():
                    if p.port is not None and (other, n) != (branch, name):
                        taken.add(p.port)
                taken |= {p for n, p in bs.ports.items() if (other, n) != (branch, name)}
            process = mine.processes.get(name) if mine else None
            if svc.run is not None and svc.port != "auto":
                own_live = (
                    process is not None and process.port == svc.port and host.alive(process)
                )  # our own process already listens there (SPEC §14 "Ports")
                ports[name] = svc.port if own_live else host.pick_port(name, taken, svc.port)
                continue
            port = naming.host_port(index, self.loaded.service_index(name))
            ours = _owns_port(mine, name, port, svc, process)
            if port in taken or not (ours or host.port_free(port)):
                raise WtError(
                    f"port {port} for service '{name}' (worktree index {index}) is in use; free it"
                )
            ports[name] = port
        return ports

    def _start_steps(  # noqa: PLR0913
        self,
        branch: str,
        names: Names,
        plan: list[Step],
        deps: dict[str, set[str]],
        worktrees: dict[str, Path],
        commands: dict[str, str],
        envs: dict[str, dict[str, str]],
        ports: dict[str, int],
        *,
        build: bool,
        timeout: int,
    ) -> list[router.Route]:
        """Start the steps in order; a step that others need must be healthy first."""
        consumed = set().union(*deps.values())
        routes: list[router.Route] = []
        for step in plan:
            if step.group is not None:  # healthy once `compose up --wait` returned
                routes += self._up_compose_group(
                    branch, names, step.group, worktrees, envs, ports, build=build, timeout=timeout
                )
            elif step.service is not None:
                name = step.service
                routes += self._up_run_service(
                    branch, names, name, commands[name], worktrees, envs, ports
                )
                if step.name in consumed:
                    ps = self.store.load().branches[branch].processes[name]
                    report = self._check_process(name, ps, timeout)
                    if report.outcome != "ok":
                        raise WtError(f"service {name!r} is not healthy: {report.detail}")
        return routes

    def _up_run_service(  # noqa: PLR0913
        self,
        branch: str,
        names: Names,
        name: str,
        command: str,
        worktrees: dict[str, Path],
        envs: dict[str, dict[str, str]],
        ports: dict[str, int],
    ) -> list[router.Route]:
        routes: list[router.Route] = []
        svc = self.cfg.services[name]
        recorded = self.store.load().branches[branch].processes.get(name)
        env_hash = _env_hash(envs[name])
        live = recorded is not None and host.alive(recorded)
        if (
            live
            and recorded is not None
            and _same_process(recorded, command, ports.get(name), env_hash)
        ):
            self.log(f"process [bold]{name}[/] already running (pid {recorded.pid})")
        else:
            if live and recorded is not None:
                self.log(f"restarting process [bold]{name}[/] (command, port or env changed)")
                host.stop(recorded)
            else:
                self.log(f"starting process [bold]{name}[/]")
            ps = host.start(
                command,
                worktrees[svc.repo],
                {**os.environ, **envs[name]},
                self.log_path(names, name),
                ports.get(name),
            ).model_copy(update={"env_hash": env_hash})

            def record(bs: BranchState, n: str = name, ps: ProcessState = ps) -> None:
                bs.processes[n] = ps

            self._record(branch, record)
        if svc.subdomain is not None and name in ports:
            routes.append(
                router.Route(name, names.host(svc.subdomain), router.host_upstream(ports[name]))
            )
        return routes

    def _ensure_route_visible(self, route: Path) -> None:
        """Recreate Traefik once if it cannot see the route file (stale bind mount)."""
        with file_lock("traefik"):
            if route.name not in router.missing_in_container():
                return
            self.log("[yellow]warning:[/] Traefik does not see the route file; recreating it")
            router.restart()
            if route.name in router.missing_in_container():
                raise WtError(
                    f"Traefik still does not see the route file {route.name} after a "
                    "recreate; run `wt doctor --fix`"
                )

    def _drop_routes(self, branch: str, names: Names) -> None:
        recorded = self.store.load().branches[branch].route_file
        router.remove_routes(router.route_file(names))
        if recorded:
            router.remove_routes(Path(recorded))
        self._record(branch, lambda bs: setattr(bs, "route_file", None))

    def _up_worktrees(self, branch: str, names: Names) -> dict[str, Path]:
        worktrees: dict[str, Path] = {}
        for repo in self.cfg.repos:
            source = self.loaded.repo_path(repo)
            key = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:12]
            with file_lock(f"repo-{key}"):
                res = git.ensure_worktree(source, branch, self.worktree_target(names, repo))
            worktrees[repo] = res.path
            if res.branch_created:
                self.log(f"created branch {branch!r} in repo {repo!r} from its current HEAD")
            if not res.created:
                self.log(
                    f"repo {repo!r}: {branch!r} is already checked out at {res.path}; using it"
                )

            def change(
                bs: BranchState,
                repo: str = repo,
                res: git.EnsureResult = res,
                source: Path = source,
            ) -> None:
                before = bs.repos.get(repo)
                bs.repos[repo] = RepoState(
                    source=str(source),
                    path=str(res.path),
                    created=res.created,
                    branch_created=res.branch_created or bool(before and before.branch_created),
                )

            self._record(branch, change)
        return worktrees

    def _up_compose_group(  # noqa: PLR0913
        self,
        branch: str,
        names: Names,
        group: ComposeGroup,
        worktrees: dict[str, Path],
        envs: dict[str, dict[str, str]],
        ports: dict[str, int],
        *,
        build: bool,
        timeout: int,
    ) -> list[router.Route]:
        routes: list[router.Route] = []
        compose_file = worktrees[group.repo] / group.file
        if not compose_file.is_file():
            raise WtError(f"compose file not found: {compose_file}")
        available = compose.list_services(compose_file)
        patches: dict[str, compose.ServicePatch] = {}
        for name in group.services:
            svc = self.cfg.services[name]
            target = svc.compose_service or name
            if target not in available:
                raise WtError(
                    f"service {name!r}: {group.file} has no service {target!r} "
                    f"(found: {', '.join(available)}); set 'compose_service'"
                )
            alias = names.network_alias(name) if svc.subdomain is not None else None
            publish = (
                (ports[name], svc.port) if isinstance(svc.port, int) and name in ports else None
            )
            patches[target] = compose.ServicePatch(
                alias=alias, env=envs[name], network=bool(self.providers()), publish=publish
            )
            if alias and svc.subdomain is not None:
                routes.append(
                    router.Route(name, names.host(svc.subdomain), f"http://{alias}:{svc.port}")
                )
        override = self.branch_dir(names) / f"{group.repo}.override.yml"
        atomic_write(override, compose.dump(compose.build_override(available, patches)))
        project = names.compose_project(group.repo)

        def record(bs: BranchState, repo: str = group.repo, project: str = project) -> None:
            bs.compose_projects[repo] = project

        self._record(branch, record)
        self.log(f"compose up [bold]{project}[/]")
        compose.up(
            project,
            [compose_file, override],
            cwd=compose_file.parent,
            build=build,
            timeout=timeout,
            on_line=self.out,
        )
        return routes

    def _verify(self, branch: str, names: Names, *, timeout: float) -> list[ServiceReport]:
        self.log("verifying services")
        processes = self.store.load().branches[branch].processes
        containers = {
            group.repo: compose.ps(names.compose_project(group.repo))
            for group in self.compose_groups()
        }

        def check(item: tuple[str, Service]) -> ServiceReport:
            name, svc = item
            url = self.service_url(names, svc)
            if svc.subdomain is not None:
                result = health.wait_healthy(
                    names.host(svc.subdomain), svc.health, router.https_port(), timeout
                )
                return ServiceReport(
                    name,
                    "ok" if result.ok else "fail",
                    f"GET {svc.health} -> {result.detail}",
                    url,
                    _kind(svc),
                )
            if svc.run is not None:
                return self._check_process(name, processes.get(name), timeout)
            target = svc.compose_service or name
            states = [c for c in containers.get(svc.repo, []) if c.service == target]
            ok = bool(states) and all(
                c.state == "running" and c.health != "unhealthy" for c in states
            )
            detail = ", ".join(f"{c.state}{'/' + c.health if c.health else ''}" for c in states)
            return ServiceReport(name, "ok" if ok else "fail", detail or "no container")

        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(check, self.cfg.services.items()))

    def _check_process(self, name: str, ps: ProcessState | None, timeout: float) -> ServiceReport:
        """Unrouted host process: TCP connect when it has a port, otherwise just alive."""
        if ps is None:
            return ServiceReport(name, "fail", "not started", kind="run")
        deadline = time.monotonic() + timeout
        while ps.port is not None:
            if not host.alive(ps):
                break
            if host.tcp_ok(ps.port):
                return ServiceReport(name, "ok", f"port {ps.port} accepts connections", kind="run")
            if time.monotonic() >= deadline:
                return ServiceReport(name, "fail", f"nothing listens on port {ps.port}", kind="run")
            time.sleep(0.2)
        if host.alive(ps):
            return ServiceReport(name, "ok", f"running (pid {ps.pid})", kind="run")
        return ServiceReport(name, "fail", f"exited; see {ps.log}", kind="run")

    # ---- down -----------------------------------------------------------------------------

    def down(self, branch: str, *, force: bool = False) -> bool:
        """Remove everything for a branch. False when there was nothing to remove."""
        if (other := self._for(branch)) is not self:
            return other.down(branch, force=force)
        with self._branch_lock(self.names(branch)):
            if self._down_locked(branch, force=force) is None:
                self._log_nothing_to_remove(branch)
                return False
            return True

    def remove(self, branch: str, *, force: bool = False) -> bool:
        """`down`, then delete the git branch in every repo, except protected ones.

        Also works after `down` (the branch is no longer in state): the branch is then looked
        up in every repo of the project. True when state or a git branch was removed.
        """
        if (other := self._for(branch)) is not self:
            return other.remove(branch, force=force)
        with self._branch_lock(self.names(branch)):
            sources = self._down_locked(branch, force=force)
            known = sources is not None
            if sources is None:
                sources = {r: self.loaded.repo_path(r) for r in self.cfg.repos}
            found, deleted = self._delete_branch_in_repos(branch, sources)
            if not known and not found:
                self._log_nothing_to_remove(branch)
            return known or deleted > 0

    def _log_nothing_to_remove(self, branch: str) -> None:
        self.log(f"branch {branch} not found; nothing to remove")

    def _delete_branch_in_repos(self, branch: str, sources: dict[str, Path]) -> tuple[int, int]:
        """Delete `branch` where it exists unless protected; returns (repos having it, deleted).

        A repo that fails (missing folder, not a git repo, refused delete) is logged and does not
        stop the others; once all were tried, one error lists the failed repos.
        """
        found = deleted = 0
        failures: dict[str, str] = {}
        for repo, source in sources.items():
            try:
                outcome = self._delete_branch_in_repo(branch, repo, source)
            except WtError as exc:
                failures[repo] = _short_reason(exc)
                self.log(
                    f"[red]repo {repo!r}: could not delete branch {branch!r}:[/] {failures[repo]}"
                )
                continue
            found += outcome != "absent"
            deleted += outcome == "deleted"
        if failures:
            listed = "; ".join(f"{repo!r} ({reason})" for repo, reason in failures.items())
            raise WtError(
                f"could not delete branch {branch!r} in {len(failures)} repo(s): {listed}"
            )
        return found, deleted

    def _delete_branch_in_repo(
        self, branch: str, repo: str, source: Path
    ) -> Literal["absent", "kept", "deleted"]:
        git.check_repo(source)
        if not git.branch_exists(source, branch):
            return "absent"
        pg = self.cfg.shared.postgres
        if branch == (pg.base_branch if pg is not None else "main"):
            self.log(f"keeping branch {branch!r} in repo {repo!r}: Postgres base branch")
        elif any(t.branch == branch for t in git.list_worktrees(source)):
            self.log(f"keeping branch {branch!r} in repo {repo!r}: checked out elsewhere")
        else:
            self.log(f"deleting branch {branch!r} in repo {repo!r}")
            git.delete_branch(source, branch)
            return "deleted"
        return "kept"

    def _down_locked(self, branch: str, *, force: bool) -> dict[str, Path] | None:  # noqa: C901, PLR0912
        """The body of `down`; None when there was nothing to remove, else repo -> main folder."""
        names = self.names(branch)
        state = self.store.load()
        for other, other_bs in state.branches.items():
            if other != branch and other_bs.slug == names.slug:
                raise WtError(
                    f"branch {branch!r} and the existing branch {other!r} map to "
                    f"the same name {names.slug!r}; run `wt down {other}` instead"
                )
        bs = state.branches.get(branch)

        # (main repo, worktree) pairs that wt created and must remove
        removals: list[tuple[Path, Path]] = []
        for repo in sorted(set(self.cfg.repos) | set(bs.repos if bs else [])):
            recorded = bs.repos.get(repo) if bs else None
            if recorded is not None:
                if recorded.created:
                    removals.append((Path(recorded.source), Path(recorded.path)))
            elif repo in self.cfg.repos and self.worktree_target(names, repo).exists():
                source, target = self.loaded.repo_path(repo), self.worktree_target(names, repo)
                if _is_worktree_of(source, target):
                    removals.append((source, target))
                else:
                    self.log(
                        f"[yellow]warning:[/] {target} is not a worktree of repo {repo!r}; "
                        "left in place, remove it by hand"
                    )

        leftovers = [router.route_file(names), *tls.cert_paths(names)]
        if (
            bs is None
            and not removals
            and not self.branch_dir(names).exists()
            and not any(p.exists() for p in leftovers)
        ):
            return None

        if not force:
            dirty = [str(p) for _, p in removals if p.exists() and git.is_dirty(p)]
            if dirty:
                raise WtError(
                    "uncommitted or untracked changes in:\n  "
                    + "\n  ".join(dirty)
                    + "\ncommit or stash them, or run `wt down --force` "
                    "(the branch and its commits are always kept)"
                )

        if bs:
            self._record(branch, lambda b: setattr(b, "status", "removing"))

        self._stop_services(branch, names, bs, volumes=True)
        tls.remove_cert([*tls.cert_paths(names), *(Path(p) for p in (bs.cert_files if bs else []))])
        self._down_providers(branch, names, bs)
        shutil.rmtree(self.branch_dir(names), ignore_errors=True)

        for source, path in removals:
            self.log(f"removing worktree {path}")
            git.remove_worktree(source, path, force=force)
        for slug_dir in {self._slug_dir(names), *(path.parent for _, path in removals)}:
            _remove_if_empty(slug_dir)

        with ports_lock():
            with self.store.edit() as state:
                state.branches.pop(branch, None)
            release_index(self.cfg.project, branch)
        sources = {r: self.loaded.repo_path(r) for r in self.cfg.repos}
        sources.update({r: Path(s.source) for r, s in (bs.repos if bs else {}).items()})
        return sources

    def _down_processes(self, branch: str, bs: BranchState | None) -> None:
        for name, ps in (bs.processes if bs else {}).items():
            self.log(f"stopping process [bold]{name}[/]")
            host.stop(ps)

            def cleared(b: BranchState, n: str = name) -> None:
                b.processes.pop(n, None)

            self._record(branch, cleared)

    def _down_providers(self, branch: str, names: Names, bs: BranchState | None) -> None:
        if bs is None or not bs.providers:
            return
        ctx = self.provider_context(names)
        for name in list(bs.providers):
            factory = registry.REGISTRY.get(name)
            if factory is None:
                leftover = ", ".join(bs.providers[name]) or "none recorded"
                self.log(
                    f"[yellow]warning:[/] provider {name!r} is not available; remove its "
                    f"resources by hand: {leftover}"
                )
                continue
            self._destroy_provider(branch, name, factory(self.cfg, self.log), ctx)

            def cleared(b: BranchState, n: str = name) -> None:
                b.providers.pop(n, None)

            self._record(branch, cleared)

    def _destroy_provider(
        self, branch: str, name: str, provider: Provider, ctx: BranchContext
    ) -> None:
        """The single place where a failed provider step of `down` becomes the user-facing error."""
        # OSError is caught too: the lock directory and the provider's own file writes (disk full,
        # permissions) fail without going through `proc.run`, so they never become a WtError.
        try:
            with file_lock(name):
                provider.ensure_server()
                self.log(f"destroying {name} resources")
                provider.destroy(ctx)
        except (WtError, OSError) as exc:
            raise WtError(
                f"{name}: could not clean branch {branch} ({_short_reason(exc)}). "
                f"Try `wt doctor --fix`, then `wt down {branch}` again."
            ) from exc

    # ---- ls / logs ------------------------------------------------------------------------

    def ls(self, *, probe: bool = True) -> list[BranchStatus]:
        state = self.store.load()

        def status(bs: BranchState) -> BranchStatus:
            try:
                me = self._for(bs.branch)
            except WtError:  # a stored selection the catalog no longer knows
                me = self
            names = me.names(bs.branch)
            containers: list[compose.Container] = []
            docker_ok = True
            try:
                containers = [c for p in bs.compose_projects.values() for c in compose.ps(p)]
            except WtError:
                docker_ok = False
            result = BranchStatus(
                branch=bs.branch,
                status=bs.status,
                running=sum(c.state == "running" for c in containers)
                + sum(host.alive(ps) for ps in bs.processes.values()),
                total=len(containers) + len(bs.processes),
                worktrees={r: s.path for r, s in bs.repos.items()},
                databases=list(bs.providers.get("postgres", [])),
                ports=sorted(bs.ports.values()),
                docker_ok=docker_ok,
                main_checkout=bool(bs.repos) and not any(r.created for r in bs.repos.values()),
            )
            for name, svc in me.cfg.services.items():
                url = me.service_url(names, svc)
                if url is None or svc.subdomain is None:
                    continue
                # a stopped branch has nothing listening: probing would only print noise
                if not probe or bs.status == "stopped":
                    result.services.append(ServiceReport(name, "skipped", "", url, _kind(svc)))
                    continue
                check = health.probe(
                    names.host(svc.subdomain), svc.health, router.https_port(), timeout=2.0
                )
                result.services.append(
                    ServiceReport(name, "ok" if check.ok else "fail", check.detail, url, _kind(svc))
                )
            if docker_ok and result.status in ("up", "degraded"):
                healthy = result.running >= result.total and not any(
                    s.outcome == "fail" for s in result.services
                )
                result.status = "up" if healthy else "degraded"
            return result

        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(status, state.branches.values()))

    def log_command(self, branch: str, service: str, *, follow: bool, tail: int) -> list[str]:
        """Command that prints (and optionally follows) a service's log."""
        if (other := self._for(branch)) is not self:  # the service may only be in its stack
            return other.log_command(branch, service, follow=follow, tail=tail)
        svc = self.cfg.services.get(service)
        if svc is None:
            raise WtError(f"unknown service {service!r} (known: {', '.join(self.cfg.services)})")
        if branch not in self.store.load().branches:
            raise WtError(f"branch {branch!r} is not up (see `wt ls`)")
        names = self.names(branch)
        if svc.run is not None:
            path = self.log_path(names, service)
            if not path.is_file():
                raise WtError(f"no log yet for {service} on {branch}")
            return ["tail", "-n", str(tail), *(["-f"] if follow else []), str(path)]
        return compose.logs_command(
            names.compose_project(svc.repo),
            svc.compose_service or service,
            follow=follow,
            tail=tail,
        )

    def logs(self, branch: str, service: str, *, follow: bool = True, tail: int = 200) -> None:
        args = self.log_command(branch, service, follow=follow, tail=tail)
        proc.run(args, capture=False, check=False)

    def services(self, branch: str) -> list[RepoRow]:
        """One row per repo of the branch's selected services (§17 Contract 6).

        Cheap on purpose (the TUI calls it every 2 s): one `compose ps` per project, a TCP
        connect per run process with a port, no HTTP probes.
        """
        me = self._for(branch)
        bs = self.store.load().branches.get(branch)
        names = me.names(branch)
        repos: dict[str, list[str]] = {}
        for name, svc in me.cfg.services.items():
            repos.setdefault(svc.repo, []).append(name)
        rows: list[RepoRow] = []
        for repo, members in repos.items():
            shown = [(n, me.cfg.services[n]) for n in members]
            pick = next((n for n, s in shown if s.subdomain is not None), None)
            routed = pick is not None
            if pick is None:
                pick = next((n for n, s in shown if s.port is not None), None)
            port = bs.ports.get(pick) if bs is not None and pick is not None else None
            url = me.service_url(names, me.cfg.services[pick]) if routed and pick else None
            status = me._repo_status(bs, repo, [n for n, s in shown if s.run is not None])
            rows.append(RepoRow(repo, status, port, url))
        return rows

    def _repo_status(
        self, bs: BranchState | None, repo: str, run_services: list[str]
    ) -> Literal["healthy", "unhealthy", "starting", "stopped"]:
        if bs is None or bs.status == "stopped":
            return "stopped"
        containers: list[compose.Container] = []
        project = bs.compose_projects.get(repo)
        docker_ok = True
        if project is not None:
            try:
                containers = compose.ps(project)
            except WtError:
                docker_ok = False
        procs = [bs.processes.get(n) for n in run_services]
        alive = [p is not None and host.alive(p) for p in procs]
        busy = ("running", "created", "restarting")
        if docker_ok and not any(alive) and not any(c.state in busy for c in containers):
            return "stopped"
        if bs.status == "creating" or any(
            c.health == "starting" or c.state in ("created", "restarting") for c in containers
        ):
            return "starting"
        listening = [
            p is not None and a and (p.port is None or host.tcp_ok(p.port, timeout=0.3))
            for p, a in zip(procs, alive, strict=True)
        ]
        if (
            not docker_ok
            or any(c.state != "running" or c.health == "unhealthy" for c in containers)
            or not all(listening)
        ):
            return "unhealthy"
        return "healthy"

    def catalog_choices(self, branch: str | None = None) -> list[Choice] | None:
        """Every catalog service with its repo and its place in the branch's stack (§20).

        Without a branch (one that is about to be created) nothing is selected and `required_by`
        lists each service's direct consumers in the whole catalog.
        """
        if self.loaded.uses is None or self.loaded.catalog is None:
            return None
        if branch is None:
            catalog = load_catalog(self.loaded.catalog).catalog
            return [
                Choice(name, svc.repo, False, _consumers(catalog.services, name))
                for name, svc in catalog.services.items()
            ]
        if self.store.load().branches.get(branch) is None:
            raise WtError(f"branch {branch!r} not found (see `wt ls`)")
        mine = self._for(branch).loaded
        catalog = load_catalog(self.loaded.catalog).catalog
        stack = list(mine.config.services)
        explicit = set(mine.uses or ())

        def required_by(name: str) -> tuple[str, ...]:
            if name in explicit or name not in stack:
                return ()
            return tuple(s for s in stack if name in catalog.services[s].consumes)

        return [
            Choice(name, svc.repo, name in stack, required_by(name))
            for name, svc in catalog.services.items()
        ]

    def adopt_branches(self, uses: list[str]) -> list[str]:
        """Give every branch of this project that has no selection `uses` (`wt migrate`, §21).

        Returns the adopted branches, sorted. Branches that already have a selection are left alone.
        """
        self.loaded.with_uses(uses)  # validates the names against the catalog
        with self.store.edit() as state:
            adopted = sorted(b for b, bs in state.branches.items() if bs.uses is None)
            for branch in adopted:
                state.branches[branch].uses = list(uses)
                state.branches[branch].updated_at = now()
        return adopted

    def configure(self, branch: str, uses: list[str]) -> None:
        """Change the services of a branch: store the selection, tear down what left, `up`.

        What leaves and what arrives is the difference between the old and the new stack (the
        selection plus the services it consumes), so a service a selected consumer still needs
        stays where it is.
        """
        if self.store.load().branches.get(branch) is None:
            raise WtError(f"branch {branch!r} not found (see `wt ls`)")
        old = self._for(branch)
        wanted = list(dict.fromkeys(uses))
        new = Orchestrator(self.loaded.with_uses(wanted), self.log, self.out)
        names = self.names(branch)
        added = [n for n in new.cfg.services if n not in old.cfg.services]
        removed = [n for n in old.cfg.services if n not in new.cfg.services]
        with self._branch_lock(names):
            bs = self.store.load().branches[branch]
            self._record(branch, lambda b: setattr(b, "uses", wanted))
            self._drop_services(branch, names, bs, old, new, removed)
            status = bs.status
        # A stopped branch stays stopped unless something new arrives; a still-healthy one with an
        # unchanged stack only had its stored selection changed. Anything else is retried/healed.
        unchanged = not added and not removed
        if added or (status != "stopped" and not (unchanged and status == "up")):
            new.up(branch)
        else:
            new._log_warnings()  # `up` says them otherwise

    def _drop_services(  # noqa: C901, PLR0913
        self,
        branch: str,
        names: Names,
        bs: BranchState,
        old: "Orchestrator",
        new: "Orchestrator",
        removed: list[str],
    ) -> None:
        """Kill the processes and remove the containers of services that left the selection."""
        kept_repos = {s.repo for s in new.cfg.services.values()}
        compose_by_repo: dict[str, list[str]] = {}
        for name in removed:
            svc = old.cfg.services[name]
            if svc.run is not None:
                if (ps := bs.processes.get(name)) is not None:
                    self.log(f"stopping process [bold]{name}[/]")
                    host.stop(ps)
            else:
                compose_by_repo.setdefault(svc.repo, []).append(name)
        for repo, services in compose_by_repo.items():
            project = bs.compose_projects.get(repo) or names.compose_project(repo)
            if repo in kept_repos:
                for name in services:
                    target = old.cfg.services[name].compose_service or name
                    self.log(f"removing compose service [bold]{project}/{target}[/]")
                    compose.remove_service(project, target)
            else:
                self.log(f"compose down [bold]{project}[/] (volumes kept)")
                compose.down_keep_volumes(project)

        def cleared(b: BranchState) -> None:
            for name in removed:
                b.processes.pop(name, None)
                b.ports.pop(name, None)
            for repo in compose_by_repo:
                if repo not in kept_repos:
                    b.compose_projects.pop(repo, None)

        self._record(branch, cleared)


def _consumers(services: dict[str, CatalogService], name: str) -> tuple[str, ...]:
    """The catalog services that consume `name` directly, in catalog order."""
    return tuple(other for other, svc in services.items() if name in svc.consumes)


def _is_worktree_of(repo: Path, path: Path) -> bool:
    """True when git lists `path` as a worktree of `repo`."""
    try:
        trees = git.list_worktrees(repo)
    except WtError:
        return False
    return any(tree.path.resolve() == path.resolve() for tree in trees)


def _holds_foreign_content(path: Path) -> bool:
    """True when `path` is a file or a folder with something in it (an empty folder is fine)."""
    return path.exists() and (not path.is_dir() or any(path.iterdir()))


def _remove_if_empty(folder: Path) -> None:
    """Never `rmtree`: a folder that holds anything is somebody's and stays."""
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()


def _env_hash(env: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()


def _owns_port(
    mine: BranchState | None, name: str, port: int, svc: Service, process: ProcessState | None
) -> bool:
    """Is `port` held by this branch's own service, so a busy socket there is not a stranger?

    A run service owns it only while its recorded process is alive: the branch status goes stale
    when the process dies, and a stranger may have taken the port since. A compose service's
    container publishes the port and there is no host process to check, so the branch status is the
    evidence: an `up` interrupted after compose started leaves `creating` or `failed` with the
    containers running, and the next `up` must reuse them."""
    if mine is None or mine.ports.get(name) != port:
        return False
    if svc.run is not None:
        return process is not None and host.alive(process)
    return mine.status not in _NOT_RUNNING_STATUSES


def _same_process(ps: ProcessState, command: str, port: int | None, env_hash: str) -> bool:
    """A live process can be kept: same command, port and env as this `up` would start."""
    same_env = ps.env_hash is None or ps.env_hash == env_hash
    return ps.command == command and ps.port == port and same_env
