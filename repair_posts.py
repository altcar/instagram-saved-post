#!/usr/bin/env python3
"""Repair individual archive rows from Instagram's current media-info API.

This is a targeted utility for posts whose owners came through as ``unknown``
or whose saved item was metadata-only (no media folder). It is deliberately
separate from the daily downloader so the normal pipeline is unaffected.

It uses the same Chrome cookie jar as the rest of the project, fetches
``/api/v1/media/<pk>/info/`` (the web app's own endpoint), downloads any
missing carousel/video files, refreshes the caption, and rewrites the row in
``data/archive.db`` while preserving category, notes, tags and comments.

Examples:
    python repair_posts.py --shortcode DRheMYTEdbQ --shortcode DSAwWoND6CK
    python repair_posts.py --shortcode DNu-QrYWuj_ --shortcode DDhrvAgu_vB
    python repair_posts.py --shortcode Dd66EkgsCh   # alias -> Dd66EkgsChS
    python repair_posts.py --missing-media
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

import archive_db as adb
from chrome_cookies import load_cookies
from download_saved import write_sidecars

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
# The user's list shortened this code by one character.
ALIASES = {"Dd66EkgsCh": "Dd66EkgsChS"}
INFO_URL = "https://www.instagram.com/api/v1/media/{media_id}/info/"


def decode_shortcode(code: str) -> Optional[str]:
    try:
        value = 0
        for char in code:
            value = value * 64 + ALPHABET.index(char)
        return str(value)
    except ValueError:
        return None


def build_session() -> requests.Session:
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


def fetch_item(session: requests.Session, media_id: str) -> Optional[Dict[str, Any]]:
    response = session.get(INFO_URL.format(media_id=media_id), timeout=45)
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    items = payload.get("items") or []
    return items[0] if items else None


def image_url(node: Dict[str, Any]) -> Optional[str]:
    candidates = (node.get("image_versions2") or {}).get("candidates") or []
    return candidates[0].get("url") if candidates else node.get("display_url")


def video_url(node: Dict[str, Any]) -> Optional[str]:
    versions = node.get("video_versions") or []
    if not versions:
        return None
    versions = sorted(
        versions,
        key=lambda v: (v.get("width") or 0) * (v.get("height") or 0),
        reverse=True,
    )
    return versions[0].get("url")


def slides(item: Dict[str, Any]) -> List[Dict[str, str]]:
    parts = item.get("carousel_media") or [item]
    out: List[Dict[str, str]] = []
    for part in parts:
        url = video_url(part) or image_url(part)
        if url:
            out.append(
                {
                    "url": url,
                    "kind": "video" if part.get("video_versions") else "image",
                }
            )
    return out


def folder_for(
    media_root: Path, row: Optional[sqlite3.Row], media_id: str, shortcode: str
) -> Path:
    if row is not None and row["folder"]:
        candidate = media_root / row["folder"]
        if candidate.is_dir():
            return candidate
    for name in (media_id, shortcode):
        candidate = media_root / name
        if candidate.is_dir():
            return candidate
    return media_root / media_id


def existing_media(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    result = []
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in adb.MEDIA_EXTS:
            continue
        if path.name.lower() == "cover.jpg":
            continue
        result.append(path)
    return result


def download_file(
    session: requests.Session, url: str, target: Path, *, timeout: int = 120
) -> bool:
    if target.is_file() and target.stat().st_size > 1024:
        return True
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with session.get(url, timeout=timeout, stream=True) as response:
            if response.status_code != 200:
                return False
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if chunk:
                        handle.write(chunk)
        if tmp.stat().st_size <= 1024:
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(target)
        return True
    except requests.RequestException:
        tmp.unlink(missing_ok=True)
        return False


def preserve_comments(folder: Path) -> List[Dict[str, Any]]:
    path = folder / "comments.json"
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (ValueError, OSError):
        return []


def fetch_existing_row(
    conn: sqlite3.Connection, shortcode: str, media_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posts WHERE shortcode=? OR media_id IN (?, ?) "
        "ORDER BY CASE WHEN shortcode=? THEN 0 ELSE 1 END LIMIT 1",
        (shortcode, media_id, f"folder:{shortcode}", shortcode),
    ).fetchone()


def repair_one(
    session: requests.Session,
    conn: sqlite3.Connection,
    media_root: Path,
    code: str,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    code = ALIASES.get(code, code)
    media_id = decode_shortcode(code) or ""
    item = fetch_item(session, media_id) if media_id else None
    if item is None:
        # A few imported posts have shortcode/ID drift; try the DB value.
        row = conn.execute(
            "SELECT media_id FROM posts WHERE shortcode=?", (code,)
        ).fetchone()
        if row and row["media_id"] and not str(row["media_id"]).startswith("folder:"):
            media_id = str(row["media_id"])
            item = fetch_item(session, media_id)
    if item is None:
        return {"code": code, "ok": False, "error": "media info unavailable"}

    actual_id = str(item.get("pk") or item.get("id") or media_id).split("_", 1)[0]
    shortcode = str(item.get("code") or code)
    row = fetch_existing_row(conn, shortcode, actual_id)
    folder = folder_for(media_root, row, actual_id, shortcode)
    found = existing_media(folder)
    wanted = slides(item)

    report: Dict[str, Any] = {
        "code": shortcode,
        "media_id": actual_id,
        "owner": (item.get("user") or {}).get("username"),
        "caption_len": len(((item.get("caption") or {}).get("text") or "")),
        "slides": len(wanted),
        "folder": str(folder),
        "existing_before": len(found),
        "downloaded": 0,
        "dry_run": dry_run,
        "ok": True,
    }
    if dry_run:
        return report

    folder.mkdir(parents=True, exist_ok=True)

    # Reuse files already in the folder; only download slides that are missing.
    if len(found) < len(wanted):
        taken = item.get("taken_at")
        if isinstance(taken, (int, float)):
            stamp = datetime.fromtimestamp(taken, tz=timezone.utc).strftime(
                "%Y%m%d_%H%M%S"
            )
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        for index in range(len(found), len(wanted)):
            media = wanted[index]
            suffix = ".mp4" if media["kind"] == "video" else ".jpg"
            name = (
                f"{shortcode}_{stamp}_{index + 1}{suffix}"
                if len(wanted) > 1
                else f"{shortcode}_{stamp}{suffix}"
            )
            target = folder / name
            if download_file(session, media["url"], target):
                report["downloaded"] += 1
                time.sleep(0.15)

    # Refresh the cover from the first slide's image (the video poster for reels).
    cover = folder / "cover.jpg"
    if not cover.is_file() or cover.stat().st_size <= 1024:
        cover_url = image_url(item)
        if not cover_url and item.get("carousel_media"):
            cover_url = image_url(item["carousel_media"][0])
        if cover_url:
            download_file(session, cover_url, cover)

    found = existing_media(folder)
    if not found:
        report["ok"] = False
        report["error"] = "no media files available"
        return report

    user = item.get("user") or {}
    caption = ((item.get("caption") or {}).get("text") or "")
    taken = item.get("taken_at")
    taken_at = (
        datetime.fromtimestamp(taken, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        if isinstance(taken, (int, float))
        else (row["taken_at"] if row is not None else None)
    )
    record = {
        "media_id": actual_id,
        "shortcode": shortcode,
        "permalink": f"https://www.instagram.com/p/{shortcode}/",
        "taken_at": taken_at,
        "caption": caption,
        "owner_username": user.get("username"),
        "owner_id": user.get("pk") or user.get("id"),
        "likes": item.get("like_count") or 0,
        "is_video": bool(item.get("video_versions")),
        "typename": item.get("__typename"),
        "media": [
            {
                "type": "video" if p.suffix.lower() in adb.VIDEO_EXTS else "image",
                "path": f"{folder.name}/{p.name}",
            }
            for p in found
        ],
        "comments": preserve_comments(folder),
        "location": (item.get("location") or {}).get("name"),
        "hashtags": re.findall(r"#(\w+)", caption),
    }
    write_sidecars(folder, record)

    old_media_id = str(row["media_id"]) if row is not None else None
    # The row's key changed (folder:<shortcode> -> real pk); remove stale
    # post_media rows for both keys before archive_db writes the fresh list.
    conn.execute(
        "DELETE FROM post_media WHERE media_id IN (?, ?)",
        (old_media_id or "", actual_id),
    )
    adb.upsert_post(
        conn,
        media_id=actual_id,
        shortcode=shortcode,
        media_root=media_root,
        taken_at=taken_at,
        owner_username=record["owner_username"],
        owner_id=record["owner_id"],
        caption=caption,
        likes=record["likes"],
        is_video=record["is_video"],
        typename=record["typename"],
        location=record["location"],
        hashtags=record["hashtags"],
        folder=folder.name,
        cover=f"{folder.name}/cover.jpg" if cover.is_file() else adb.find_cover(folder),
        source=(row["source"] if row is not None and row["source"] else "api"),
    )
    conn.commit()
    report["files"] = len(found)
    report["cover"] = cover.is_file()
    return report


def missing_media_codes(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT shortcode FROM posts p WHERE NOT EXISTS "
        "(SELECT 1 FROM post_media m WHERE m.media_id=p.media_id) "
        "ORDER BY sort_order, id"
    ).fetchall()
    return [row["shortcode"] for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair named Instagram posts")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--shortcode", action="append", default=[])
    parser.add_argument("--missing-media", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    media_root = Path(args.posts)
    conn = adb.connect(Path(args.db))
    codes = list(args.shortcode)
    if args.missing_media:
        codes += missing_media_codes(conn)
    codes = list(dict.fromkeys(ALIASES.get(c, c) for c in codes))
    if not codes:
        parser.error("give --shortcode or --missing-media")

    session = build_session()
    failures = 0
    for code in codes:
        try:
            report = repair_one(session, conn, media_root, code, dry_run=args.dry_run)
        except Exception as exc:  # keep going for the remaining codes
            report = {"code": code, "ok": False, "error": str(exc)}
        print(json.dumps(report, ensure_ascii=False))
        if not report.get("ok"):
            failures += 1
        time.sleep(0.35)
    conn.close()
    print(f"done: {len(codes) - failures}/{len(codes)} repaired")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
