#!/usr/bin/env python3
"""Audit carousel files against the raw saved-feed metadata and fill gaps.

The saved feed carries carousel_media (or carousel_media_count). This compares
that expected count with the files on disk and downloads missing slides.

    python audit_media.py --dry-run
    python audit_media.py --limit 100
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

import archive_db as adb


def best_url(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    if item.get("video_versions"):
        versions = sorted(
            item["video_versions"],
            key=lambda v: (v.get("width") or 0) * (v.get("height") or 0),
            reverse=True,
        )
        return versions[0].get("url"), "video"
    versions = item.get("image_versions2") or {}
    candidates = versions.get("candidates") or []
    if candidates:
        return candidates[0].get("url"), "image"
    return None


def expected_items(media: Dict[str, Any]) -> List[Tuple[str, str]]:
    parts = media.get("carousel_media") or []
    urls = []
    if parts:
        for part in parts:
            found = best_url(part)
            if found:
                urls.append(found)
        return urls
    found = best_url(media)
    return [found] if found else []


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit carousel media")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--raw", default="data/raw/items.jsonl")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    raw: Dict[str, Dict[str, Any]] = {}
    with open(args.raw, encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            media = item.get("media") or item
            if media.get("code"):
                raw[media["code"]] = media

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM posts WHERE folder IS NOT NULL ORDER BY sort_order"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.instagram.com/",
        }
    )

    missing_posts = 0
    downloaded = 0
    checked = 0
    unknown = 0
    for row in rows:
        media = raw.get(row["shortcode"])
        if not media:
            unknown += 1
            continue
        expected = expected_items(media)
        have = conn.execute(
            "SELECT COUNT(*) FROM post_media WHERE media_id=?", (row["media_id"],)
        ).fetchone()[0]
        checked += 1
        if have >= len(expected):
            continue
        missing_posts += 1
        print(
            f"{row['shortcode']}: expected {len(expected)}, have {have}",
            flush=True,
        )
        if args.dry_run:
            continue
        folder = Path(args.posts) / row["folder"]
        folder.mkdir(parents=True, exist_ok=True)
        stamp = (row["taken_at"] or "post").replace("-", "").replace(":", "")[:13]
        for index in range(have, len(expected)):
            url, kind = expected[index]
            suffix = ".mp4" if kind == "video" else ".jpg"
            target = folder / f"{row['shortcode']}_{stamp}_carousel_{index + 1}{suffix}"
            if target.exists():
                continue
            try:
                response = session.get(url, timeout=60)
                if response.status_code == 200 and response.content:
                    target.write_bytes(response.content)
                    downloaded += 1
            except requests.RequestException:
                continue
        adb.upsert_post(
            conn,
            media_id=row["media_id"],
            shortcode=row["shortcode"],
            media_root=Path(args.posts),
            taken_at=row["taken_at"],
            owner_username=row["owner_username"],
            owner_id=row["owner_id"],
            caption=row["caption"] or "",
            likes=row["likes"] or 0,
            is_video=bool(row["is_video"]),
            typename=row["typename"],
            location=row["location"],
            hashtags=json.loads(row["hashtags"] or "[]"),
            folder=row["folder"],
            cover=adb.find_cover(folder),
            source=row["source"] or "api",
        )
        conn.commit()

    conn.close()
    print(
        f"checked {checked} saved-feed posts; {missing_posts} had fewer files; "
        f"downloaded {downloaded}; {unknown} archived posts not in the live feed"
    )


if __name__ == "__main__":
    main()
