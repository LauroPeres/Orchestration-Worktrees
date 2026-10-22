import pytest

from wt.errors import WtError
from wt.naming import AUTO_PORT_RANGE, MAX_SLUG, Names, auto_port, fit, slugify


@pytest.mark.parametrize(
    ("branch", "slug"),
    [
        ("main", "main"),
        ("feat/cart", "feat-cart"),
        ("Feat/Cart_V2", "feat-cart-v2"),
        ("fix//double--dash", "fix-double-dash"),
        ("-weird-", "weird"),
        ("release/1.2.3", "release-1-2-3"),
    ],
)
def test_slugify(branch: str, slug: str) -> None:
    assert slugify(branch) == slug


def test_slugify_rejects_empty() -> None:
    with pytest.raises(WtError):
        slugify("///")


def test_long_slugs_are_cut_and_stay_unique() -> None:
    a = slugify("feature/" + "a" * 80)
    b = slugify("feature/" + "a" * 79 + "b")
    assert len(a) <= MAX_SLUG and len(b) <= MAX_SLUG
    assert a != b
    assert slugify("feature/" + "a" * 80) == a  # deterministic


def test_fit_keeps_short_names() -> None:
    assert fit("short") == "short"
    assert len(fit("x" * 100)) == 63


def test_names() -> None:
    n = Names("shop", "feat/cart", "shop.localhost")
    assert n.slug == "feat-cart"
    assert n.ident == "feat_cart"
    assert n.branch_domain == "feat-cart.shop.localhost"
    assert n.host("") == "feat-cart.shop.localhost"
    assert n.host("api") == "api.feat-cart.shop.localhost"
    assert n.compose_project("api") == "wt-shop-feat-cart-api"
    assert n.network_alias("api") == "wt-shop-feat-cart-api"
    assert n.router("api") == "shop--feat-cart--api"


def test_auto_port_is_stable_and_in_range() -> None:
    port = auto_port("shop", "feat/cart", "web")
    assert port == auto_port("shop", "feat/cart", "web")
    assert AUTO_PORT_RANGE[0] <= port < AUTO_PORT_RANGE[1]
    assert port != auto_port("shop", "feat/other", "web")
