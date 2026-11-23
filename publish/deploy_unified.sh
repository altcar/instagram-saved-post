#!/usr/bin/env bash
# Rebuild the unified gallery and publish it to GitHub Pages.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 publish/export_static_overrides.py --state "${CLOUD_STATE:-data/cloud_state.json}" || true
python3 publish/build_unified.py
touch publish/site-unified/.nojekyll

DEPLOY="$ROOT/publish/.deploy-pages"
if [ ! -d "$DEPLOY/.git" ]; then
  rm -rf "$DEPLOY"
  git clone --branch gh-pages --depth 1 \
    https://github.com/altcar/instagram-saved-post.git "$DEPLOY"
fi

rsync -a --delete --exclude='.git' publish/site-unified/ "$DEPLOY"/
cd "$DEPLOY"
git fetch origin gh-pages || true
git add -A
git -c user.name=altcar -c user.email=altcar@users.noreply.github.com \
  commit -q -m "Deploy unified Saved Archive gallery" || true
# The Hugging Face uploader may force-push an orphan gh-pages commit while an
# upload finishes.  The freshly built tree is authoritative, so replace it.
git push --force origin gh-pages
echo "published https://altcar.github.io/instagram-saved-post/"
