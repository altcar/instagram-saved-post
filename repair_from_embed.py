#!/usr/bin/env python3
"""Repair owners, captions and covers from Instagram's public embed page.

``/p/<shortcode>/embed/captioned/`` is public HTML (no API/session), and it
contains the real username, caption and first media image/poster. It is much
lighter than rendering every post with Playwright and is perfect for legacy
rows whose owner/caption/cover are missing.

For deleted posts the embed route redirects to the owner profile, so the real
handle is still recoverable.

    python repair_from_embed.py --dry-run
    python repair_from_embed.py --workers 8
"""

from __future__ import annotations

import argparse
import html
import io
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from PIL import Image

import archive_db as adb
from repair_archive import apply_report, invalidate_thumbs, row_gaps

USER_RE = re.compile(r'<span class="UsernameText">([^<]+)</span>')
CAPTION_RE = re.compile(
    r'<div class="Caption">.*?<a class="CaptionUsername".*?>(.*?)</a>(.*?)</div>',
    re.S,
)
IMAGE_RE = re.compile(
    r'<img class="EmbeddedMediaImage"[^>]+src="([^"]+)"', re.S
)
PROFILE_RE = re.compile(r"^https?://www\.instagram\.com/([^/?#]+)/?$")
LOCK = threading.Lock()


def clean_caption(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(
        r"\s*View all [\d,]+ (?:comments?|replies)\s*$", "", value, flags=re.I
    )
    return "\n".join(line.rstrip() for line in value.strip().splitlines()).strip()


def to_jpeg(data: bytes) -> Optional[bytes]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, "JPEG", quality=90)
            return out.getvalue()
    except Exception:
        return None


def fetch_embed(
    session: requests.Session, shortcode: str
) -> Tuple[str, Optional[int], Optional[str], Optional[str], Optional[str]]:
    try:
        response = session.get(
            f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
            allow_redirects=False,
            timeout=30,
        )
    except requests.RequestException:
        return shortcode, None, None, None, None
    if response.status_code in (301, 302, 303, 307, 308):
        match = PROFILE_RE.match(response.headers.get("location") or "")
        owner = match.group(1) if match else None
        return shortcode, response.status_code, owner, None, None
    if response.status_code != 200:
        return shortcode, response.status_code, None, None, None
    text = response.text
    owner_match = USER_RE.search(text)
    owner = html.unescape(owner_match.group(1)) if owner_match else None
    caption = None
    caption_match = CAPTION_RE.search(text)
    if caption_match:
        caption = clean_caption(caption_match.group(2))
    image_match = IMAGE_RE.search(text)
    image = html.unescape(image_match.group(1)) if image_match else None
    return shortcode, response.status_code, owner, caption, image


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair from public embed pages")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--cache", default="data/.cache")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    media_root = Path(args.posts)
    conn = adb.connect(Path(args.db))
    conn.row_factory = sqlite3.Row
    media_by_id: Dict[str, list] = {}
    for media in conn.execute("SELECT media_id, path, kind FROM post_media"):
        media_by_id.setdefault(media["media_id"], []).append(
            (media["path"], media["kind"])
        )
    tasks = []
    for row in conn.execute("SELECT * FROM posts ORDER BY sort_order, id"):
        gaps = row_gaps(row, media_root, media_by_id)
        if "owner" in gaps or "caption" in gaps or "cover" in gaps:
            tasks.append((row, gaps))
    if args.limit:
        tasks = tasks[: args.limit]
    print(f"{len(tasks)} embed repairs pending")
    if args.dry_run or not tasks:
        conn.close()
        return

    local = threading.local()

    def session() -> requests.Session:
        if not hasattr(local, "session"):
            value = requests.Session()
            value.headers.update(
                {
                    # Instagram serves the public embed only to a plain
                    # browser-like Accept/UA pair; the full Chrome UA gets
                    # the app shell instead.
                    "User-Agent": "Mozilla/5.0",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Accept-Language": "en-US,en;q=0.5",
                    "Referer": "https://www.instagram.com/",
                }
            )
            local.session = value
        return local.session

    def work(row: sqlite3.Row, gaps: list):
        code, status, owner, caption, image = fetch_embed(session(), row["shortcode"])
        report: Dict[str, Any] = {
            "id": row["id"],
            "shortcode": code,
            "gaps": gaps,
            "ok": False,
            "status": status,
            "new_media_id": row["media_id"],
            "new_taken": row["taken_at"],
            "new_likes": row["likes"] or 0,
            "new_video": bool(row["is_video"]),
            "new_typename": row["typename"],
            "new_location": row["location"],
            "new_owner": row["owner_username"],
            "new_owner_id": row["owner_id"],
            "new_caption": row["caption"] or "",
            "new_cover": row["cover"],
        }
        if owner:
            report["new_owner"] = row["owner_username"] or owner
        if caption and not (row["caption"] or "").strip():
            report["new_caption"] = caption
        if image and (
            not row["cover"] or not (media_root / row["cover"]).is_file()
        ):
            try:
                response = session().get(image, timeout=45)
                if response.status_code == 200 and response.content:
                    data = response.content
                    if not data.startswith(b"\xff\xd8"):
                        data = to_jpeg(data) or data
                    folder = media_root / row["folder"]
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / "cover.jpg").write_bytes(data)
                    report["new_cover"] = f"{folder.name}/cover.jpg"
            except (requests.RequestException, OSError):
                pass
        report["new_hashtags"] = re.findall(r"#(\w+)", report["new_caption"] or "")
        has_change = (
            report["new_owner"] != row["owner_username"]
            or report["new_caption"] != (row["caption"] or "")
            or report["new_cover"] != row["cover"]
        )
        report["ok"] = bool(has_change and (owner or caption or report["new_cover"]))
        return row, report

    done = 0
    ok = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(work, row, gaps) for row, gaps in tasks]
        for future in as_completed(futures):
            row, report = future.result()
            done += 1
            if report.get("ok"):
                ok += 1
                with LOCK:
                    apply_report(conn, row, report, media_root)
                invalidate_thumbs(Path(args.cache), row["id"])
            if done % 100 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} repaired={ok}", flush=True)
    conn.close()
    print(f"done: {ok}/{len(tasks)} repaired from embed pages")


if __name__ == "__main__":
    main()
