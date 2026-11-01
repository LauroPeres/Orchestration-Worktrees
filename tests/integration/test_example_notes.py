"""The examples/notes stack end to end against a real Docker daemon: `pytest -m docker`."""

import contextlib
import json
import re
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wt import compose, health, router
from wt.catalog import load_project
from wt.errors import WtError
from wt.orchestrator import Orchestrator
from wt.providers import postgres

from .isolation import reserve_free_indexes

pytestmark = pytest.mark.docker

USES = ["front"]  # the front consumes the back, so the back comes in too
BOOTSTRAP = Path(__file__).parents[2] / "examples" / "notes" / "bootstrap.sh"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def fetch(
    host: str, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, bytes]:
    """HTTPS request through Traefik, reusing the loopback + SNI connection of wt.health."""
    conn = health._LoopbackHTTPS(host, router.https_port(), 10.0)
    try:
        headers = {"Host": host}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def request(
    host: str, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    status, raw = fetch(host, method, path, body)
    return status, json.loads(raw or b"null")


@pytest.fixture
def notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Orchestrator, list[str]]]:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.fail("docker is not available")
    instance = f"wtnotes{free_port()}"
    monkeypatch.setenv("WT_INSTANCE", instance)
    monkeypatch.setenv("WT_HOME", str(tmp_path / "wt-home"))
    reserve_free_indexes(tmp_path / "wt-home")
    monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
    monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
    monkeypatch.setenv("WT_POSTGRES_PORT", str(free_port()))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")  # no mkcert: self-signed path

    stack = tmp_path / "stack"
    subprocess.run([str(BOOTSTRAP), str(stack)], check=True, capture_output=True)
    config = stack / "catalog.yaml"
    # A unique project name keeps compose projects and images away from any real `notes` stack.
    config.write_text(config.read_text().replace("project: notes\n", f"project: {instance}\n"))
    logs: list[str] = []
    orch = Orchestrator(load_project(config), log=logs.append)
    try:
        yield orch, logs
    finally:
        for branch in list(orch.store.load().branches):
            with contextlib.suppress(WtError):
                orch.down(branch, force=True)
        docker("rm", "-f", "-v", router.container(), postgres.container())
        docker("volume", "rm", "-f", postgres.volume())
        docker("network", "rm", compose.network_name())
        # Compose names built images `<compose project>-<service>`, and the project embeds the
        # unique instance name, so this only matches images this test built.
        images = docker("image", "ls", "-q", "--filter", f"reference=*{instance}*").stdout.split()
        if images:
            docker("rmi", "-f", *images)


def up(orch: Orchestrator, branch: str, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        reports = orch.up(branch, uses=USES, timeout=180)
    except WtError as exc:
        if "not available" not in str(exc) and "already allocated" not in str(exc):
            raise
        monkeypatch.setenv("WT_HTTP_PORT", str(free_port()))
        monkeypatch.setenv("WT_HTTPS_PORT", str(free_port()))
        monkeypatch.setenv("WT_POSTGRES_PORT", str(free_port()))
        docker("rm", "-f", "-v", postgres.container())
        reports = orch.up(branch, uses=USES, timeout=180)
    assert {r.service: r.outcome for r in reports} == {"back": "ok", "front": "ok"}, reports


def first(logs: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(logs) if needle in line)


def page_config(host: str) -> dict[str, str]:
    status, raw = fetch(host, "GET", "/")
    assert status == 200
    match = re.search(r"const cfg = (\{.*?\});", raw.decode())
    assert match, raw
    cfg: dict[str, str] = json.loads(match.group(1))
    return cfg


def test_notes_branches_are_isolated(
    notes: tuple[Orchestrator, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, logs = notes
    domain = orch.cfg.domain
    up(orch, "main", monkeypatch)
    # Infra first, then the back (compose), then the front (process): the front consumes the back.
    assert first(logs, "provider") < first(logs, "compose up") < first(logs, "starting process")
    up(orch, "feat/tags", monkeypatch)

    main_api, tags_api = f"api.main.{domain}", f"api.feat-tags.{domain}"
    main_front, tags_front = f"main.{domain}", f"feat-tags.{domain}"
    assert request(main_api, "GET", "/info") == (200, {"branch": "main", "database": "notes__main"})
    assert request(tags_api, "GET", "/info") == (
        200,
        {"branch": "feat/tags", "database": "notes__feat_tags"},
    )

    assert page_config(main_front) == {"branch": "main", "apiUrl": orch.url(main_api)}
    assert page_config(tags_front) == {"branch": "feat/tags", "apiUrl": orch.url(tags_api)}

    main_notes = request(main_api, "GET", "/notes")[1]
    tags_notes = request(tags_api, "GET", "/notes")[1]
    assert main_notes and all("tags" not in row for row in main_notes)
    assert tags_notes and all("tags" in row for row in tags_notes)

    status, created = request(tags_api, "POST", "/notes", {"text": "only here", "tags": ["x"]})
    assert (status, created["text"], created["tags"]) == (201, "only here", ["x"])
    assert "only here" in [row["text"] for row in request(tags_api, "GET", "/notes")[1]]
    assert "only here" not in [row["text"] for row in request(main_api, "GET", "/notes")[1]]

    orch.down("feat/tags")
    assert not health.probe(tags_api, "/healthz", router.https_port()).ok
    assert health.probe(main_api, "/healthz", router.https_port()).ok
    orch.down("main")
    assert not health.probe(main_api, "/healthz", router.https_port()).ok
    assert orch.store.load().branches == {}
