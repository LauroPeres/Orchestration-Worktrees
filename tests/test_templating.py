import subprocess
from pathlib import Path

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
    with pytest.raises(WtError, match=r"kafka provider arrives in v0\.4"):
        render("{{ kafka.topic('orders') }}", {}, "env.TOPIC")


def test_sandbox_blocks_internals() -> None:
    with pytest.raises(WtError):
        render("{{ ''.__class__.__mro__ }}", {}, "env.A")


def test_registered_provider_loses_the_upcoming_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import FakeProvider
    from wt import providers

    monkeypatch.setitem(providers.REGISTRY, "postgres", lambda cfg, log: FakeProvider([]))
    with pytest.raises(WtError) as exc:
        render("{{ postgres.url('shop') }}", {}, "env.A")
    assert "arrives in" not in str(exc.value)


HOSTILE = ["x;touch {marker}", "$(touch {marker})", "`touch {marker}`", "a&touch {marker}", "it's"]


@pytest.mark.parametrize("branch", HOSTILE)
def test_run_quoting_keeps_hostile_value_one_literal_argument(branch: str, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    branch = branch.format(marker=marker)
    cmd = render("printf %s {{ branch }}", {"branch": branch}, "run", quote=True)
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=True)
    assert out.stdout == branch
    assert not marker.exists()
    assert render("{{ branch }}", {"branch": branch}, "env.A") == branch


def test_run_quoting_leaves_safe_values_and_numbers_alone() -> None:
    ctx = {"port": 8080, "url": "https://a.localhost"}
    assert render("serve --port {{ port }} {{ url }}", ctx, "run", quote=True) == (
        "serve --port 8080 https://a.localhost"
    )
