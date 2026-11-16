"""The shop web page: its template, branch accent colors and the server that renders it."""

import importlib.util
import json
import re
import threading
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

SERVER = Path(__file__).resolve().parent.parent / "examples" / "shop" / "web" / "server.py"


@pytest.fixture(scope="module")
def web() -> ModuleType:
    spec = importlib.util.spec_from_file_location("shop_web_server", SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hue(color: str) -> int:
    match = re.fullmatch(r"hsl\((\d+) \d+% \d+%\)", color)
    assert match, color
    return int(match.group(1))


def gap(a: int, b: int) -> int:
    """Shortest distance between two hues on the color wheel."""
    return min((a - b) % 360, (b - a) % 360)


def test_main_has_a_fixed_color(web: ModuleType) -> None:
    assert web.accent_color("main") == web.accent_color("main")
    assert hue(web.accent_color("main")) == web.MAIN_HUE


def test_a_branch_keeps_its_color_between_runs(web: ModuleType) -> None:
    # crc32, not hash(): Python randomizes str hashes per process, so a screenshot would drift.
    assert web.accent_color("feat/cart") == "hsl(342 70% 40%)"


def test_feat_cart_looks_clearly_different_from_main(web: ModuleType) -> None:
    assert gap(hue(web.accent_color("feat/cart")), hue(web.accent_color("main"))) >= 40


def test_no_branch_name_gets_main_s_color(web: ModuleType) -> None:
    names = [
        "feat/cart",
        "",
        "x",
        "main2",
        "Main",
        "feat/Cart_v2",
        "é",
        *[f"b{i}" for i in range(500)],
    ]
    gaps = {gap(hue(web.accent_color(name)), web.MAIN_HUE) for name in names}

    assert min(gaps) >= web.MIN_HUE_GAP


def test_different_branches_spread_over_the_wheel(web: ModuleType) -> None:
    hues = {hue(web.accent_color(f"feat/{n}")) for n in ("cart", "login", "search", "pay", "ui")}

    assert len(hues) >= 4


def render(web: ModuleType, branch: str = "feat/cart", sign_in: str = "") -> str:
    page: str = web.render_page({"branch": branch, "apiUrl": "https://api.example.test"}, sign_in)
    return page


def test_the_page_has_the_isolated_panel_and_the_old_parts(web: ModuleType) -> None:
    page = render(web)

    assert "Isolated for this branch" in page
    for element in ("database", "redis-prefix", "kafka-topic", "kafka-group", "events", "recent"):
        assert f'id="{element}"' in page
    assert "Events received" in page
    for element in ("branch", "items", "form", "name"):
        assert f'id="{element}"' in page


def test_the_page_polls_info_every_two_seconds(web: ModuleType) -> None:
    page = render(web)

    assert 'fetch(cfg.apiUrl + "/info")' in page
    assert "setInterval(refreshInfo, 2000)" in page


def test_the_page_uses_the_system_font_and_nothing_from_the_network(web: ModuleType) -> None:
    page = render(web)

    assert "system-ui" in page
    assert not re.search(r"""(src|href)=["']https?:""", page)
    assert "@import" not in page
    assert "url(" not in page


def test_no_placeholder_is_left_in_the_page(web: ModuleType) -> None:
    assert not re.search(r"__[A-Z_]+__", render(web))


def test_the_accent_and_the_title_follow_the_branch(web: ModuleType) -> None:
    assert f"--accent: {web.accent_color('feat/cart')};" in render(web, "feat/cart")
    assert f"--accent: {web.accent_color('main')};" in render(web, "main")
    assert "<title>shop on main</title>" in render(web, "main")


def test_a_hostile_branch_name_cannot_break_out_of_the_page(web: ModuleType) -> None:
    page = render(web, "</script><b>x</b>__CONFIG__")

    assert "</script><b>" not in page
    assert "<title>shop on &lt;/script&gt;&lt;b&gt;x&lt;/b&gt;__CONFIG__</title>" in page
    config = re.search(r"const cfg = (.*);\n", page)
    assert config is not None
    assert json.loads(config.group(1))["branch"] == "</script><b>x</b>__CONFIG__"


def test_the_sign_in_link_still_sits_on_the_page(web: ModuleType) -> None:
    link = '<p id="sign-in-line"><a id="sign-in" href="https://kc.example/auth">Sign in</a></p>'

    assert link in render(web, sign_in=link)


@pytest.fixture
def served(web: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("BRANCH", "feat/cart")
    monkeypatch.setenv("API_URL", "https://api.feat-cart.shop.localhost")
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_the_server_answers_with_the_branch_page(served: str) -> None:
    with urllib.request.urlopen(served + "/", timeout=10) as resp:
        body = resp.read().decode()
        content_type = resp.headers["Content-Type"]

    assert content_type == "text/html; charset=utf-8"
    assert '"apiUrl": "https://api.feat-cart.shop.localhost"' in body
    assert '"branch": "feat/cart"' in body
