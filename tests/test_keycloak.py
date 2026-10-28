import json
import stat
from collections.abc import Sequence

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
    monkeypatch.setattr(compose, "ensure_network", lambda: None)
    monkeypatch.setattr("wt.providers.keycloak.time.sleep", lambda s: None)
    return f


def ctx(branch: str = "feat/cart", urls: tuple[str, ...] = URLS) -> BranchContext:
    return BranchContext(
        Names("shop", branch, "shop.localhost"), branch, "main", StateStore("shop"), urls
    )


def provider(with_keycloak: bool = True) -> KeycloakProvider:
    return KeycloakProvider(config(with_keycloak), lambda message: None)


def clients(*rows: tuple[str, str, list[str]]) -> proc.Result:
    return proc.Result(
        0,
        json.dumps([{"id": i, "clientId": c, "redirectUris": r} for i, c, r in rows]),
        "",
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


def test_ensure_server_treats_existing_realm_as_done(fake: FakeProc) -> None:
    fake.on("create realms", proc.Result(1, "", "Realm shop already exists"))
    provider().ensure_server()


def test_ensure_server_raises_on_other_realm_errors(fake: FakeProc) -> None:
    fake.on("create realms", proc.Result(1, "", "boom"))
    with pytest.raises(WtError, match="boom"):
        provider().ensure_server()


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


def test_client_id_uses_branch_slug() -> None:
    assert provider().client_id(ctx("feat/Cart")) == "web-feat-cart"


def test_create_makes_a_public_standard_flow_client(fake: FakeProc) -> None:
    fake.on("get clients", clients())
    fake.on("create clients", proc.Result(0, f"Created new client with id '{UUID}'\n", ""))
    res = provider().create(ctx())
    assert res.names == ["shop/web-feat-cart"]
    lookup, create = fake.kc()
    assert lookup[:2] == ["get", "clients"]
    assert "clientId=web-feat-cart" in lookup and lookup[lookup.index("-r") + 1] == "shop"
    assert create[:2] == ["create", "clients"]
    assert create[create.index("-r") + 1] == "shop"
    sets = [create[i + 1] for i, a in enumerate(create) if a == "-s"]
    assert "clientId=web-feat-cart" in sets
    assert "publicClient=true" in sets
    assert "standardFlowEnabled=true" in sets
    assert f"redirectUris={json.dumps(rendered())}" in sets
    assert f"webOrigins={json.dumps(list(URLS))}" in sets
    assert not any("pkce" in s for s in sets)


def test_create_updates_an_existing_client(fake: FakeProc) -> None:
    fake.on("get clients", clients((UUID, "web-feat-cart", ["https://old/*"])))
    provider().create(ctx())
    _, update = fake.kc()
    assert update[:2] == ["update", f"clients/{UUID}"]
    assert update[update.index("-r") + 1] == "shop"
    sets = [update[i + 1] for i, a in enumerate(update) if a == "-s"]
    assert f"redirectUris={json.dumps(rendered())}" in sets
    assert not any(c[:2] == ["create", "clients"] for c in fake.kc())


def test_create_matches_client_id_exactly_not_by_prefix(fake: FakeProc) -> None:
    fake.on("get clients", clients(("other-id", "web-feat-cart-x", ["https://x/*"])))
    provider().create(ctx())
    assert fake.kc()[1][:2] == ["create", "clients"]


def test_create_without_keycloak_section_fails() -> None:
    with pytest.raises(WtError, match=r"shared\.keycloak"):
        provider(with_keycloak=False).create(ctx())


def test_verify_ok(fake: FakeProc) -> None:
    fake.on("get clients", clients((UUID, "web-feat-cart", list(reversed(rendered())))))
    check = provider().verify(ctx())
    assert check.ok and not check.warnings
    assert any(c[:2] == ["get", "realms/shop"] for c in fake.kc())


def test_verify_detects_redirect_uri_mismatch(fake: FakeProc) -> None:
    fake.on("get clients", clients((UUID, "web-feat-cart", ["https://old/*"])))
    check = provider().verify(ctx())
    assert not check.ok and "redirect" in check.detail


def test_verify_detects_missing_client(fake: FakeProc) -> None:
    fake.on("get clients", clients())
    check = provider().verify(ctx())
    assert not check.ok and "web-feat-cart" in check.detail


def test_verify_detects_unanswering_realm(fake: FakeProc) -> None:
    fake.on("get realms/shop", proc.Result(1, "", "Resource not found"))
    check = provider().verify(ctx())
    assert not check.ok and "shop" in check.detail


def test_verify_warns_when_no_routed_urls(fake: FakeProc) -> None:
    fake.on("get clients", clients((UUID, "web-feat-cart", [])))
    check = provider().verify(ctx(urls=()))
    assert check.ok and check.warnings


def test_destroy_deletes_only_the_exact_client(fake: FakeProc) -> None:
    fake.on(
        "get clients",
        clients(
            ("id-x", "web-feat-cart-x", ["https://x/*"]),
            (UUID, "web-feat-cart", ["https://y/*"]),
        ),
    )
    provider().destroy(ctx())
    deletes = [c for c in fake.kc() if c[0] == "delete"]
    assert deletes == [["delete", f"clients/{UUID}", "-r", "shop"]]


def test_destroy_never_touches_a_longer_branch(fake: FakeProc) -> None:
    fake.on("get clients", clients(("id-x", "web-feat-x", ["https://x/*"])))
    provider().destroy(ctx("feat"))
    assert not any(c[0] == "delete" for c in fake.kc())


def test_destroy_is_safe_when_client_or_realm_is_missing(fake: FakeProc) -> None:
    fake.on("get clients", proc.Result(1, "", "Realm not found"))
    provider().destroy(ctx())
    assert not any(c[0] == "delete" for c in fake.kc())


def test_destroy_uses_recorded_names_when_config_is_gone(fake: FakeProc) -> None:
    c = ctx()
    with c.state.edit() as state:
        state.branches["feat/cart"] = BranchState(
            branch="feat/cart", slug="feat-cart", providers={"keycloak": ["old/web-feat-cart"]}
        )
    fake.on("get clients", clients((UUID, "web-feat-cart", [])))
    provider(with_keycloak=False).destroy(c)
    kc = fake.kc()
    assert kc[0][kc[0].index("-r") + 1] == "old"
    assert ["delete", f"clients/{UUID}", "-r", "old"] in kc


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
        "client_id": "web-feat-cart",
    }
    assert p.template_vars(ctx(), "compose") == {
        "keycloak": {**base, "internal_url": "http://wt-keycloak:8080"}
    }
    assert p.template_vars(ctx(), "run") == {
        "keycloak": {**base, "internal_url": "http://localhost:58080"}
    }
