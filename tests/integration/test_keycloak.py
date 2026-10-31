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
from wt.errors import WtError
from wt.naming import Names
from wt.providers import keycloak
from wt.providers.base import BranchContext
from wt.providers.keycloak import KeycloakProvider
from wt.state import BranchState, StateStore

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


def make(
    branch: str, realm: str = REALM, project: str = "shop"
) -> tuple[KeycloakProvider, BranchContext]:
    cfg = Config.model_validate(
        {
            "project": project,
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": {"keycloak": {"realm": realm, "client": "web"}},
        }
    )
    names = Names(project, branch, "shop.localhost")
    port = keycloak.host_port()
    urls = (
        f"https://web.{names.branch_domain}:{port}",
        f"https://api.{names.branch_domain}:{port}",
    )
    return KeycloakProvider(cfg), BranchContext(names, branch, "main", StateStore(project), urls)


def leftover_configs() -> str:
    """How many kcadm login config files remain inside the Keycloak container."""
    res = docker(
        "exec", keycloak.container(), "sh", "-c", "ls /tmp/kcadm-*.config 2>/dev/null | wc -l"
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


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
        client = provider.client_id(ctx)
        assert provider.create(ctx).names == [f"{REALM}/{client}"]
        assert provider.create(ctx).names == [f"{REALM}/{client}"]
        check = provider.verify(ctx)
        assert check.ok, check.detail
    found = clients()
    feat_id, longer_id = feat.client_id(feat_ctx), longer.client_id(longer_ctx)
    assert feat_id == "web-shop.feat"  # the one literal: a rename of the scheme is caught
    assert set(found[feat_id]) == {u + "/*" for u in feat_ctx.urls}
    assert set(found[longer_id]) == {u + "/*" for u in longer_ctx.urls}
    assert list(found).count(feat_id) == 1


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
    feat, feat_ctx = make("feat")
    longer, longer_ctx = make("feat-x")
    own = feat_ctx.urls[0] + "/callback"
    other = longer_ctx.urls[0] + "/callback"
    feat_id, longer_id = feat.client_id(feat_ctx), longer.client_id(longer_ctx)
    assert auth_status(feat_id, own) == 200
    assert auth_status(longer_id, other) == 200
    assert auth_status(feat_id, other) != 200


def test_destroy_leaves_the_other_branch_and_the_realm(server: str) -> None:
    feat, feat_ctx = make("feat")
    longer, longer_ctx = make("feat-x")
    feat.destroy(feat_ctx)
    feat.destroy(feat_ctx)  # safe to repeat
    feat_id, longer_id = feat.client_id(feat_ctx), longer.client_id(longer_ctx)
    found = clients()
    assert feat_id not in found
    assert longer_id in found
    check = longer.verify(longer_ctx)
    assert check.ok, check.detail
    assert not feat.verify(feat_ctx).ok
    assert discovery(keycloak.url())["issuer"] == f"{keycloak.url()}/realms/{REALM}"
    longer.destroy(longer_ctx)
    assert longer_id not in clients()


def test_down_without_the_keycloak_section_deletes_the_recorded_client(server: str) -> None:
    feat, feat_ctx = make("nosection")
    sibling, sibling_ctx = make("nosection-sibling")
    for provider, ctx in [(feat, feat_ctx), (sibling, sibling_ctx)]:
        recorded = provider.create(ctx).names
        with ctx.state.edit() as state:
            state.branches[ctx.branch] = BranchState(
                branch=ctx.branch, slug=ctx.names.slug, providers={"keycloak": recorded}
            )
    realms_before = json.loads(keycloak.kcadm("get", "realms", "--fields", "realm").stdout)
    assert feat.client_id(feat_ctx) in clients()

    bare_cfg = Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
        }
    )
    bare = KeycloakProvider(bare_cfg)
    bare.ensure_server()
    bare.destroy(feat_ctx)

    found = clients()
    assert feat.client_id(feat_ctx) not in found
    assert sibling.client_id(sibling_ctx) in found
    assert json.loads(keycloak.kcadm("get", "realms", "--fields", "realm").stdout) == realms_before
    sibling.destroy(sibling_ctx)
    assert sibling.client_id(sibling_ctx) not in clients()


def test_same_branch_in_two_projects_keeps_separate_clients(server: str) -> None:
    shop, shop_ctx = make("main", project="shop")
    blog, blog_ctx = make("main", project="blog")
    shop_id, blog_id = shop.client_id(shop_ctx), blog.client_id(blog_ctx)
    assert shop_id != blog_id
    shop.create(shop_ctx)
    blog.create(blog_ctx)
    assert {shop_id, blog_id} <= set(clients())

    shop.destroy(shop_ctx)
    found = clients()
    assert shop_id not in found and blog_id in found
    assert blog.verify(blog_ctx).ok

    blog.destroy(blog_ctx)
    assert blog_id not in clients()


def test_destroy_cleans_a_branch_recorded_by_0_5_0_without_the_project(server: str) -> None:
    provider, ctx = make("legacy")
    other, other_ctx = make("legacy", project="blog")
    other.create(other_ctx)
    kcadm_args = (
        "create",
        "clients",
        "-r",
        REALM,
        "-s",
        "clientId=web-legacy",
        "-s",
        "enabled=true",
    )
    keycloak.kcadm(*kcadm_args)  # the 0.5.0 client id: `<client>-<slug>`
    with ctx.state.edit() as state:
        state.branches[ctx.branch] = BranchState(
            branch=ctx.branch, slug=ctx.names.slug, providers={"keycloak": [f"{REALM}/web-legacy"]}
        )

    provider.destroy(ctx)
    found = clients()
    assert "web-legacy" not in found
    assert other.client_id(other_ctx) in found

    other.destroy(other_ctx)
    assert other.client_id(other_ctx) not in clients()


def test_no_login_config_file_is_left_in_the_container(server: str) -> None:
    provider, ctx = make("cfgleak")
    provider.create(ctx)
    assert keycloak.find_client(REALM, provider.client_id(ctx)) is not None
    assert leftover_configs() == "0"
    assert keycloak.find_client("never-created-realm", "web") is None
    with pytest.raises(WtError):
        keycloak.kcadm("get", "clients", "-r", "never-created-realm")
    assert leftover_configs() == "0"
    provider.destroy(ctx)
    assert leftover_configs() == "0"


def test_missing_realm_destroy_is_silent_and_verify_fails(server: str) -> None:
    provider, ctx = make("norealm", realm="wt-never-created")
    provider.destroy(ctx)
    check = provider.verify(ctx)
    assert not check.ok
    assert "not found" in check.detail


def test_stopped_server_raises_then_destroy_works_after_restart(server: str) -> None:
    gone, gone_ctx = make("stopgone")
    kept, kept_ctx = make("stopkept")
    gone.create(gone_ctx)
    kept.create(kept_ctx)
    secrets = [keycloak.password(), keycloak.SCRIPT]
    assert docker("stop", keycloak.container()).returncode == 0
    try:
        for action in (gone.destroy, gone.verify):
            with pytest.raises(WtError) as raised:
                action(gone_ctx)
            message = str(raised.value)
            assert message.startswith("keycloak:"), message
            assert not any(secret in message for secret in secrets), message
    finally:
        assert docker("start", keycloak.container()).returncode == 0
        keycloak.wait_ready()
    gone.destroy(gone_ctx)
    found = clients()
    assert gone.client_id(gone_ctx) not in found
    assert kept.client_id(kept_ctx) in found
    assert kept.verify(kept_ctx).ok
    kept.destroy(kept_ctx)
    assert discovery(keycloak.url())["issuer"] == f"{keycloak.url()}/realms/{REALM}"
