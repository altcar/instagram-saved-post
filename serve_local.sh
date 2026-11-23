#!/usr/bin/env bash
set -u
REPO="/mnt/friendly/save/devdrive/_organized/archives/instagram save"
cd "$REPO" || exit 1
mkdir -p /tmp/opencode
while true; do
  if ! curl -sf http://127.0.0.1:8770/ >/dev/null 2>&1; then
    pgrep -f '[w]ebapp.py.*--port 8770' >/dev/null || \
      nohup /tmp/opencode/igvenv/bin/python webapp.py --db data/archive.db \
        --media data/posts --cache data/.cache --port 8770 \
        >>/tmp/opencode/webapp.log 2>&1 &
  fi
  if ! curl -sf http://127.0.0.1:8766/ >/dev/null 2>&1; then
    pgrep -f '[p]rogress_gui.py.*--port 8766' >/dev/null || \
      nohup python3 progress_gui.py --data data/posts \
        --log /tmp/opencode/incr2.log --total 12028 --port 8766 \
        --db data/archive.db --comments-log /tmp/opencode/comments_all.log \
        >>/tmp/opencode/progress_gui.log 2>&1 &
  fi
  if ! curl -sf http://127.0.0.1:8891/health >/dev/null 2>&1; then
    pgrep -f '[h]ub/server.py' >/dev/null || \
      nohup python3 hub/server.py \
        >>/tmp/opencode/hub.log 2>&1 &
  fi
  if ! curl -sf http://127.0.0.1:8799/ >/dev/null 2>&1; then
    pgrep -f '[u]vicorn server:app.*--port 8799' >/dev/null ||       (cd /home/over/opencode-linux-x64/tg-archiver &&        nohup ./.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8799 \
         >>/tmp/opencode/tg-8799.log 2>&1 &)
  fi
  if ! curl -sf http://127.0.0.1:8892/gallery/ >/dev/null 2>&1; then
    pgrep -f '[w]ebapp.py.*--port 8892' >/dev/null || \
      nohup /tmp/opencode/igvenv/bin/python webapp.py --db data/archive.db \
        --media data/posts --cache data/.cache --port 8892 \
        >>/tmp/opencode/webapp-8892.log 2>&1 &
  fi
  sleep 20
done
