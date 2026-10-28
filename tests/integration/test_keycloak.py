"""Keycloak provider against a real server: `pytest -m docker`. Isolated by WT_INSTANCE."""

import json
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

import pytest

from wt import compose
from wt.config import Config
from wt.naming import Names
from wt.providers import keycloak
from wt.providers.base import BranchContext
from wt.providers.keycloak import KeycloakProvider
from wt.state import StateStore

pytestmark = pytest.mark.docker

REALM = "shop"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The shared server and its WT_HOME (also the value of the `home` env for every test)."""
    if shutil.which("docker") is None or docker("info").returncode != 0:
        pytest.fail("docker is not available")
    mp = pytest.MonkeyPatch()
    instance = f"wtkc{free_port()}"
    mp.setenv("WT_INSTANCE", instance)
    home = tmp_path_factory.mktemp("wt-home")
    mp.setenv("WT_HOME", str(home))
    mp.setenv("WT_KEYCLOAK_PORT", str(free_port()))
    mp.setenv("WT_HTTPS_PORT", str(free_port()))
    try:
        keycloak.ensure_server(REALM)
        yield str(home)
    finally:
        docker("rm", "-f", "-v", keycloak.container())
        docker("volume", "rm", "-f", keycloak.volume())
        docker("network", "rm", compose.network_name())
        mp.undo()


@pytest.fixture(autouse=True)
def module_home(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The root conftest gives every test a fresh WT_HOME; this module needs the server's one."""
    monkeypatch.setenv("WT_HOME", server)


def make(branch: str) -> tuple[KeycloakProvider, BranchContext]:
    cfg = Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": {"keycloak": {"realm": REALM, "client": "web"}},
        }
    )
    names = Names("shop", branch, "shop.localhost")
    port = keycloak.host_port()
    urls = (
        f"https://web.{names.branch_domain}:{port}",
        f"https://api.{names.branch_domain}:{port}",
    )
    return KeycloakProvider(cfg), BranchContext(names, branch, "main", StateStore("shop"), urls)


def clients() -> dict[str, list[str]]:
    rows = json.loads(
        keycloak.kcadm("get", "clients", "-r", REALM, "--fields", "clientId,redirectUris").stdout
    )
    return {r["clientId"]: r["redirectUris"] for r in rows}


def discovery(base: str) -> dict[str, object]:
    with urllib.request.urlopen(
        f"{base}/realms/{REALM}/.well-known/openid-configuration", timeout=30
    ) as resp:
        return dict(json.loads(resp.read()))


def auth_status(client_id: str, redirect_uri: str) -> int:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid",
        }
    )
    try:
        with urllib.request.urlopen(
            f"{keycloak.url()}/realms/{REALM}/protocol/openid-connect/auth?{query}", timeout=30
        ) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return e.code


def test_realm_is_created_once(server: str) -> None:
    keycloak.ensure_server(REALM)
    realms = json.loads(keycloak.kcadm("get", "realms", "--fields", "realm").stdout)
    assert [r["realm"] for r in realms].count(REALM) == 1


def test_create_makes_one_client_per_branch_and_is_idempotent(server: str) -> None:
    feat, feat_ctx = make("feat")
    longer, longer_ctx = make("feat-x")
    for provider, ctx in [(feat, feat_ctx), (longer, longer_ctx)]:
        assert provider.create(ctx).names == [f"{REALM}/web-{ctx.names.slug}"]
        assert provider.create(ctx).names == [f"{REALM}/web-{ctx.names.slug}"]
        check = provider.verify(ctx)
        assert check.ok, check.detail
    found = clients()
    assert set(found["web-feat"]) == {u + "/*" for u in feat_ctx.urls}
    assert set(found["web-feat-x"]) == {u + "/*" for u in longer_ctx.urls}
    assert list(found).count("web-feat") == 1


def test_issuer_matches_from_host_and_from_the_wt_network(server: str) -> None:
    provider, ctx = make("feat")
    issuer = provider.template_vars(ctx, "run")["keycloak"]["issuer"]
    assert discovery(keycloak.url())["issuer"] == issuer
    internal = provider.template_vars(ctx, "compose")["keycloak"]["internal_url"]
    script = (
        f"exec 3<>/dev/tcp/{keycloak.container()}/{keycloak.INTERNAL_PORT}; "
        f"printf 'GET /realms/{REALM}/.well-known/openid-configuration HTTP/1.0\\r\\n"
        f"Host: {keycloak.container()}:{keycloak.INTERNAL_PORT}\\r\\n\\r\\n' >&3; cat <&3"
    )
    res = docker(
        "run", "--rm", "--network", compose.network_name(), "--entrypoint", "bash",
        keycloak.image(), "-c", script,
    )  # fmt: skip
    assert internal == f"http://{keycloak.container()}:8080"
    assert res.returncode == 0, res.stderr
    # subprocess text mode turns CRLF into LF
    assert "\n\n" in res.stdout, (res.stdout, res.stderr)
    body = res.stdout.split("\n\n", 1)[1]
    assert json.loads(body)["issuer"] == issuer


def test_redirect_uri_is_enforced_per_client(server: str) -> None:
    _, feat_ctx = make("feat")
    _, longer_ctx = make("feat-x")
    own = feat_ctx.urls[0] + "/callback"
    other = longer_ctx.urls[0] + "/callback"
    assert auth_status("web-feat", own) == 200
    assert auth_status("web-feat-x", other) == 200
    assert auth_status("web-feat", other) != 200


def test_destroy_leaves_the_other_branch_and_the_realm(server: str) -> None:
    feat, feat_ctx = make("feat")
    longer, longer_ctx = make("feat-x")
    feat.destroy(feat_ctx)
    feat.destroy(feat_ctx)  # safe to repeat
    found = clients()
    assert "web-feat" not in found
    assert "web-feat-x" in found
    check = longer.verify(longer_ctx)
    assert check.ok, check.detail
    assert not feat.verify(feat_ctx).ok
    assert discovery(keycloak.url())["issuer"] == f"{keycloak.url()}/realms/{REALM}"
    longer.destroy(longer_ctx)
    assert "web-feat-x" not in clients()
