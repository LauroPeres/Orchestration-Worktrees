#!/usr/bin/env bash
# Usage: bootstrap.sh <dir>
# Creates <dir>/notes-back and <dir>/notes-front as git repos on main (one commit each), a
# feat/tags branch in notes-back with a visible change, <dir>/catalog.yaml and
# <dir>/worktree.yaml. Never touches ~/.wt/catalog.yaml.
set -eu

if [ $# -ne 1 ]; then
  echo "usage: $0 <dir>" >&2
  exit 2
fi

here="$(cd "$(dirname "$0")" && pwd)"
dir="$1"

if [ -e "$dir" ] && [ -n "$(ls -A "$dir")" ]; then
  echo "refusing to use non-empty directory: $dir" >&2
  exit 1
fi
mkdir -p "$dir"
dir="$(cd "$dir" && pwd)"

git_in() {
  repo="$1"
  shift
  git -C "$repo" -c user.name=notes -c user.email=notes@example.invalid "$@"
}

for pair in back:notes-back front:notes-front; do
  src="${pair%%:*}"
  repo="$dir/${pair##*:}"
  mkdir -p "$repo"
  cp -R "$here/$src/." "$repo/"
  git init -q -b main "$repo"
  git_in "$repo" add -A
  git_in "$repo" commit -q -m "Initial $src"
done

git_in "$dir/notes-back" checkout -q -b feat/tags
cp "$here/feat-tags/app.py" "$here/feat-tags/schema.sql" "$dir/notes-back/"
git_in "$dir/notes-back" add -A
git_in "$dir/notes-back" commit -q -m "Add tags to notes"
git_in "$dir/notes-back" checkout -q main

cp "$here/catalog.yaml" "$here/worktree.yaml" "$dir/"
echo "created $dir (notes-back on main and feat/tags, notes-front on main)"
