"""Serve one HTML page: the branch, its items and what is isolated for it, all from the API."""

import argparse
import html
import json
import os
import re
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAIN_HUE = 215  # `main` always wears this blue
MIN_HUE_GAP = 40  # other branches stay at least this far from it, so main never looks like a branch
REFRESH_MS = 2000

# The page is a template next to this file; render_page fills its `__NAME__` placeholders.
PAGE = (Path(__file__).parent / "page.html").read_text()


def accent_color(branch: str) -> str:
    """The branch's page color: `main` is fixed, the others get a hue from a hash of the name."""
    if branch == "main":
        hue = MAIN_HUE
    else:
        free_arc = 360 - 2 * MIN_HUE_GAP
        hue = (MAIN_HUE + MIN_HUE_GAP + zlib.crc32(branch.encode()) % free_arc) % 360
    return f"hsl({hue} 70% 40%)"


def render_page(config: dict[str, str], sign_in: str) -> str:
    """The page for one branch; `config` reaches the script as JSON that cannot close its tag."""
    values = {
        "TITLE": html.escape(config["branch"]),
        "ACCENT": accent_color(config["branch"]),
        "SIGN_IN": sign_in,
        "REFRESH_MS": str(REFRESH_MS),
        "CONFIG": json.dumps(config).replace("<", "\\u003c"),
    }
    # One pass, so a value that looks like a placeholder is never substituted again.
    return re.sub(r"__([A-Z_]+)__", lambda match: values[match.group(1)], PAGE)


def sign_in_link() -> str:
    """A link to this branch's Keycloak client; empty unless every variable is set."""
    base = os.environ.get("KEYCLOAK_URL", "")
    realm = os.environ.get("KEYCLOAK_REALM", "")
    client_id = os.environ.get("KEYCLOAK_CLIENT_ID", "")
    public = os.environ.get("PUBLIC_URL", "")
    if not (base and realm and client_id and public):
        return ""
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": public + "/",
        }
    )
    path = f"/realms/{urllib.parse.quote(realm, safe='')}/protocol/openid-connect/auth"
    href = html.escape(f"{base}{path}?{query}")
    return f'<p id="sign-in-line"><a id="sign-in" href="{href}">Sign in</a></p>'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        config = {"branch": os.environ.get("BRANCH", ""), "apiUrl": os.environ.get("API_URL", "")}
        body = render_page(config, sign_in_link()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    # 0.0.0.0 so Traefik, running in Docker, can reach this host process.
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
