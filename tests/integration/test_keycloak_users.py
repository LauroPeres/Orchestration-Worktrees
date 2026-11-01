"""Keycloak dev users (SPEC section 25) through the real CLI against a real Keycloak.

One isolated installation (WT_INSTANCE, WT_HOME, ports) for the module: Keycloak takes a minute
or two to start, so the tests share it and each one uses its own realm. `pytest -m docker`.
"""

import json
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from wt import compose
from wt.providers import keycloak

from .isolation import reserve_free_indexes
from .test_v061 import UP_TIMEOUT, Wt, docker, free_port, serve_service

pytestmark = pytest.mark.docker

SAMPLE_PAUSE = 0.02
LOGIN_CONFIG = "/tmp/kcadm-login-test.config"  # inside the Keycloak container, removed after use


class Stack:
    """The isolated installation plus the helpers every test needs."""

    def __init__(self, wt: Wt, repo: Path) -> None:
        self.wt = wt
        self.repo = repo
        self.argv = ""  # every process argv sampled during the last `up`

    def write_catalog(self, realm: str, users: list[dict[str, Any]]) -> None:
        keycloak_infra = {"realm": realm, "client": "web", "users": users}
        service = serve_service("app", infra={"keycloak": keycloak_infra})
        document: dict[str, Any] = {
            "project": self.wt.instance,
            "domain": self.wt.domain,
            "repos": {"app": str(self.repo)},
            "services": {"app": service},
        }
        self.wt.catalog.write_text(yaml.safe_dump(document))

    def up(self, branch: str, secrets: list[str]) -> subprocess.CompletedProcess[str]:
        """`wt up` with `ps -eo args` sampled while it runs (kept in `self.argv`)."""
        seen: list[str] = []
        stop = threading.Event()

        def sample() -> None:
            while not stop.is_set():
                seen.append(
                    subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
                )
                time.sleep(SAMPLE_PAUSE)

        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            result = self.wt.run("up", branch, "--uses", "app", "--timeout", str(UP_TIMEOUT))
        finally:
            stop.set()
            sampler.join()
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert len(seen) > 5
        self.argv = "\n".join(seen)
        for secret in secrets:
            assert secret not in result.stdout + result.stderr
            host_side = [
                line for line in self.argv.splitlines() if "docker" in line or "wt" in line
            ]
            assert secret not in "\n".join(host_side), "password in the argv of a host process"
        return result


def kcadm_json(*args: str) -> list[dict[str, Any]]:
    rows = json.loads(keycloak.kcadm(*args).stdout)
    assert isinstance(rows, list)
    return rows


def usernames(realm: str) -> list[str]:
    return sorted(
        r["username"] for r in kcadm_json("get", "users", "-r", realm, "--fields", "username")
    )


def roles_of(realm: str, username: str) -> set[str]:
    return {r["name"] for r in kcadm_json("get-roles", "-r", realm, "--uusername", username)}


def realm_roles(realm: str) -> set[str]:
    return {r["name"] for r in kcadm_json("get", "roles", "-r", realm, "--fields", "name")}


def login(realm: str, username: str, password: str) -> subprocess.CompletedProcess[str]:
    """Log in as the user through the realm's admin-cli client (a password grant).

    The `web` client is public with direct access grants off, so a plain token request against it
    would fail for a reason that has nothing to do with the user.
    """
    script = (
        f"/opt/keycloak/bin/kcadm.sh config credentials --config {LOGIN_CONFIG} "
        f'--server http://localhost:{keycloak.INTERNAL_PORT} --realm "$1" --user "$2" '
        f'--password "$3"; code=$?; rm -f {LOGIN_CONFIG}; exit $code'
    )
    return docker(
        "exec", keycloak.container(), "bash", "-c", script, "login", realm, username, password
    )


def realm_name() -> str:
    return f"r{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.fail("docker is not available")
    mp = pytest.MonkeyPatch()
    instance = f"wtkcu{free_port()}"
    wt_home = tmp_path_factory.mktemp("wt-home")
    base = tmp_path_factory.mktemp("project")
    repo = base / "app"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["add", "."]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test",
         "commit", "-qm", "init"],
        check=True, capture_output=True,
    )  # fmt: skip
    mp.setenv("WT_INSTANCE", instance)
    mp.setenv("WT_HOME", str(wt_home))
    mp.setenv("WT_CATALOG", str(base / "catalog.yaml"))
    mp.setenv("WT_KEYCLOAK_PORT", str(free_port()))
    mp.setenv("WT_HTTP_PORT", str(free_port()))
    mp.setenv("WT_HTTPS_PORT", str(free_port()))
    mp.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")  # no mkcert: the self-signed path
    reserve_free_indexes(wt_home)
    wt = Wt(instance, wt_home, base / "catalog.yaml")
    try:
        yield Stack(wt, repo)
    finally:
        wt.cleanup()
        docker("rm", "-f", "-v", keycloak.container())
        docker("volume", "rm", "-f", keycloak.volume())
        docker("network", "rm", compose.network_name())
        mp.undo()


@pytest.fixture(autouse=True)
def module_env(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root conftest gives every test a fresh WT_HOME; this module needs the stack's one."""
    monkeypatch.setenv("WT_HOME", str(stack.wt.wt_home))


def users_config(pw1: str, pw2: str) -> list[dict[str, Any]]:
    return [
        {
            "username": "alice",
            "password": pw1,
            "email": "alice@example.test",
            "roles": ["editor", "viewer"],
        },
        {"username": "bob", "password": pw2},
    ]


def test_users_and_roles_are_created_and_nothing_leaks(stack: Stack) -> None:
    realm = realm_name()
    pw1, pw2 = (f"Pw-{uuid.uuid4().hex}" for _ in range(2))
    secrets = [pw1, pw2]
    stack.write_catalog(realm, users_config(pw1, pw2))

    stack.up("feat/one", secrets)

    assert usernames(realm) == ["alice", "bob"]
    assert {"editor", "viewer"} <= roles_of(realm, "alice")
    assert not {"editor", "viewer"} & roles_of(realm, "bob")
    email = kcadm_json("get", "users", "-r", realm, "-q", "username=alice", "--fields", "email")
    assert email == [{"email": "alice@example.test"}]
    for args in (["ls", "--json"], ["ls"]):
        listing = stack.wt.run(*args)
        assert listing.returncode == 0, (listing.stdout, listing.stderr)
        assert not any(s in listing.stdout + listing.stderr for s in secrets)
    files = [p for p in stack.wt.wt_home.rglob("*") if p.is_file()]
    state = "".join(p.read_text(errors="replace") for p in files)
    assert not any(s in state for s in secrets), "password stored under WT_HOME"
    stack.wt.run("down", "feat/one", "--force")


def test_user_can_log_in_right_after_up(stack: Stack) -> None:
    realm = realm_name()
    pw1, pw2 = (f"Pw-{uuid.uuid4().hex}" for _ in range(2))
    stack.write_catalog(realm, users_config(pw1, pw2))
    stack.up("feat/login", [pw1, pw2])

    # the login goes through the realm's admin-cli client (a password grant)
    for username, password in (("alice", pw1), ("bob", pw2)):
        res = login(realm, username, password)
        assert res.returncode == 0, (username, res.stdout, res.stderr)
    stack.wt.run("down", "feat/login", "--force")


def test_wrong_password_is_refused(stack: Stack) -> None:
    realm = realm_name()
    pw1, pw2 = (f"Pw-{uuid.uuid4().hex}" for _ in range(2))
    stack.write_catalog(realm, users_config(pw1, pw2))
    stack.up("feat/wrong", [pw1, pw2])

    assert login(realm, "alice", pw1).returncode == 0
    wrong = login(realm, "alice", "not-the-password")
    print("kcadm wrong-password output:", repr(wrong.stdout), repr(wrong.stderr))
    assert wrong.returncode != 0
    assert pw1 not in wrong.stdout + wrong.stderr
    stack.wt.run("down", "feat/wrong", "--force")


def test_users_survive_other_branches_a_password_change_and_down(stack: Stack) -> None:
    realm = realm_name()
    pw1, pw2, pw3 = (f"Pw-{uuid.uuid4().hex}" for _ in range(3))
    secrets = [pw1, pw2, pw3]
    stack.write_catalog(realm, users_config(pw1, pw2))
    stack.up("feat/one", secrets)

    # another branch of the same realm leaves users, roles and passwords alone
    stack.up("feat/two", secrets)
    assert usernames(realm) == ["alice", "bob"]
    assert login(realm, "alice", pw1).returncode == 0
    assert login(realm, "bob", pw2).returncode == 0
    assert roles_of(realm, "alice") >= {"editor", "viewer"}

    # a changed password in the catalog: the new one works, the old one fails
    stack.write_catalog(realm, users_config(pw3, pw2))
    stack.up("feat/two", secrets)
    assert login(realm, "alice", pw3).returncode == 0
    assert login(realm, "alice", pw1).returncode != 0
    assert login(realm, "bob", pw2).returncode == 0

    # down removes the branch's client only
    for branch in ("feat/one", "feat/two"):
        down = stack.wt.run("down", branch, "--force")
        assert down.returncode == 0, (down.stdout, down.stderr)
        assert not any(s in down.stdout + down.stderr for s in secrets)
    rows = kcadm_json("get", "clients", "-r", realm, "--fields", "clientId")
    assert not any(r["clientId"].startswith("web-") for r in rows), rows
    assert usernames(realm) == ["alice", "bob"]
    assert realm_roles(realm) >= {"editor", "viewer"}
    assert roles_of(realm, "alice") >= {"editor", "viewer"}
    assert login(realm, "alice", pw3).returncode == 0

    # a later up of a new branch finds the users again and keeps them single
    stack.up("feat/three", secrets)
    assert usernames(realm) == ["alice", "bob"]
    stack.wt.run("down", "feat/three", "--force")


def test_password_is_in_no_process_argv_on_the_machine(stack: Stack) -> None:
    """SPEC section 25: the password goes through the environment, not argv.

    `ps` on the host also lists the processes inside the container, so this covers the kcadm
    process the `docker exec` starts, not only the `docker exec` command line.
    """
    realm = realm_name()
    password = f"Pw-{uuid.uuid4().hex}"
    stack.write_catalog(realm, [{"username": "carol", "password": password}])
    stack.up("feat/argv", [password])
    try:
        assert password not in stack.argv
    finally:
        stack.wt.run("down", "feat/argv", "--force")
