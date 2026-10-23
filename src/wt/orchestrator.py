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

from wt import compose, git, health, host, proc, router, tls
from wt import providers as registry
from wt.config import LoadedConfig, Service
from wt.errors import WtError
from wt.naming import Names
from wt.paths import project_dir
from wt.providers.base import BranchContext, Kind, Provider
from wt.state import (
    BranchState,
    ProcessState,
    RepoState,
    StateStore,
    atomic_write,
    file_lock,
    now,
)
from wt.templating import render

Log = Callable[[str], None]
Outcome = Literal["ok", "fail", "skipped"]


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
    docker_ok: bool = True


def _kind(svc: Service) -> Literal["compose", "run"]:
    return "run" if svc.run is not None else "compose"


@dataclass(frozen=True)
class ComposeGroup:
    repo: str
    file: str  # compose file, relative to the repo root
    services: list[str]  # worktree.yaml service names in this group


class Orchestrator:
    def __init__(
        self, loaded: LoadedConfig, log: Log = print, out: Callable[[str], None] | None = None
    ) -> None:
        self.loaded = loaded
        self.cfg = loaded.config
        self.store = StateStore(self.cfg.project)
        self.log = log
        self.out = out

    # ---- naming helpers -------------------------------------------------------------------

    def names(self, branch: str) -> Names:
        return Names(self.cfg.project, branch, self.cfg.domain)

    def worktree_target(self, names: Names, repo: str) -> Path:
        return project_dir(self.cfg.project) / "worktrees" / names.slug / repo

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

    def providers(self) -> list[Provider]:
        return registry.active(self.cfg, self.log)

    def provider_context(self, names: Names) -> BranchContext:
        pg = self.cfg.shared.postgres
        base = pg.base_branch if pg is not None else "main"
        return BranchContext(names, names.branch, base, self.store)

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

    # ---- up -------------------------------------------------------------------------------

    def up(self, branch: str, *, build: bool = False, timeout: int = 120) -> list[ServiceReport]:
        git.check_branch_name(branch)
        names = self.names(branch)
        with self._branch_lock(names):
            # Ports are picked, templates rendered (a typo fails here, before anything is
            # created) and the ports recorded under one lock, so two `up` never pick the same one.
            with file_lock(f"ports-{self.cfg.project}"):
                ports = self._resolve_ports(branch)
                envs, commands = self._render(names, ports)
                for provider in self.providers():
                    provider.preflight(self.provider_context(names))
                with self.store.edit() as state:
                    for other, bs in state.branches.items():
                        if other != branch and bs.slug == names.slug:
                            raise WtError(
                                f"branch {branch!r} and the running branch {other!r} map to "
                                f"the same name {names.slug!r}; run `wt down {other}` first"
                            )
                    state.config_path = str(self.loaded.path)
                    current = state.branches.setdefault(
                        branch, BranchState(branch=branch, slug=names.slug)
                    )
                    current.status = "creating"
                    current.ports = dict(ports)

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

                routes = self._up_compose(
                    branch, names, worktrees, envs, build=build, timeout=timeout
                )

                routes += self._up_run(branch, names, worktrees, commands, envs, ports)

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
                bs.providers[n] = r

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

    def _resolve_ports(self, branch: str) -> dict[str, int]:
        """Ports of `run:` services (D9). A port recorded for this branch is kept."""
        state = self.store.load()
        mine = state.branches.get(branch)
        ports: dict[str, int] = {}
        for name, svc in self.cfg.services.items():
            if svc.run is None or svc.port is None:
                continue
            taken = set(ports.values())
            for other, bs in state.branches.items():
                for n, p in bs.processes.items():
                    if p.port is not None and (other, n) != (branch, name):
                        taken.add(p.port)
                taken |= {p for n, p in bs.ports.items() if (other, n) != (branch, name)}
            recorded = mine.ports.get(name) if mine else None
            process = mine.processes.get(name) if mine else None
            if (
                recorded is not None
                and svc.port in ("auto", recorded)
                and recorded not in taken
                and ((process is not None and host.alive(process)) or host.port_free(recorded))
            ):
                ports[name] = recorded
                continue
            ports[name] = host.pick_port(self.cfg.project, branch, name, taken, svc.port)
        return ports

    def _up_run(
        self,
        branch: str,
        names: Names,
        worktrees: dict[str, Path],
        commands: dict[str, str],
        envs: dict[str, dict[str, str]],
        ports: dict[str, int],
    ) -> list[router.Route]:
        routes: list[router.Route] = []
        for name, command in commands.items():
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

    def _up_compose(
        self,
        branch: str,
        names: Names,
        worktrees: dict[str, Path],
        envs: dict[str, dict[str, str]],
        *,
        build: bool,
        timeout: int,
    ) -> list[router.Route]:
        routes: list[router.Route] = []
        for group in self.compose_groups():
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
                patches[target] = compose.ServicePatch(
                    alias=alias, env=envs[name], network=bool(self.providers())
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
        names = self.names(branch)
        with self._branch_lock(names):
            state = self.store.load()
            for other, other_bs in state.branches.items():
                if other != branch and other_bs.slug == names.slug:
                    raise WtError(
                        f"branch {branch!r} and the running branch {other!r} map to "
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
                self.log(f"branch {branch} not found; nothing to remove")
                return False

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

            self._down_processes(branch, bs)

            projects = {names.compose_project(g.repo) for g in self.compose_groups()}
            projects |= set(bs.compose_projects.values()) if bs else set()
            for project in sorted(projects):
                self.log(f"compose down [bold]{project}[/]")
                compose.down(project)

            router.remove_routes(router.route_file(names))
            if bs and bs.route_file:
                router.remove_routes(Path(bs.route_file))
            tls.remove_cert(
                [*tls.cert_paths(names), *(Path(p) for p in (bs.cert_files if bs else []))]
            )
            self._down_providers(branch, names, bs)
            shutil.rmtree(self.branch_dir(names), ignore_errors=True)

            for source, path in removals:
                self.log(f"removing worktree {path}")
                git.remove_worktree(source, path, force=force)
            slug_dir = self.worktree_target(names, "x").parent
            if slug_dir.is_dir() and not any(slug_dir.iterdir()):
                slug_dir.rmdir()

            with self.store.edit() as state:
                state.branches.pop(branch, None)
            return True

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
            provider = factory(self.cfg, self.log)
            with file_lock(name):
                try:
                    provider.ensure_server()
                except WtError as exc:
                    raise WtError(
                        f"cannot start the {name} server to remove this branch's resources: "
                        f"{exc}. Try `wt doctor --fix`, then run `wt down {branch}` again"
                    ) from exc
                self.log(f"destroying {name} resources")
                provider.destroy(ctx)

            def cleared(b: BranchState, n: str = name) -> None:
                b.providers.pop(n, None)

            self._record(branch, cleared)

    # ---- ls / logs ------------------------------------------------------------------------

    def ls(self, *, probe: bool = True) -> list[BranchStatus]:
        state = self.store.load()

        def status(bs: BranchState) -> BranchStatus:
            names = self.names(bs.branch)
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
                docker_ok=docker_ok,
            )
            for name, svc in self.cfg.services.items():
                url = self.service_url(names, svc)
                if url is None or svc.subdomain is None:
                    continue
                if not probe:
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

    def logs(self, branch: str, service: str, *, follow: bool = True, tail: int = 200) -> None:
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
            args = ["tail", "-n", str(tail), *(["-f"] if follow else []), str(path)]
            proc.run(args, capture=False, check=False)
            return
        compose.logs(
            names.compose_project(svc.repo),
            svc.compose_service or service,
            follow=follow,
            tail=tail,
        )


def _is_worktree_of(repo: Path, path: Path) -> bool:
    """True when git lists `path` as a worktree of `repo`."""
    try:
        trees = git.list_worktrees(repo)
    except WtError:
        return False
    return any(tree.path.resolve() == path.resolve() for tree in trees)


def _env_hash(env: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()


def _same_process(ps: ProcessState, command: str, port: int | None, env_hash: str) -> bool:
    """A live process can be kept: same command, port and env as this `up` would start."""
    same_env = ps.env_hash is None or ps.env_hash == env_hash
    return ps.command == command and ps.port == port and same_env
