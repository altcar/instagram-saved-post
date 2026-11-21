#!/usr/bin/env python3
"""Fill missing images for non-video Instagram ads.

The Saved feed often exposes only the first creative for an ad even when the
real post has a carousel. Instagram's own media-info endpoint still returns the
full ``carousel_media`` list, which is what the web page renders for
``?img_index=1,2,3...``.

This utility is intentionally separate from ``ads_sync.py`` so the normal
pipeline is untouched. It only downloads extra slides that are missing, then
refreshes ``data/ads.json`` with the discovered counts.

    python ads_carousel.py --dry-run
    python ads_carousel.py --limit 20
    python ads_carousel.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

import archive_db as adb
from repair_posts import build_session, fetch_item, image_url, slides, video_url


def existing_media(folder: Path) -> List[Path]:
    return adb.list_media(folder) if folder.is_dir() else []


def existing_files(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    files = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in adb.MEDIA_EXTS:
            continue
        if path.name.lower() == "cover.jpg":
            continue
        files.append(path)
    return files


def download(
    session: requests.Session, url: str, target: Path, timeout: int = 90
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


def enrich_one(
    session: requests.Session,
    conn: sqlite3.Connection,
    media_root: Path,
    row: sqlite3.Row,
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    item = fetch_item(session, str(row["media_id"]))
    if item is None:
        return {
            "shortcode": row["shortcode"],
            "ok": False,
            "error": "media info unavailable",
        }

    wanted = slides(item)
    folder = media_root / row["folder"] if row["folder"] else None
    if folder is None:
        return {"shortcode": row["shortcode"], "ok": False, "error": "no folder"}

    have = existing_files(folder)
    report: Dict[str, Any] = {
        "shortcode": item.get("code") or row["shortcode"],
        "media_id": str(row["media_id"]),
        "owner": (item.get("user") or {}).get("username") or row["owner_username"],
        "slides": len(wanted),
        "existing": len(have),
        "downloaded": 0,
        "ok": True,
        "dry_run": dry_run,
    }
    if dry_run or len(wanted) <= len(have):
        return report

    folder.mkdir(parents=True, exist_ok=True)
    taken = item.get("taken_at")
    stamp = (
        datetime.fromtimestamp(taken, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        if isinstance(taken, (int, float))
        else datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )
    code = item.get("code") or row["shortcode"]
    for index in range(len(have), len(wanted)):
        media = wanted[index]
        suffix = ".mp4" if media["kind"] == "video" else ".jpg"
        # Match the names written by ads_sync.py so repeats are idempotent.
        name = f"{code}_{stamp}_ad_{index + 1}{suffix}"
        if download(session, media["url"], folder / name):
            report["downloaded"] += 1
        time.sleep(0.12)

    # Refresh the owner/caption while we have the authoritative payload.
    user = item.get("user") or {}
    caption = (item.get("caption") or {}).get("text") or row["caption"] or ""
    cover = adb.find_cover(folder)
    adb.upsert_post(
        conn,
        media_id=str(row["media_id"]),
        shortcode=code,
        media_root=media_root,
        taken_at=row["taken_at"],
        owner_username=user.get("username") or row["owner_username"],
        owner_id=user.get("pk") or row["owner_id"],
        caption=caption,
        likes=item.get("like_count") or row["likes"] or 0,
        is_video=bool(item.get("video_versions")),
        typename=row["typename"],
        location=row["location"],
        hashtags=json.loads(row["hashtags"] or "[]"),
        folder=row["folder"],
        cover=cover,
        source=row["source"] or "ad",
    )
    conn.commit()
    report["files"] = len(existing_files(folder))
    return report


def update_ads_json(path: Path, reports: List[Dict[str, Any]]) -> None:
    if not path.is_file():
        return
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    by_code = {
        item.get("shortcode"): item
        for item in current
        if isinstance(item, dict) and item.get("shortcode")
    }
    for report in reports:
        entry = by_code.get(report.get("shortcode"))
        if not entry or not report.get("ok"):
            continue
        entry["carousel_count"] = max(
            int(entry.get("carousel_count") or 0),
            int(report.get("slides") or 0) - 1 if report.get("slides", 0) > 1 else 0,
        )
        entry["creatives"] = max(
            int(entry.get("creatives") or 0), int(report.get("slides") or 0)
        )
        entry["files"] = report.get("files")
        entry["api_enriched"] = True
    path.write_text(
        json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill multi-image ad carousels")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--ads-json", default="data/ads.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = adb.connect(Path(args.db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM posts WHERE is_ad=1 AND is_video=0 "
        "ORDER BY sort_order, id"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    session = build_session()

    reports: List[Dict[str, Any]] = []
    failures = 0
    for index, row in enumerate(rows, 1):
        try:
            report = enrich_one(
                session, conn, Path(args.posts), row, dry_run=args.dry_run
            )
        except Exception as exc:
            report = {
                "shortcode": row["shortcode"],
                "ok": False,
                "error": str(exc),
            }
        reports.append(report)
        status = "ok" if report.get("ok") else "skip"
        print(
            f"[{index}/{len(rows)}] {report.get('shortcode')} {status} "
            f"{report.get('existing', '-')} -> {report.get('slides', '-')} "
            f"downloaded={report.get('downloaded', 0)}"
            + (f" ({report.get('error')})" if report.get("error") else ""),
            flush=True,
        )
        if not report.get("ok"):
            failures += 1
        time.sleep(0.35)

    if not args.dry_run:
        update_ads_json(Path(args.ads_json), reports)
    conn.close()
    extra = sum(int(r.get("downloaded") or 0) for r in reports)
    print(f"done: {len(rows) - failures}/{len(rows)} ads, {extra} new files")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
