"""Serve one HTML page that lists notes fetched from the back and shows which back it talks to."""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8"><title>notes</title>
<style>
#connection { border: 2px solid #2a7; padding: 0.5em; margin-bottom: 1em; }
#connection.mismatch { border-color: #c00; background: #fee; }
</style>
</head>
<body>
<h1>notes on <code id="branch"></code></h1>
<div id="connection">
  <strong>connection</strong>
  <div>API_URL: <code id="api"></code></div>
  <div>back: <span id="back">loading...</span></div>
</div>
<ul id="notes"></ul>
<form id="form">
  <input id="text" placeholder="note text" required>
  <input id="tags" placeholder="tags, comma separated">
  <button>Add</button>
</form>
<script>
const cfg = %s;
document.getElementById("branch").textContent = cfg.branch;
document.getElementById("api").textContent = cfg.apiUrl;
async function showInfo() {
  const box = document.getElementById("connection");
  const out = document.getElementById("back");
  try {
    const info = await (await fetch(cfg.apiUrl + "/info")).json();
    out.textContent = "branch " + info.branch + ", database " + info.database;
    const same = info.branch === cfg.branch;
    box.classList.toggle("mismatch", !same);
    if (!same) out.textContent += " (MISMATCH: front is on " + cfg.branch + ")";
  } catch (error) {
    out.textContent = "unreachable";
    box.classList.add("mismatch");
  }
}
async function load() {
  const rows = await (await fetch(cfg.apiUrl + "/notes")).json();
  document.getElementById("notes").innerHTML = "";
  for (const row of rows) {
    const li = document.createElement("li");
    const tags = row.tags && row.tags.length ? " [" + row.tags.join(", ") + "]" : "";
    li.textContent = row.text + tags;
    document.getElementById("notes").appendChild(li);
  }
}
document.getElementById("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {text: document.getElementById("text").value};
  const raw = document.getElementById("tags").value.split(",");
  const tags = raw.map((t) => t.trim()).filter(Boolean);
  if (tags.length) body.tags = tags;
  await fetch(cfg.apiUrl + "/notes", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify(body),
  });
  document.getElementById("text").value = "";
  document.getElementById("tags").value = "";
  load();
});
showInfo();
load();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        config = {"branch": os.environ.get("BRANCH", ""), "apiUrl": os.environ.get("API_URL", "")}
        # "<" is escaped so a value can never close the script tag.
        body = (PAGE % json.dumps(config).replace("<", "\\u003c")).encode()
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
