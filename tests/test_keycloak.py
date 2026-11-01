import json
import stat
from collections.abc import Callable, Sequence

import pytest

from wt import compose, proc
from wt.config import Config
from wt.errors import WtError
from wt.naming import Names
from wt.providers import keycloak
from wt.providers.base import BranchContext
from wt.providers.keycloak import KeycloakProvider
from wt.state import BranchState, StateStore

URLS = ("https://shop.feat-cart.shop.localhost", "https://api.feat-cart.shop.localhost:8443")
UUID = "11111111-2222-3333-4444-555555555555"


def config(with_keycloak: bool = True) -> Config:
    shared = {"keycloak": {"realm": "shop", "client": "web"}} if with_keycloak else {}
    return Config.model_validate(
        {
            "project": "shop",
            "domain": "shop.localhost",
            "repos": {"api": "./api"},
            "services": {"api": {"repo": "api", "compose": "c.yml"}},
            "shared": shared,
        }
    )


class FakeProc:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.rules: list[tuple[str, proc.Result]] = []

    def on(self, needle: str, result: proc.Result) -> None:
        self.rules.append((needle, result))

    def kc(self) -> list[list[str]]:
        """Arguments after the `kcadm` $0 marker for every exec call, in order."""
        return [c[c.index("kcadm") + 1 :] for c in self.calls if "kcadm" in c]

    def __call__(self, args: Sequence[str], *, check: bool = True, **kwargs: object) -> proc.Result:
        self.calls.append(list(args))
        line = " ".join(args[args.index("kcadm") :]) if "kcadm" in args else " ".join(args)
        result = proc.Result(0, "", "")
        for needle, rule in self.rules:
            if needle in line:
                result = rule
                break
        if check and not result.ok:
            raise proc.CommandError(args, result)
        return result


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeProc:
    f = FakeProc()
    monkeypatch.setattr(proc, "run", f)
    f.on("docker port", proc.Result(0, f"8080/tcp -> 127.0.0.1:{keycloak.host_port()}\n", ""))
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr("wt.providers.keycloak.time.sleep", lambda s: None)
    return f


def ctx(
    branch: str = "feat/cart", urls: tuple[str, ...] = URLS, project: str = "shop"
) -> BranchContext:
    return BranchContext(
        Names(project, branch, f"{project}.localhost"), branch, "main", StateStore(project), urls
    )


def provider(with_keycloak: bool = True) -> KeycloakProvider:
    return KeycloakProvider(config(with_keycloak), lambda message: None)


def clients(*rows: tuple[str, str, list[str]]) -> proc.Result:
    return proc.Result(
        0,
        json.dumps([{"id": i, "clientId": c, "redirectUris": r} for i, c, r in rows]),
        "",
    )


REALM_NOT_FOUND = proc.Result(
    1,
    "",
    "Logging into http://localhost:8080 as user admin of realm master\n"
    "Resource not found for url: http://localhost:8080/admin/realms/shop/clients?clientId=x\n",
)
LOGIN_REFUSED = proc.Result(
    1,
    "",
    "Logging into http://localhost:8080 as user admin of realm master\n"
    "Invalid user credentials [invalid_grant]\n",
)
UNREACHABLE = proc.Result(
    1,
    "",
    "Logging into http://localhost:8080 as user admin of realm master\n"
    "Failed to send request - Connect to localhost:8080 [localhost/127.0.0.1] failed: "
    "Connection refused\n",
)
CONTAINER_STOPPED = proc.Result(
    1, "", "Error response from daemon: container abc123 is not running\n"
)
FAILURES = pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (LOGIN_REFUSED, "Invalid user credentials"),
        (UNREACHABLE, "Connection refused"),
        (CONTAINER_STOPPED, "is not running"),
        (proc.Result(1, "", "something kcadm never said\n"), "something kcadm never said"),
        (proc.Result(0, "<html>proxy error</html>", ""), "did not return JSON"),
        (proc.Result(0, '{"error": "x"}', ""), "did not return a list"),
    ],
)


def rendered(urls: tuple[str, ...] = URLS) -> list[str]:
    return [u + "/*" for u in urls]


def test_names_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("WT_INSTANCE", "WT_KEYCLOAK_IMAGE", "WT_KEYCLOAK_PORT"):
        monkeypatch.delenv(var, raising=False)
    assert keycloak.container() == "wt-keycloak"
    assert keycloak.volume() == "wt-keycloak-data"
    assert keycloak.image() == "quay.io/keycloak/keycloak:26.8.1"
    assert keycloak.host_port() == 58080
    monkeypatch.setenv("WT_INSTANCE", "t1")
    monkeypatch.setenv("WT_KEYCLOAK_IMAGE", "kc:1")
    monkeypatch.setenv("WT_KEYCLOAK_PORT", "58100")
    assert keycloak.container() == "t1-keycloak"
    assert keycloak.volume() == "t1-keycloak-data"
    assert keycloak.image() == "kc:1"
    assert keycloak.host_port() == 58100


def test_password_created_once_with_mode_0600() -> None:
    first = keycloak.password()
    path = keycloak.password_file()
    assert first and path.read_text().strip() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keycloak.password() == first


def test_password_fixes_mode_and_regenerates_empty_file() -> None:
    path = keycloak.password_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret\n")
    path.chmod(0o644)
    assert keycloak.password() == "secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("\n")
    assert keycloak.password() not in ("", "secret")


def test_compose_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_KEYCLOAK_PORT", "58100")
    doc = keycloak.compose_doc()
    svc = doc["services"]["keycloak"]
    assert svc["image"] == keycloak.image()
    assert svc["container_name"] == keycloak.container()
    assert svc["command"] == ["start-dev"]
    assert svc["restart"] == "unless-stopped"
    assert svc["ports"] == ["127.0.0.1:58100:8080"]
    assert svc["networks"] == [compose.NETWORK]
    assert svc["volumes"] == [f"{keycloak.volume()}:/opt/keycloak/data"]
    assert "healthcheck" not in svc
    env = svc["environment"]
    assert env["KC_BOOTSTRAP_ADMIN_USERNAME"] == "admin"
    assert env["KC_BOOTSTRAP_ADMIN_PASSWORD"] == keycloak.password()
    assert env["KC_HOSTNAME"] == "http://localhost:58100"
    assert env["KC_HOSTNAME_BACKCHANNEL_DYNAMIC"] == "true"
    assert env["KC_HTTP_ENABLED"] == "true"
    assert doc["volumes"] == {keycloak.volume(): {"name": keycloak.volume()}}


def test_kcadm_runs_a_fixed_script_with_positional_args(fake: FakeProc) -> None:
    keycloak.kcadm("get", "realms/x; rm -rf /", "-r", "$(id)")
    call = fake.calls[0]
    assert call[:5] == ["docker", "exec", keycloak.container(), "bash", "-c"]
    assert call[6] == "kcadm"
    assert call[7:] == ["get", "realms/x; rm -rf /", "-r", "$(id)"]
    script = call[5]
    assert "$KC_BOOTSTRAP_ADMIN_PASSWORD" in script and 'kcadm.sh "$@"' in script
    assert "trap" in script and "rm -f" in script
    assert "rm -rf /" not in script and "$(id)" not in script


def test_admin_password_never_in_host_argv(fake: FakeProc) -> None:
    secret = keycloak.password()
    fake.on("get clients", clients())
    provider().ensure_server()
    provider().create(ctx())
    provider().verify(ctx())
    provider().destroy(ctx())
    assert fake.calls
    assert all(secret not in part for call in fake.calls for part in call)


def test_ensure_server_starts_waits_then_creates_realm(fake: FakeProc) -> None:
    provider().ensure_server()
    cmds = [" ".join(c) for c in fake.calls]
    up = next(i for i, c in enumerate(cmds) if "compose" in c and " up " in c)
    login = next(i for i, c in enumerate(cmds) if c.endswith("get realms/master"))
    assert up < login
    assert (keycloak.server_dir() / "compose.yml").exists()
    assert [
        "create",
        "realms",
        "-s",
        "realm=shop",
        "-s",
        "enabled=true",
        "-s",
        "sslRequired=none",
    ] in fake.kc()


def test_ensure_server_without_the_section_only_starts_and_waits_for_the_login(
    fake: FakeProc,
) -> None:
    provider(with_keycloak=False).ensure_server()
    cmds = [" ".join(c) for c in fake.calls]
    up = next(i for i, c in enumerate(cmds) if "compose" in c and " up " in c)
    login = next(i for i, c in enumerate(cmds) if c.endswith("get realms/master"))
    assert up < login
    assert fake.kc() == [["get", "realms/master"]]  # no realm is created


def test_ensure_server_without_the_section_still_fails_when_the_login_is_refused(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.on("get realms/master", LOGIN_REFUSED)
    ticks = iter(range(0, 1000, 100))
    monkeypatch.setattr("wt.providers.keycloak.time.monotonic", lambda: next(ticks))
    with pytest.raises(WtError, match="did not accept the admin login"):
        provider(with_keycloak=False).ensure_server()


def test_ensure_server_treats_existing_realm_as_done(fake: FakeProc) -> None:
    fake.on("create realms", proc.Result(1, "", "Realm shop already exists"))
    provider().ensure_server()


def test_ensure_server_raises_on_other_realm_errors(fake: FakeProc) -> None:
    fake.on("create realms", proc.Result(1, "", "boom"))
    with pytest.raises(WtError) as err:
        provider().ensure_server()
    assert str(err.value) == "keycloak: boom (kcadm.sh create realms)"


def test_ensure_realm_failure_drops_the_login_notice_and_puts_the_cause_first(
    fake: FakeProc,
) -> None:
    fake.on("create realms", UNREACHABLE)
    with pytest.raises(WtError) as err:
        keycloak.ensure_realm("shop")
    message = str(err.value)
    assert "Logging into" not in message and "Connection refused" in message[:100]
    assert message.endswith("(kcadm.sh create realms)")


def test_wait_ready_times_out_naming_password_file_and_volume(
    fake: FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.on("get realms/master", proc.Result(1, "", "Invalid user credentials"))
    ticks = iter(range(0, 1000, 10))
    monkeypatch.setattr("wt.providers.keycloak.time.monotonic", lambda: next(ticks))
    with pytest.raises(WtError) as err:
        keycloak.wait_ready(timeout=25)
    assert str(keycloak.password_file()) in str(err.value)
    assert keycloak.volume() in str(err.value)


def test_find_client_returns_none_for_a_missing_realm(fake: FakeProc) -> None:
    fake.on("get clients", REALM_NOT_FOUND)
    assert keycloak.find_client("shop", "web-x") is None


def test_find_client_returns_none_for_a_missing_client(fake: FakeProc) -> None:
    fake.on("get clients", proc.Result(0, "[ ]", ""))
    assert keycloak.find_client("shop", "web-x") is None


def test_find_client_does_not_take_a_login_404_for_a_missing_realm(fake: FakeProc) -> None:
    fake.on(
        "get clients",
        proc.Result(1, "", "Resource not found for url: http://localhost:8080/realms/master/x\n"),
    )
    with pytest.raises(WtError, match=r"\(kcadm\.sh get clients\)$"):
        keycloak.find_client("shop", "web-x")


@FAILURES
def test_find_client_raises_on_every_other_failure(
    fake: FakeProc, failure: proc.Result, reason: str
) -> None:
    fake.on("get clients", failure)
    with pytest.raises(WtError, match=reason):
        keycloak.find_client("shop", "web-x")


def test_failing_kcadm_puts_the_cause_first_and_names_the_call_not_the_login_script(
    fake: FakeProc,
) -> None:
    fake.on("get realms/shop", LOGIN_REFUSED)
    with pytest.raises(WtError) as err:
        keycloak.kcadm("get", "realms/shop")
    message = str(err.value)
    assert message == "keycloak: Invalid user credentials [invalid_grant] (kcadm.sh get realms)"
    assert keycloak.SCRIPT not in message and "mktemp" not in message
    assert "KC_BOOTSTRAP_ADMIN_PASSWORD" not in message and "docker exec" not in message


def test_the_cause_survives_a_long_realm_and_client_name(fake: FakeProc) -> None:
    realm, client = "a-long-realm-name", "web-some-very-long-branch-name-here"
    fake.on("get clients", proc.Result(1, "", "Connection refused"))
    with pytest.raises(WtError) as err:
        keycloak.find_client(realm, client)
    message = str(err.value)
    assert "Connection refused" in message[:100]
    assert realm not in message and client not in message


def test_a_long_reason_is_capped_but_keeps_its_end(fake: FakeProc) -> None:
    fake.on("get clients", UNREACHABLE)
    with pytest.raises(WtError) as err:
        keycloak.find_client("shop", "web-x")
    message = str(err.value)
    assert len(message) < 120 and "Connection refused" in message


def test_the_call_name_drops_ids_and_names(fake: FakeProc) -> None:
    fake.on("delete clients", UNREACHABLE)
    with pytest.raises(WtError) as err:
        keycloak.kcadm("delete", f"clients/{UUID}", "-r", "shop")
    assert str(err.value).endswith("(kcadm.sh delete clients)")


def test_no_message_the_module_builds_carries_the_admin_password(fake: FakeProc) -> None:
    secret = keycloak.password()
    fake.on("kcadm", UNREACHABLE)
    failing: list[Callable[[], object]] = [
        lambda: keycloak.kcadm("get", "realms/shop"),
        lambda: keycloak.ensure_realm("shop"),
        lambda: keycloak.find_client("shop", "web-x"),
        lambda: provider().verify(ctx()),
        lambda: provider().destroy(ctx()),
        lambda: keycloak.wait_ready(timeout=0),
    ]
    for call in failing:
        with pytest.raises(WtError) as err:
            call()
        assert secret not in str(err.value)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (proc.Result(1, "", "[\n  {"), "exit code 1"),
        (proc.Result(1, "", "<html>...</html>"), "exit code 1"),
        (proc.Result(1, "", "Logging into x\nboom"), "boom"),
        (proc.Result(1, "[\n  {", "boom"), "boom"),
        (proc.Result(1, "stdout cause", ""), "stdout cause"),
        (proc.Result(1, "stdout cause", "stderr cause"), "stderr cause"),
        (proc.Result(1, "", '{\n  "error": "x"\n}\n'), '"error": "x"'),
        (proc.Result(137, "", ""), "exit code 137"),
    ],
)
def test_failure_reason_prefers_stderr_and_never_returns_a_lone_fragment(
    failure: proc.Result, expected: str
) -> None:
    assert keycloak._failure_reason(failure) == expected


def test_kcadm_failure_without_output_still_has_a_reason(fake: FakeProc) -> None:
    fake.on("get realms/shop", proc.Result(137, "", ""))
    with pytest.raises(WtError, match="exit code 137"):
        keycloak.kcadm("get", "realms/shop")


def test_client_id_uses_project_and_branch_slug() -> None:
    assert provider().client_id(ctx("feat/Cart")) == "web-shop.feat-cart"


def test_two_projects_with_the_same_branch_get_different_client_ids() -> None:
    p = provider()
    assert p.client_id(ctx("main", project="shop")) == "web-shop.main"
    assert p.client_id(ctx("main", project="blog")) == "web-blog.main"


def test_project_and_slug_never_blur_into_each_other() -> None:
    p = provider()
    assert p.client_id(ctx("c", project="a-b")) != p.client_id(ctx("b-c", project="a"))


def test_create_makes_a_public_standard_flow_client(fake: FakeProc) -> None:
    fake.on("get clients", clients())
    fake.on("create clients", proc.Result(0, f"Created new client with id '{UUID}'\n", ""))
    res = provider().create(ctx())
    assert res.names == ["shop/web-shop.feat-cart"]
    lookup, create = fake.kc()
    assert lookup[:2] == ["get", "clients"]
    assert "clientId=web-shop.feat-cart" in lookup and lookup[lookup.index("-r") + 1] == "shop"
    assert create[:2] == ["create", "clients"]
    assert create[create.index("-r") + 1] == "shop"
    sets = [create[i + 1] for i, a in enumerate(create) if a == "-s"]
    assert "clientId=web-shop.feat-cart" in sets
    assert "publicClient=true" in sets
    assert "standardFlowEnabled=true" in sets
    assert f"redirectUris={json.dumps(rendered())}" in sets
    assert f"webOrigins={json.dumps(list(URLS))}" in sets
    assert not any("pkce" in s for s in sets)


def test_create_updates_an_existing_client(fake: FakeProc) -> None:
    fake.on("get clients", clients((UUID, "web-shop.feat-cart", ["https://old/*"])))
    provider().create(ctx())
    _, update = fake.kc()
    assert update[:2] == ["update", f"clients/{UUID}"]
    assert update[update.index("-r") + 1] == "shop"
    sets = [update[i + 1] for i, a in enumerate(update) if a == "-s"]
    assert f"redirectUris={json.dumps(rendered())}" in sets
    assert not any(c[:2] == ["create", "clients"] for c in fake.kc())


def test_create_matches_client_id_exactly_not_by_prefix(fake: FakeProc) -> None:
    fake.on("get clients", clients(("other-id", "web-shop.feat-cart-x", ["https://x/*"])))
    provider().create(ctx())
    assert fake.kc()[1][:2] == ["create", "clients"]


def test_create_without_keycloak_section_fails() -> None:
    with pytest.raises(WtError, match=r"infra\.keycloak"):
        provider(with_keycloak=False).create(ctx())


def test_verify_ok(fake: FakeProc) -> None:
    fake.on("get clients", clients((UUID, "web-shop.feat-cart", list(reversed(rendered())))))
    check = provider().verify(ctx())
    assert check.ok and not check.warnings


def test_verify_detects_redirect_uri_mismatch(fake: FakeProc) -> None:
    fake.on("get clients", clients((UUID, "web-shop.feat-cart", ["https://old/*"])))
    check = provider().verify(ctx())
    assert not check.ok and "redirect" in check.detail


def test_verify_detects_missing_client(fake: FakeProc) -> None:
    fake.on("get clients", clients())
    check = provider().verify(ctx())
    assert not check.ok and "web-shop.feat-cart" in check.detail


def test_verify_reports_a_missing_realm_as_a_failed_check(fake: FakeProc) -> None:
    fake.on("get clients", REALM_NOT_FOUND)
    check = provider().verify(ctx())
    assert not check.ok and "shop" in check.detail and "web-shop.feat-cart" in check.detail


@FAILURES
def test_verify_raises_when_kcadm_fails(fake: FakeProc, failure: proc.Result, reason: str) -> None:
    fake.on("get clients", failure)
    with pytest.raises(WtError, match=reason) as err:
        provider().verify(ctx())
    assert str(err.value).endswith("(kcadm.sh get clients)")


def test_verify_warns_when_no_routed_urls(fake: FakeProc) -> None:
    fake.on("get clients", clients((UUID, "web-shop.feat-cart", [])))
    check = provider().verify(ctx(urls=()))
    assert check.ok and check.warnings


def test_destroy_deletes_only_the_exact_client(fake: FakeProc) -> None:
    fake.on(
        "get clients",
        clients(
            ("id-x", "web-shop.feat-cart-x", ["https://x/*"]),
            (UUID, "web-shop.feat-cart", ["https://y/*"]),
        ),
    )
    provider().destroy(ctx())
    deletes = [c for c in fake.kc() if c[0] == "delete"]
    assert deletes == [["delete", f"clients/{UUID}", "-r", "shop"]]


def test_destroy_of_feat_in_shop_never_touches_siblings_or_other_projects(
    fake: FakeProc,
) -> None:
    fake.on(
        "get clients",
        clients(
            ("id-x", "web-shop.feat-x", []),
            ("id-sh", "web-sh.feat", []),
            ("id-blog", "web-blog.feat", []),
            ("id-old", "web-feat", []),
            (UUID, "web-shop.feat", []),
        ),
    )
    provider().destroy(ctx("feat"))
    assert [c for c in fake.kc() if c[0] == "delete"] == [
        ["delete", f"clients/{UUID}", "-r", "shop"]
    ]


def test_destroy_never_touches_a_longer_branch(fake: FakeProc) -> None:
    fake.on("get clients", clients(("id-x", "web-shop.feat-x", ["https://x/*"])))
    provider().destroy(ctx("feat"))
    assert not any(c[0] == "delete" for c in fake.kc())


def test_destroy_is_silent_when_the_realm_is_missing(fake: FakeProc) -> None:
    fake.on("get clients", REALM_NOT_FOUND)
    provider().destroy(ctx())
    assert not any(c[0] == "delete" for c in fake.kc())


def test_destroy_is_silent_when_the_client_is_missing(fake: FakeProc) -> None:
    fake.on("get clients", proc.Result(0, "[ ]", ""))
    provider().destroy(ctx())
    assert not any(c[0] == "delete" for c in fake.kc())


@FAILURES
def test_destroy_raises_when_kcadm_fails(fake: FakeProc, failure: proc.Result, reason: str) -> None:
    fake.on("get clients", failure)
    with pytest.raises(WtError, match=reason):
        provider().destroy(ctx())
    assert not any(c[0] == "delete" for c in fake.kc())


def test_destroy_raises_when_the_delete_itself_fails(fake: FakeProc) -> None:
    fake.on("get clients", clients((UUID, "web-shop.feat-cart", [])))
    fake.on("delete clients", UNREACHABLE)
    with pytest.raises(WtError, match="Connection refused"):
        provider().destroy(ctx())


def test_destroy_uses_recorded_names_when_config_is_gone(fake: FakeProc) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart", slug="feat-cart", providers={"keycloak": ["old/web-shop.feat-cart"]}
        )
    fake.on("get clients", clients((UUID, "web-shop.feat-cart", [])))
    provider(with_keycloak=False).destroy(c)
    kc = fake.kc()
    assert kc[0][kc[0].index("-r") + 1] == "old"
    assert ["delete", f"clients/{UUID}", "-r", "old"] in kc


def test_destroy_cleans_a_client_recorded_by_0_5_0_and_the_current_one(fake: FakeProc) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart", slug="feat-cart", providers={"keycloak": ["shop/web-feat-cart"]}
        )
    fake.on(
        "get clients",
        clients(
            (UUID, "web-feat-cart", []),
            ("id-new", "web-shop.feat-cart", []),
            ("id-x", "web-feat-cart-x", []),
        ),
    )
    provider().destroy(c)
    deletes = [cmd for cmd in fake.kc() if cmd[0] == "delete"]
    assert deletes == [
        ["delete", f"clients/{UUID}", "-r", "shop"],
        ["delete", "clients/id-new", "-r", "shop"],
    ]


def test_down_after_the_section_left_the_config_deletes_the_recorded_client(
    fake: FakeProc,
) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart",
            slug="feat-cart",
            providers={"keycloak": ["shop/web-shop.feat-cart"]},
        )
    fake.on(
        "get clients",
        clients((UUID, "web-shop.feat-cart", []), ("id-x", "web-shop.feat-cart-x", [])),
    )
    p = provider(with_keycloak=False)
    p.ensure_server()  # what the orchestrator calls before destroy
    p.destroy(c)
    kc = fake.kc()
    assert ["delete", f"clients/{UUID}", "-r", "shop"] in kc
    assert [cmd for cmd in kc if cmd[0] == "delete"] == [
        ["delete", f"clients/{UUID}", "-r", "shop"]
    ]
    assert not any(cmd[1] == "realms" for cmd in kc if cmd[0] in ("create", "delete", "update"))


def test_destroy_without_the_section_is_silent_when_the_recorded_realm_is_gone(
    fake: FakeProc,
) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart",
            slug="feat-cart",
            providers={"keycloak": ["shop/web-shop.feat-cart"]},
        )
    fake.on("get clients", REALM_NOT_FOUND)
    provider(with_keycloak=False).destroy(c)
    assert not any(cmd[0] == "delete" for cmd in fake.kc())


def test_destroy_without_the_section_skips_malformed_names_and_keeps_going(
    fake: FakeProc,
) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart",
            slug="feat-cart",
            providers={"keycloak": ["", "/web", "shop/", "no-slash", "shop/web-shop.feat-cart"]},
        )
    fake.on("get clients", clients((UUID, "web-shop.feat-cart", [])))
    provider(with_keycloak=False).destroy(c)
    gets = [cmd for cmd in fake.kc() if cmd[0] == "get"]
    assert len(gets) == 1
    assert ["delete", f"clients/{UUID}", "-r", "shop"] in fake.kc()


def test_destroy_ignores_malformed_recorded_names(fake: FakeProc) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart", slug="feat-cart", providers={"keycloak": ["no-slash"]}
        )
    provider(with_keycloak=False).destroy(c)
    assert fake.calls == []


def test_template_vars_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WT_INSTANCE", raising=False)
    monkeypatch.delenv("WT_KEYCLOAK_PORT", raising=False)
    p = provider()
    base = {
        "url": "http://localhost:58080",
        "issuer": "http://localhost:58080/realms/shop",
        "realm": "shop",
        "client_id": "web-shop.feat-cart",
    }
    assert p.template_vars(ctx(), "compose") == {
        "keycloak": {**base, "internal_url": "http://wt-keycloak:8080"}
    }
    assert p.template_vars(ctx(), "run") == {
        "keycloak": {**base, "internal_url": "http://localhost:58080"}
    }
