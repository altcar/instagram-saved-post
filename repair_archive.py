#!/usr/bin/env python3
"""Backfill missing owners, captions, covers and media across the archive.

The web app masks legacy rows without owner metadata, but the archive should
still keep the real data. This utility scans every post for one of these gaps:

  * owner_username is empty
  * caption is empty
  * cover column/path is missing or the file is gone
  * no media rows exist, or a media path is gone

It asks Instagram's media-info endpoint for the authoritative post, updates the
row, downloads a real cover (including video posters), downloads missing media,
and invalidates stale thumbnail caches. It is safe to re-run.

Examples:
    python repair_archive.py --dry-run
    python repair_archive.py --limit 50 --workers 4
    python repair_archive.py --workers 4
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image

import archive_db as adb
from chrome_cookies import load_cookies
from download_saved import write_sidecars
from repair_posts import decode_shortcode, image_url, slides, video_url

INFO_URL = "https://www.instagram.com/api/v1/media/{media_id}/info/"
THUMB_RE = re.compile(r"^(?P<id>\d+)_\d+_\d+\.jpg$")


def session_factory() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
            ),
            "X-IG-App-ID": "936619743392459",
            "Accept": "*/*",
            "Referer": "https://www.instagram.com/",
            "Origin": "https://www.instagram.com",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    for name, value in load_cookies("instagram.com").items():
        session.cookies.set(name, value, domain=".instagram.com", path="/")
    return session


def image_candidates(node: Dict[str, Any]) -> List[str]:
    out = []
    for source in (node, *(node.get("carousel_media") or [])):
        url = image_url(source)
        if url:
            out.append(url)
    return list(dict.fromkeys(out))


def to_jpeg(data: bytes) -> Optional[bytes]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            buffer = io.BytesIO()
            img.save(buffer, "JPEG", quality=90)
            return buffer.getvalue()
    except Exception:
        return None


def download_bytes(session: requests.Session, url: str, timeout: int = 60) -> Optional[bytes]:
    try:
        response = session.get(url, timeout=timeout)
        if response.status_code != 200 or not response.content:
            return None
        return response.content
    except requests.RequestException:
        return None


def download_cover(
    session: requests.Session, item: Dict[str, Any], folder: Path
) -> Optional[str]:
    target = folder / "cover.jpg"
    if target.is_file() and target.stat().st_size > 1024:
        return f"{folder.name}/cover.jpg"
    for url in image_candidates(item):
        data = download_bytes(session, url)
        if not data:
            continue
        if not data.startswith(b"\xff\xd8"):
            data = to_jpeg(data) or data
        try:
            target.write_bytes(data)
        except OSError:
            return None
        if target.stat().st_size > 1024:
            return f"{folder.name}/cover.jpg"
    return None


def download_missing_media(
    session: requests.Session,
    item: Dict[str, Any],
    folder: Path,
    code: str,
    stamp: str,
    existing_paths: List[Path],
) -> int:
    wanted = slides(item)
    have = len(existing_paths)
    if have >= len(wanted):
        return 0
    folder.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for index in range(have, len(wanted)):
        media = wanted[index]
        suffix = ".mp4" if media["kind"] == "video" else ".jpg"
        target = folder / (
            f"{code}_{stamp}_{index + 1}{suffix}"
            if len(wanted) > 1
            else f"{code}_{stamp}{suffix}"
        )
        if target.is_file() and target.stat().st_size > 1024:
            continue
        data = download_bytes(session, media["url"], timeout=120)
        if not data or len(data) <= 1024:
            continue
        try:
            target.write_bytes(data)
            downloaded += 1
        except OSError:
            pass
        time.sleep(0.1)
    return downloaded


def row_gaps(
    row: sqlite3.Row,
    media_root: Path,
    media_by_id: Dict[str, List[Tuple[str, str]]],
) -> List[str]:
    gaps: List[str] = []
    if not (row["owner_username"] or "").strip():
        gaps.append("owner")
    if not (row["caption"] or "").strip():
        gaps.append("caption")
    cover = media_root / row["cover"] if row["cover"] else None
    if cover is None or not cover.is_file() or cover.stat().st_size <= 1024:
        gaps.append("cover")
    media = media_by_id.get(row["media_id"], [])
    if not media:
        gaps.append("media")
    else:
        missing = [
            path for path, _ in media
            if not (media_root / path).is_file()
            or (media_root / path).stat().st_size <= 1024
        ]
        if missing:
            gaps.append("media")
    return gaps


def fetch_item(session: requests.Session, media_id: str) -> Optional[Dict[str, Any]]:
    for attempt in range(3):
        try:
            response = session.get(INFO_URL.format(media_id=media_id), timeout=45)
        except requests.RequestException:
            time.sleep(2 + attempt * 3)
            continue
        if response.status_code == 200:
            try:
                items = response.json().get("items") or []
            except ValueError:
                return None
            return items[0] if items else None
        if response.status_code == 429:
            time.sleep(8 + attempt * 10)
            continue
        if response.status_code in (500, 502, 503, 504):
            time.sleep(3 + attempt * 4)
            continue
        return None
    return None


def media_id_for(row: sqlite3.Row) -> str:
    value = str(row["media_id"] or "")
    if value.startswith("folder:"):
        return decode_shortcode(row["shortcode"]) or ""
    return value


def repair_row(
    row: sqlite3.Row,
    media_root: Path,
    media_by_id: Dict[str, List[Tuple[str, str]]],
    session: requests.Session,
    gaps: List[str],
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "id": row["id"],
        "shortcode": row["shortcode"],
        "gaps": gaps,
        "ok": False,
        "owner": None,
        "caption": None,
        "cover": None,
        "media": 0,
    }
    media_id = media_id_for(row)
    if not media_id:
        report["error"] = "no media id"
        return report
    item = fetch_item(session, media_id)
    if item is None:
        report["error"] = "media unavailable"
        return report

    report["ok"] = True
    owner = (item.get("user") or {}).get("username")
    caption = ((item.get("caption") or {}).get("text") or "")
    report["owner"] = owner
    report["caption"] = caption[:80]

    folder = media_root / row["folder"] if row["folder"] else media_root / media_id
    folder.mkdir(parents=True, exist_ok=True)
    code = item.get("code") or row["shortcode"]
    taken = item.get("taken_at")
    stamp = (
        datetime.fromtimestamp(taken, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        if isinstance(taken, (int, float))
        else datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )

    existing_media = [
        media_root / path
        for path, _ in media_by_id.get(row["media_id"], [])
        if (media_root / path).is_file() and (media_root / path).stat().st_size > 1024
    ]
    if not existing_media:
        existing_media = [
            path for path in sorted(folder.iterdir())
            if path.is_file() and path.suffix.lower() in adb.MEDIA_EXTS
            and path.name.lower() != "cover.jpg"
        ]
    if "media" in gaps:
        report["media"] = download_missing_media(
            session, item, folder, code, stamp, existing_media
        )

    cover = download_cover(session, item, folder)
    report["cover"] = cover

    # Update the row with authoritative data where it is currently missing.
    new_owner = row["owner_username"] or owner
    new_owner_id = row["owner_id"] or (str((item.get("user") or {}).get("pk") or "") or None)
    new_caption = row["caption"] if (row["caption"] or "").strip() else caption
    new_taken = row["taken_at"] or (
        datetime.fromtimestamp(taken, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        if isinstance(taken, (int, float))
        else None
    )
    new_likes = item.get("like_count") or row["likes"] or 0
    new_video = bool(item.get("video_versions")) or bool(row["is_video"])
    new_typename = item.get("__typename") or row["typename"]
    new_location = (item.get("location") or {}).get("name") or row["location"]
    new_hashtags = re.findall(r"#(\w+)", new_caption)
    new_cover = cover or row["cover"]
    if not new_cover:
        new_cover = adb.find_cover(folder)

    report.update(
        {
            "new_owner": new_owner,
            "new_owner_id": new_owner_id,
            "new_caption": new_caption,
            "new_taken": new_taken,
            "new_likes": new_likes,
            "new_video": new_video,
            "new_typename": new_typename,
            "new_location": new_location,
            "new_hashtags": new_hashtags,
        }
    )

    record = {
        "media_id": str(item.get("pk") or media_id).split("_", 1)[0],
        "shortcode": code,
        "permalink": f"https://www.instagram.com/p/{code}/",
        "taken_at": new_taken,
        "caption": new_caption,
        "owner_username": new_owner,
        "owner_id": new_owner_id,
        "likes": new_likes,
        "is_video": new_video,
        "typename": new_typename,
        "media": [
            {
                "type": "video" if path.suffix.lower() in adb.VIDEO_EXTS else "image",
                "path": f"{folder.name}/{path.name}",
            }
            for path in sorted(folder.iterdir())
            if path.is_file() and path.suffix.lower() in adb.MEDIA_EXTS
            and path.name.lower() != "cover.jpg"
        ],
        "comments": [],
        "location": new_location,
        "hashtags": new_hashtags,
    }
    comments_file = folder / "comments.json"
    if comments_file.is_file():
        try:
            comments = json.loads(comments_file.read_text(encoding="utf-8"))
            record["comments"] = comments if isinstance(comments, list) else []
        except (OSError, ValueError):
            record["comments"] = []
    write_sidecars(folder, record)
    if new_cover:
        (folder / "cover.jpg").exists()  # keep the canonical filename

    report["new_media_id"] = record["media_id"]
    report["new_cover"] = new_cover
    return report


def apply_report(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    report: Dict[str, Any],
    media_root: Path,
) -> None:
    if not report.get("ok"):
        return
    folder = row["folder"]
    new_media_id = report.get("new_media_id") or row["media_id"]
    old_media_id = row["media_id"]
    conn.execute(
        "DELETE FROM post_media WHERE media_id IN (?, ?)",
        (old_media_id, new_media_id),
    )
    if new_media_id != old_media_id:
        conn.execute(
            "UPDATE OR IGNORE post_tags SET media_id=? WHERE media_id=?",
            (new_media_id, old_media_id),
        )
        conn.execute("DELETE FROM post_tags WHERE media_id=?", (old_media_id,))
    adb.upsert_post(
        conn,
        media_id=new_media_id,
        shortcode=row["shortcode"],
        media_root=media_root,
        taken_at=report.get("new_taken") or row["taken_at"],
        owner_username=report.get("new_owner") or row["owner_username"],
        owner_id=report.get("new_owner_id") or row["owner_id"],
        caption=report.get("new_caption") or "",
        likes=report.get("new_likes") or 0,
        is_video=bool(report.get("new_video")),
        typename=report.get("new_typename") or row["typename"],
        location=report.get("new_location") or row["location"],
        hashtags=report.get("new_hashtags") or [],
        folder=folder,
        cover=report.get("new_cover") or row["cover"],
        source=row["source"] or "api",
    )
    conn.commit()


def invalidate_thumbs(cache_dir: Path, post_id: int) -> None:
    thumbs = cache_dir / "thumbs"
    if not thumbs.is_dir():
        return
    for path in thumbs.glob(f"{post_id}_*.jpg"):
        try:
            path.unlink()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill archive gaps")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--cache", default="data/.cache")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.15)
    args = parser.parse_args()

    media_root = Path(args.posts)
    conn = adb.connect(Path(args.db))
    conn.row_factory = sqlite3.Row
    media_by_id: Dict[str, List[Tuple[str, str]]] = {}
    for media in conn.execute("SELECT media_id, path, kind FROM post_media"):
        media_by_id.setdefault(media["media_id"], []).append(
            (media["path"], media["kind"])
        )

    rows = conn.execute("SELECT * FROM posts ORDER BY sort_order, id").fetchall()
    tasks: List[Tuple[sqlite3.Row, List[str]]] = []
    for row in rows:
        gaps = row_gaps(row, media_root, media_by_id)
        if gaps:
            tasks.append((row, gaps))
    if args.limit:
        tasks = tasks[: args.limit]

    counts: Dict[str, int] = {}
    for _, gaps in tasks:
        for gap in gaps:
            counts[gap] = counts.get(gap, 0) + 1
    print(f"{len(tasks)} posts need repair: {counts}")
    if args.dry_run or not tasks:
        conn.close()
        return

    local = threading.local()

    def get_session() -> requests.Session:
        if not hasattr(local, "session"):
            local.session = session_factory()
        return local.session

    def work(row: sqlite3.Row, gaps: List[str]) -> Tuple[sqlite3.Row, List[str], Dict[str, Any]]:
        try:
            report = repair_row(row, media_root, media_by_id, get_session(), gaps)
        except Exception as exc:
            report = {
                "id": row["id"],
                "shortcode": row["shortcode"],
                "gaps": gaps,
                "ok": False,
                "error": str(exc),
            }
        time.sleep(args.sleep)
        return row, gaps, report

    done = 0
    ok = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(work, row, gaps) for row, gaps in tasks]
        for future in as_completed(futures):
            row, gaps, report = future.result()
            done += 1
            if report.get("ok"):
                ok += 1
                apply_report(conn, row, report, media_root)
                invalidate_thumbs(Path(args.cache), row["id"])
            else:
                failed += 1
            if done % 25 == 0 or done == len(tasks):
                print(
                    f"  {done}/{len(tasks)} ok={ok} failed={failed} "
                    f"last={row['shortcode']} gaps={','.join(gaps)}"
                    + (f" error={report.get('error')}" if report.get("error") else ""),
                    flush=True,
                )
    conn.close()
    print(f"done: {ok} repaired, {failed} unavailable")


if __name__ == "__main__":
    main()
