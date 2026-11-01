#!/usr/bin/env bash
# Usage: bootstrap.sh <dir>
# Creates <dir>/shop-api and <dir>/shop-web as git repos on main (one commit each), a
# feat/cart branch in shop-api with a visible change, <dir>/catalog.yaml (use it with
# WT_CATALOG=<dir>/catalog.yaml) and <dir>/legacy/worktree.yaml (a v0.5 file for `wt migrate`).
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
  git -C "$repo" -c user.name=shop -c user.email=shop@example.invalid "$@"
}

for pair in api:shop-api web:shop-web; do
  src="${pair%%:*}"
  repo="$dir/${pair##*:}"
  mkdir -p "$repo"
  cp -R "$here/$src/." "$repo/"
  git init -q -b main "$repo"
  git_in "$repo" add -A
  git_in "$repo" commit -q -m "Initial $src"
done

git_in "$dir/shop-api" checkout -q -b feat/cart
cp "$here/feat-cart/app.py" "$here/feat-cart/schema.sql" "$dir/shop-api/"
git_in "$dir/shop-api" add -A
git_in "$dir/shop-api" commit -q -m "Add item quantity"
git_in "$dir/shop-api" checkout -q main

cp "$here/catalog.yaml" "$dir/catalog.yaml"
mkdir -p "$dir/legacy"
cp "$here/legacy/worktree.yaml" "$dir/legacy/worktree.yaml"
echo "created $dir (shop-api on main and feat/cart, shop-web on main)"
