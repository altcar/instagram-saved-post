#!/usr/bin/env python3
"""Browser fallback for posts the regular API cannot download (ads, deleted
API media, cover-only posts, etc.).

Uses Playwright with the user's Chrome cookies, opens the post page, captures
the media GraphQL response (or embedded JSON), downloads the real video/images
and cover, and writes the same per-post files as the normal downloader.

Examples:
    python browser_fetch.py --shortcode DaSP3wRzyD3
    python browser_fetch.py --missing --limit 20
    python browser_fetch.py --shortcode X --headful
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from playwright.sync_api import sync_playwright

import archive_db as adb
from chrome_cookies import playwright_cookies
from download_saved import comments_text, write_sidecars


def walk_nodes(obj: Any, shortcode: str, out: List[Dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        if obj.get("code") == shortcode and (
            obj.get("video_versions") or obj.get("image_versions2")
        ):
            out.append(obj)
        for value in obj.values():
            walk_nodes(value, shortcode, out)
    elif isinstance(obj, list):
        for value in obj:
            walk_nodes(value, shortcode, out)


def parse_for_shortcode(payload: Any, shortcode: str) -> Optional[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    walk_nodes(payload, shortcode, found)
    if found:
        # prefer the richest node
        found.sort(key=lambda n: len(json.dumps(n)), reverse=True)
        return found[0]
    return None


def decode_json_text(text: str) -> Optional[Any]:
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[9:]
    try:
        return json.loads(text)
    except ValueError:
        return None


def best_video(node: Dict[str, Any]) -> Optional[str]:
    versions = node.get("video_versions") or []
    if not versions:
        return None
    versions = sorted(
        versions,
        key=lambda v: (v.get("width") or 0) * (v.get("height") or 0),
        reverse=True,
    )
    return versions[0].get("url")


def image_url(obj: Dict[str, Any]) -> Optional[str]:
    versions = obj.get("image_versions2") or {}
    candidates = versions.get("candidates") or []
    if candidates:
        return candidates[0].get("url")
    return obj.get("display_url")


def collect_media(node: Dict[str, Any]) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    carousel = node.get("carousel_media") or []
    if carousel:
        for part in carousel:
            url = best_video(part) if part.get("video_versions") else image_url(part)
            if url:
                items.append(
                    {
                        "url": url,
                        "kind": "video" if part.get("video_versions") else "image",
                    }
                )
    else:
        url = best_video(node) if node.get("video_versions") else image_url(node)
        if url:
            items.append(
                {
                    "url": url,
                    "kind": "video" if node.get("video_versions") else "image",
                }
            )
    return items


def next_folder(media_root: Path, shortcode: str) -> Path:
    media_root.mkdir(parents=True, exist_ok=True)
    existing = sorted(media_root.glob(f"*_{shortcode}"))
    if existing:
        return existing[0]
    max_index = 0
    for folder in media_root.iterdir():
        if folder.is_dir() and "_" in folder.name:
            head = folder.name.split("_", 1)[0]
            if head.isdigit():
                max_index = max(max_index, int(head))
    return media_root / f"{max_index + 1:04d}_{shortcode}"


def fetch_post(shortcode: str, args: argparse.Namespace, session: requests.Session) -> bool:
    node: Optional[Dict[str, Any]] = None
    og_image = None
    og_desc = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=not args.headful,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        context.add_cookies(playwright_cookies())

        captured: List[Any] = []

        def on_response(response):
            url = response.url
            if "graphql" not in url and "/api/v1/media/" not in url:
                return
            try:
                text = response.text()
            except Exception:
                return
            payload = decode_json_text(text)
            if payload is not None:
                captured.append(payload)

        page = context.new_page()
        page.on("response", on_response)
        page.goto(
            f"https://www.instagram.com/p/{shortcode}/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(args.wait_ms)

        for payload in captured:
            node = parse_for_shortcode(payload, shortcode)
            if node:
                break

        if node is None:
            html = page.content()
            for match in re.finditer(r'"video_versions":\s*(\[[^\]]*\])', html):
                try:
                    versions = json.loads(match.group(1))
                except ValueError:
                    continue
                node = {"video_versions": versions, "code": shortcode}
                break
            if node is None:
                for match in re.finditer(r'"image_versions2":\s*(\{.*?\}\})', html):
                    try:
                        versions = json.loads(match.group(1))
                    except ValueError:
                        continue
                    node = {"image_versions2": versions, "code": shortcode}
                    break

        try:
            loc = page.locator('meta[property="og:image"]')
            og_image = loc.get_attribute("content") if loc.count() else None
            loc = page.locator('meta[property="og:description"]')
            og_desc = loc.get_attribute("content") if loc.count() else None
        except Exception:
            pass
        browser.close()

    if node is None:
        print(f"{shortcode}: no media node found (og:image={bool(og_image)})")
        if not og_image:
            return False
        node = {"image_versions2": {"candidates": [{"url": og_image}]}, "code": shortcode}

    folder = next_folder(Path(args.media), shortcode)
    folder.mkdir(parents=True, exist_ok=True)

    items = collect_media(node)
    if not items and og_image:
        items = [{"url": og_image, "kind": "image"}]
    if not items:
        print(f"{shortcode}: no downloadable media")
        return False

    taken = node.get("taken_at")
    if isinstance(taken, (int, float)):
        stamp = datetime.fromtimestamp(taken, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    saved: List[str] = []
    for i, item in enumerate(items, 1):
        suffix = ".mp4" if item["kind"] == "video" else ".jpg"
        filename = f"{shortcode}_{stamp}_{i}{suffix}" if len(items) > 1 else f"{shortcode}_{stamp}{suffix}"
        target = folder / filename
        if target.is_file() and target.stat().st_size > 1024:
            saved.append(filename)
            continue
        try:
            response = session.get(item["url"], timeout=60)
            if response.status_code == 200 and response.content:
                target.write_bytes(response.content)
                saved.append(filename)
        except requests.RequestException as exc:
            print(f"{shortcode}: download failed {exc}", file=sys.stderr)

    if not saved:
        print(f"{shortcode}: no files downloaded")
        return False

    cover_url = og_image or image_url(node)
    if cover_url:
        try:
            response = session.get(cover_url, timeout=30)
            if response.status_code == 200 and response.content:
                (folder / "cover.jpg").write_bytes(response.content)
        except requests.RequestException:
            pass

    caption = ""
    cap = node.get("caption")
    if isinstance(cap, dict):
        caption = cap.get("text") or ""
    elif isinstance(cap, str):
        caption = cap
    if not caption and og_desc:
        caption = og_desc

    user = node.get("user") or {}
    record = {
        "media_id": node.get("pk") or node.get("id"),
        "shortcode": shortcode,
        "permalink": f"https://www.instagram.com/p/{shortcode}/",
        "taken_at": (
            datetime.fromtimestamp(taken, tz=timezone.utc).replace(microsecond=0).isoformat()
            if isinstance(taken, (int, float))
            else None
        ),
        "caption": caption,
        "owner_username": user.get("username"),
        "owner_id": user.get("pk") or user.get("id"),
        "likes": node.get("like_count") or 0,
        "is_video": bool(node.get("video_versions")),
        "typename": "GraphVideo" if node.get("video_versions") else "GraphImage",
        "media": [
            {"type": "video" if f.endswith(".mp4") else "image",
             "path": f"{folder.name}/{f}"}
            for f in saved
        ],
        "comments": [],
        "location": (node.get("location") or {}).get("name"),
        "hashtags": re.findall(r"#(\w+)", caption),
    }
    write_sidecars(folder, record)
    print(f"{shortcode}: saved {len(saved)} file(s) to {folder.name}")

    if Path(args.db).exists():
        conn = adb.connect(Path(args.db))
        media_id = str(node.get("pk") or node.get("id") or "")
        adb.upsert_post(
            conn,
            media_id=media_id or None,
            shortcode=shortcode,
            media_root=Path(args.media),
            taken_at=record["taken_at"],
            owner_username=record["owner_username"],
            owner_id=record["owner_id"],
            caption=caption,
            likes=record["likes"],
            is_video=record["is_video"],
            typename=record["typename"],
            location=record["location"],
            hashtags=record["hashtags"],
            folder=folder.name,
            cover=f"{folder.name}/cover.jpg" if (folder / "cover.jpg").is_file() else None,
            source="browser",
        )
        conn.commit()
        conn.close()

    try:
        from dedup import Dedup
        Dedup(Path(args.dedup)).mark(
            str(node.get("pk") or node.get("id") or f"folder:{shortcode}"),
            shortcode,
            folder.name,
            Path(args.media).parent.name,
        )
    except Exception:
        pass
    return True


def posts_missing_media(db: Path) -> List[str]:
    import sqlite3
    conn = sqlite3.connect(db)
    rows = conn.execute(
        """SELECT p.shortcode FROM posts p
           WHERE NOT EXISTS (SELECT 1 FROM post_media m WHERE m.media_id=p.media_id)
           ORDER BY p.sort_order"""
    ).fetchall()
    missing = [r[0] for r in rows]
    conn.close()
    return missing


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser fallback for hard posts")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--dedup", default="data/dedup.db")
    parser.add_argument("--media", default="data/posts")
    parser.add_argument("--shortcode", action="append", default=[])
    parser.add_argument("--missing", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--wait-ms", type=int, default=6000)
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()

    codes = list(args.shortcode)
    if args.missing:
        codes += posts_missing_media(Path(args.db))
    codes = list(dict.fromkeys(codes))
    if args.limit:
        codes = codes[:args.limit]
    if not codes:
        print("Nothing to do. Use --shortcode or --missing.")
        return

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
    ok = 0
    for code in codes:
        try:
            if fetch_post(code, args, session):
                ok += 1
        except Exception as exc:
            print(f"{code}: {exc}", file=sys.stderr)
        time.sleep(1.5)
    print(f"done: {ok}/{len(codes)}")


if __name__ == "__main__":
    main()
