#!/usr/bin/env python3
"""Identify real Instagram ads (product_type == "ad") and download all creatives.

The Saved API marks promoted posts with product_type="ad". This sets is_ad=1,
forces the Ads category, fills any missing carousel/video files, and writes
data/ads.json as the auditable list.

    python ads_sync.py --dry-run
    python ads_sync.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

import archive_db as adb


def best_url(item: Dict[str, Any]) -> Optional[tuple]:
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


def creatives(media: Dict[str, Any]) -> List[tuple]:
    parts = media.get("carousel_media") or []
    if parts:
        return [found for part in parts if (found := best_url(part))]
    found = best_url(media)
    return [found] if found else []


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync real Instagram ads")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--raw", default="data/raw/items.jsonl")
    parser.add_argument("--out", default="data/ads.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ads: Dict[str, Dict[str, Any]] = {}
    with open(args.raw, encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            media = item.get("media") or item
            if media.get("product_type") == "ad" and media.get("code"):
                ads[media["code"]] = media

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("ALTER TABLE posts ADD COLUMN is_ad INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

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

    downloaded = 0
    report = []
    if not args.dry_run:
        conn.execute("UPDATE posts SET is_ad=0")
    for code, media in ads.items():
        row = conn.execute(
            "SELECT * FROM posts WHERE shortcode=?", (code,)
        ).fetchone()
        if row is None or not row["folder"]:
            continue
        folder = Path(args.posts) / row["folder"]
        folder.mkdir(parents=True, exist_ok=True)
        urls = creatives(media)
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM post_media WHERE media_id=?", (row["media_id"],)
        ).fetchone()[0]
        if not args.dry_run:
            conn.execute(
                "UPDATE posts SET is_ad=1, category='ad', category_source='ads', "
                "category_confidence=1.0 WHERE id=?",
                (row["id"],),
            )
            stamp = (row["taken_at"] or "ad").replace("-", "").replace(":", "")[:13]
            existing = {
                path.name for path in folder.iterdir() if path.is_file()
            }
            # Existing media already covers these slides; only fill real gaps.
            for index, (url, kind) in enumerate(urls[existing_count:], existing_count + 1):
                suffix = ".mp4" if kind == "video" else ".jpg"
                name = f"{code}_{stamp}_ad_{index}{suffix}"
                if name in existing:
                    continue
                try:
                    response = session.get(url, timeout=60)
                    if response.status_code == 200 and response.content:
                        (folder / name).write_bytes(response.content)
                        downloaded += 1
                except requests.RequestException:
                    continue
            adb.upsert_post(
                conn,
                media_id=row["media_id"],
                shortcode=code,
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
                source="ad",
            )
            conn.commit()

        report.append(
            {
                "shortcode": code,
                "owner": (media.get("user") or {}).get("username") or row["owner_username"],
                "folder": row["folder"],
                "media_type": media.get("media_type"),
                "carousel_count": len(media.get("carousel_media") or []),
                "creatives": len(urls),
                "caption": (media.get("caption") or {}).get("text") if media.get("caption") else None,
                "urls": [url for url, _ in urls],
            }
        )

    conn.commit()
    conn.close()
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(
        f"{len(report)} real ads; downloaded {downloaded} missing creatives; "
        f"list -> {args.out}"
    )


if __name__ == "__main__":
    main()
