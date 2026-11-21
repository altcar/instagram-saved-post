#!/usr/bin/env bash
# Daily incremental archive update (cron-friendly).
set -uo pipefail

REPO="/mnt/friendly/save/devdrive/_organized/archives/instagram save"
PY="/mnt/friendly/igmlvenv/bin/python"
cd "$REPO" || exit 1
mkdir -p data/logs
LOG="data/logs/update-$(date +%F).log"
exec >>"$LOG" 2>&1

# cron has no desktop session; make the GNOME keyring reachable for Chrome cookies
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
export HF_HOME="/mnt/friendly/hf"
export TRANSFORMERS_CACHE="/mnt/friendly/hf"

echo "=== start $(date -Is) ==="

SID="$("$PY" chrome_cookies.py --domain instagram.com --json 2>/dev/null \
  | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("sessionid",""))')"
if [ -z "${SID:-}" ]; then
  echo "ERROR: no Instagram sessionid in Chrome cookies"
  exit 1
fi

run() {
  echo "--- $*"
  "$@" || echo "WARN: command failed: $*"
}

run "$PY" download_saved.py --username shalevfrfr --sessionid "$SID" \
  --out data/posts --json data/saved_posts.json \
  --db data/dedup.db --archive-db data/archive.db

run "$PY" fetch_raw.py --out data/raw --media data/posts --db data/archive.db --no-covers --fresh
run "$PY" fetch_covers.py --raw data/raw/items.jsonl --media data/posts --db data/archive.db --workers 8
run "$PY" archive_db.py import --db data/archive.db --json data/saved_posts.json --media data/posts
run "$PY" resort.py --db data/archive.db --raw data/raw/items.jsonl
run "$PY" archive_db.py export-json --db data/archive.db --json data/saved_posts.json --media data/posts
run "$PY" audit_media.py --db data/archive.db --posts data/posts --raw data/raw/items.jsonl
run "$PY" classify.py fast --db data/archive.db --media data/posts --raw data/raw/items.jsonl
run "$PY" classify.py clip --db data/archive.db --media data/posts --cache data/.cache --limit 100
run "$PY" classify.py fuse --db data/archive.db --media data/posts --cache data/.cache
run "$PY" archive_db.py write-files --db data/archive.db --media data/posts
run "$PY" comments_fetch.py --db data/archive.db --posts data/posts --missing --limit 100

echo "=== done $(date -Is) ==="
