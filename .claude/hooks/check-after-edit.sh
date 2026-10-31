#!/usr/bin/env bash
# PostToolUse (Edit/Write/MultiEdit) for Python files: lint (with the complexity limits) and check
# the layer contracts right after the edit. Formatting is done by the code-framework hook.
# A violation exits 2, so Claude sees it and must fix it before going on.
set -uo pipefail
file=$(python3 -c 'import json,sys; print((json.load(sys.stdin).get("tool_input") or {}).get("file_path",""))')
case "$file" in *.py) ;; *) exit 0 ;; esac
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
[ -x .venv/bin/ruff ] || exit 0
[ -f "$file" ] || exit 0

errors=$(.venv/bin/ruff check --quiet "$file" 2>&1)
if [[ "$file" == */src/wt/* || "$file" == src/wt/* ]] && [ -x .venv/bin/lint-imports ]; then
  layers=$(.venv/bin/lint-imports --no-cache 2>&1) || errors+=$'\n'"$(grep -A12 'BROKEN\|not allowed' <<<"$layers" | head -40)"
fi
if [ -n "${errors//[$'\n ']/}" ]; then
  echo "Checks failed after editing $file — fix before continuing:" >&2
  echo "$errors" >&2
  exit 2
fi
exit 0
