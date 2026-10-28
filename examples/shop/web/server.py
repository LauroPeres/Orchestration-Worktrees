"""Serve one HTML page that shows the branch and lists items fetched from the API."""

import argparse
import html
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>shop</title></head>
<body>
<h1>shop on <code id="branch"></code></h1>
%s
<ul id="items"></ul>
<form id="form">
  <input id="name" placeholder="item name" required>
  <button>Add</button>
</form>
<script>
const cfg = %s;
document.getElementById("branch").textContent = cfg.branch;
async function load() {
  const rows = await (await fetch(cfg.apiUrl + "/items")).json();
  document.getElementById("items").innerHTML = "";
  for (const row of rows) {
    const li = document.createElement("li");
    li.textContent = row.name + ("quantity" in row ? " x" + row.quantity : "");
    document.getElementById("items").appendChild(li);
  }
}
document.getElementById("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await fetch(cfg.apiUrl + "/items", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({name: document.getElementById("name").value}),
  });
  document.getElementById("name").value = "";
  load();
});
load();
</script>
</body>
</html>
"""


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
    return f'<p><a id="sign-in" href="{html.escape(f"{base}{path}?{query}")}">Sign in</a></p>'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        config = {"branch": os.environ.get("BRANCH", ""), "apiUrl": os.environ.get("API_URL", "")}
        # "<" is escaped so a value can never close the script tag.
        body = (PAGE % (sign_in_link(), json.dumps(config).replace("<", "\\u003c"))).encode()
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
