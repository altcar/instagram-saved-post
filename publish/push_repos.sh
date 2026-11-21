#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
for i in 1 2 3 4 5 6; do
  repo="$ROOT/repos/ig-archive-$i"
  echo "=== ig-archive-$i $(date -Is) ==="
  cd "$repo"
  if [ ! -d .git ]; then
    git init -q
    git checkout -q -b main
  fi
  git config user.name altcar
  git config user.email altcar@users.noreply.github.com
  git config http.postBuffer 524288000
  git add -A
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    git commit -q -m "Add Instagram archive media bucket $i"
  else
    git commit -q -m "Update Instagram archive media bucket $i" || true
  fi
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/altcar/ig-archive-$i.git"
  git push -u origin main
  echo "=== done ig-archive-$i $(date -Is) ==="
done
