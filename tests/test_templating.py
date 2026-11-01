import subprocess
from pathlib import Path

import pytest

from wt.errors import WtError
from wt.naming import service_ref
from wt.templating import render


def test_renders_values() -> None:
    ctx = {"url": "https://x.localhost", "services": {"api": {"url": "https://api.x"}}}
    assert render("{{ url }}/cb", ctx, "env.A") == "https://x.localhost/cb"
    assert render("{{ services.api.url }}", ctx, "env.A") == "https://api.x"
    assert render("plain", ctx, "env.A") == "plain"


def test_unknown_name_is_an_error() -> None:
    with pytest.raises(WtError, match=r"env\.A: .*'nope' is undefined"):
        render("{{ nope }}", {"url": ""}, "env.A")


def test_sandbox_blocks_internals() -> None:
    with pytest.raises(WtError):
        render("{{ ''.__class__.__mro__ }}", {}, "env.A")


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


HYPHEN_CTX = {"services": {"music-back": {"url": "https://api.x", "port": 8000, "host": "api.x"}}}


@pytest.mark.parametrize(
    ("attr", "expected"), [("url", "https://api.x"), ("port", "8000"), ("host", "api.x")]
)
def test_subscript_form_reaches_a_hyphenated_service(attr: str, expected: str) -> None:
    assert render(f"{{{{ services['music-back'].{attr} }}}}", HYPHEN_CTX, "env.A") == expected


def test_service_ref_output_renders() -> None:
    assert render(service_ref("music-back", "port"), HYPHEN_CTX, "env.A") == "8000"


def test_service_ref_survives_quotes_in_the_name() -> None:
    ctx = {"services": {"a'b\\c": {"url": "ok"}}}
    assert render(service_ref("a'b\\c", "url"), ctx, "env.A") == "ok"


def test_dot_form_of_a_hyphenated_name_points_at_the_subscript_form() -> None:
    with pytest.raises(WtError, match=r"use services\['music-back'\]\.url"):
        render("{{ services.music-back.url }}", HYPHEN_CTX, "env.A")


def test_no_subscript_hint_without_a_hyphenated_reference() -> None:
    with pytest.raises(WtError) as exc:
        render("{{ nope }}", HYPHEN_CTX, "env.A")
    assert "use services[" not in str(exc.value)
