import pytest

from wt.errors import WtError
from wt.naming import (
    MAX_SLUG,
    PORT_BASE,
    PORT_STRIDE,
    Names,
    fit,
    host_port,
    slugify,
)


@pytest.fixture(autouse=True)
def private_port_base() -> None:
    """Override the conftest fixture: the formula tests use the real PORT_BASE."""


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


@pytest.mark.parametrize("branch", ["feat/x", "feat-x", "Feat_X"])
def test_branches_that_differ_only_in_separators_share_a_slug(branch: str) -> None:
    assert slugify(branch) == "feat-x"


def test_fit_long_name_has_hash_suffix() -> None:
    result = fit("a" * 100)
    assert len(result) == 63
    prefix, sep, suffix = result.rpartition("-")
    assert sep == "-"
    assert len(suffix) == 6
    assert all(c in "0123456789abcdef" for c in suffix)
    assert prefix == "a" * 56
    assert fit("a" * 100) == result  # stable across calls


def test_fit_differs_for_long_names_that_differ_at_the_end() -> None:
    assert fit("a" * 99 + "b") != fit("a" * 100)


def test_slugify_cuts_a_60_char_branch_to_max_slug() -> None:
    slug = slugify("feat/" + "b" * 55)
    assert len(slug) <= MAX_SLUG


def test_host_port_formula() -> None:
    assert (PORT_BASE, PORT_STRIDE) == (10000, 100)
    assert host_port(0, 0) == 10000
    assert host_port(1, 1) == 10101
    assert host_port(3, 99) == 10399
    assert host_port(554, 99) == 65499


def test_host_port_limits() -> None:
    with pytest.raises(WtError, match="service index"):
        host_port(0, 100)
    with pytest.raises(WtError, match="65535"):
        host_port(555, 99)
    with pytest.raises(WtError):
        host_port(-1, 0)
    with pytest.raises(WtError):
        host_port(0, -1)
