import pytest

from wt.errors import WtError
from wt.templating import render


def test_renders_values() -> None:
    ctx = {"url": "https://x.localhost", "services": {"api": {"url": "https://api.x"}}}
    assert render("{{ url }}/cb", ctx, "env.A") == "https://x.localhost/cb"
    assert render("{{ services.api.url }}", ctx, "env.A") == "https://api.x"
    assert render("plain", ctx, "env.A") == "plain"


def test_unknown_name_is_an_error() -> None:
    with pytest.raises(WtError, match=r"env\.A: .*'nope' is undefined"):
        render("{{ nope }}", {"url": ""}, "env.A")


def test_upcoming_provider_hint() -> None:
    with pytest.raises(WtError, match=r"postgres provider arrives in v0\.2"):
        render("{{ postgres.url('shop') }}", {}, "env.DATABASE_URL")


def test_sandbox_blocks_internals() -> None:
    with pytest.raises(WtError):
        render("{{ ''.__class__.__mro__ }}", {}, "env.A")


def test_registered_provider_loses_the_upcoming_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import FakeProvider
    from wt import providers

    monkeypatch.setitem(providers.REGISTRY, "postgres", lambda cfg: FakeProvider([]))
    with pytest.raises(WtError) as exc:
        render("{{ postgres.url('shop') }}", {}, "env.A")
    assert "arrives in" not in str(exc.value)
