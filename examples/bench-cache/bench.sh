#!/usr/bin/env bash
# Measure `wt up` for a branch whose `setup:` is `npm ci`, with and without `setup_cache: npm`.
#
#   examples/bench-cache/bench.sh <npm-project-dir>
#
# Only package.json and package-lock.json are copied out of <npm-project-dir>; the project itself
# is never modified or run. Needs docker, git, node, npm and uv, and network access for `npm ci`.
# Everything lives in a temp folder with its own WT_HOME, WT_INSTANCE, catalog and ports; the
# real ~/.wt, the shared Traefik and any other stack are never touched.
set -euo pipefail

if [[ $# -ne 1 || ! -f "$1/package.json" || ! -f "$1/package-lock.json" ]]; then
  echo "usage: $0 <npm-project-dir>   (needs package.json and package-lock.json)" >&2
  exit 2
fi
SOURCE=$(cd "$1" && pwd)
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)

# Resolve the tools before PATH is trimmed (no mkcert: wt falls back to a self-signed cert).
for tool in uv git node npm docker; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done
TOOL_DIRS=$(for tool in uv git node npm docker; do dirname "$(command -v "$tool")"; done | sort -u | paste -sd: -)
export PATH="$TOOL_DIRS:/usr/bin:/bin"

WORK=$(mktemp -d)
INSTANCES=()

wt() { uv run --project "$ROOT" python -m wt "$@"; }

cleanup() {
  local instance
  for instance in "${INSTANCES[@]:-}"; do
    [[ -n "$instance" ]] || continue
    (
      export WT_INSTANCE="$instance" WT_HOME="$WORK/$instance/home" WT_CATALOG="$WORK/$instance/catalog.yaml"
      for branch in bench/prime bench/first bench/second; do
        wt down "$branch" --force >/dev/null 2>&1 || true
      done
      docker rm -f -v "$instance-traefik" >/dev/null 2>&1 || true
      docker network rm "$instance" >/dev/null 2>&1 || true
    )
  done
  rm -rf "$WORK"
}
trap cleanup EXIT

now() { date +%s.%N; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", b - a }'; }

# One variant = one isolated instance. $1 = label, $2 = "cache" or "plain".
bench() {
  local label=$1 mode=$2 instance dir port_note setup_cache=""
  instance="wtbench$$${label}"
  INSTANCES+=("$instance")
  dir="$WORK/$instance"
  mkdir -p "$dir/home" "$dir/repo"
  cp "$SOURCE/package.json" "$SOURCE/package-lock.json" "$dir/repo/"
  git -C "$dir/repo" init -q -b main
  git -C "$dir/repo" add .
  git -C "$dir/repo" -c user.name=bench -c user.email=bench@localhost commit -qm project
  [[ "$mode" == cache ]] && setup_cache="    setup_cache: npm"

  local server="node -e \"require('http').createServer((q,r)=>r.end('ok')).listen(+process.argv[1])\" {{ port }}"
  cat >"$dir/catalog.yaml" <<YAML
project: $instance
domain: $instance.localhost
repos:
  app: $dir/repo
services:
  app:
    repo: app
    setup: npm ci --no-audit --no-fund
$setup_cache
    run: $server
    port: auto
    subdomain: app
  probe:
    repo: app
    run: $server
    port: auto
    subdomain: probe
YAML

  export WT_INSTANCE="$instance" WT_HOME="$dir/home" WT_CATALOG="$dir/catalog.yaml"
  export WT_HTTP_PORT WT_HTTPS_PORT
  WT_HTTP_PORT=$(free_port) WT_HTTPS_PORT=$(free_port)
  # A cold npm download cache for the plain variant too, so both start from nothing.
  [[ "$mode" == plain ]] && export npm_config_cache="$dir/npm-cache"
  (cd "$ROOT" && WT_HOME="$dir/home" PYTHONPATH="$ROOT" \
    uv run --project "$ROOT" python -c \
    "from pathlib import Path; from tests.integration.isolation import reserve_free_indexes; reserve_free_indexes(Path('$dir/home'))")

  wt up bench/prime --uses probe --json >/dev/null  # starts Traefik so it is not part of the timings
  local t0 t1 t2
  t0=$(now); wt up bench/first --uses app --json >/dev/null; t1=$(now)
  wt up bench/second --uses app --json >/dev/null; t2=$(now)
  printf '%s first branch: %ss\n%s second branch: %ss\n' \
    "$label" "$(elapsed "$t0" "$t1")" "$label" "$(elapsed "$t1" "$t2")"
  local modules
  modules=$(find "$WT_HOME" -type d -name node_modules -path '*bench-second*' -prune | head -1)
  PACKAGES=$(cd "$(dirname "$modules")" && npm ls --all --parseable 2>/dev/null | tail -n +2 | wc -l)
  [[ "$mode" == plain ]] && unset npm_config_cache
  return 0
}

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }

echo "node $(node --version), npm $(npm --version)"
bench cache cache
bench plain plain
echo "node_modules (second branch): $PACKAGES installed packages (npm ls --all)"
