#!/usr/bin/env python3
"""Download real cover images for video/sidecar posts, in parallel.

Reads the raw feed items stored by fetch_raw.py, resolves each post's folder
through the SQLite index, and writes <folder>/cover.jpg. Skips covers that
already exist, so it is safe to re-run.

    python fetch_covers.py --raw data/1911/raw --db data/archive.db --workers 8
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from PIL import Image

DB_LOCK = threading.Lock()


def candidate_urls(media: Dict) -> List[str]:
    urls: List[str] = []
    for source in (media, *(media.get("carousel_media") or [])):
        versions = source.get("image_versions2") or {}
        candidates = versions.get("candidates") or []
        if candidates and candidates[0].get("url"):
            urls.append(candidates[0]["url"])
    return urls


def to_jpeg(data: bytes) -> Optional[bytes]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, "JPEG", quality=90)
            return out.getvalue()
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch cover images in parallel")
    parser.add_argument("--raw", default="data/1911/raw/items.jsonl")
    parser.add_argument("--media", default="data/1911/saved_media")
    parser.add_argument("--db", default="data/1911/archive.db")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    media_root = Path(args.media)
    db = sqlite3.connect(args.db, timeout=60, check_same_thread=False)
    db.row_factory = sqlite3.Row
    folders: Dict[str, str] = {
        row["shortcode"]: row["folder"]
        for row in db.execute("SELECT shortcode, folder FROM posts WHERE folder IS NOT NULL")
    }
    done: Dict[str, str] = {
        row["shortcode"]: row["cover"]
        for row in db.execute("SELECT shortcode, cover FROM posts WHERE cover IS NOT NULL")
    }

    tasks: List[Tuple[str, List[str], Path]] = []
    seen = set()
    with open(args.raw, encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            media = item.get("media") or item
            code = media.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            folder = folders.get(code)
            if not folder:
                continue
            folder_path = media_root / folder
            cover = folder_path / "cover.jpg"
            if cover.is_file() and cover.stat().st_size > 1024:
                continue
            if done.get(code):
                existing = media_root / done[code]
                if existing.is_file():
                    continue
            urls = candidate_urls(media)
            if urls:
                tasks.append((code, urls, folder_path))

    print(f"{len(tasks)} covers to fetch")
    local = threading.local()
    lock = threading.Lock()
    count = 0
    failed = 0

    def get_session() -> requests.Session:
        if not hasattr(local, "session"):
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
            local.session = session
        return local.session

    def work(task: Tuple[str, List[str], Path]) -> bool:
        code, urls, folder = task
        session = get_session()
        for url in urls:
            try:
                response = session.get(url, timeout=30)
                if response.status_code != 200 or not response.content:
                    continue
                data = response.content
                if not data.startswith(b"\xff\xd8"):
                    data = to_jpeg(data) or data
                cover = folder / "cover.jpg"
                cover.write_bytes(data)
                rel = f"{folder.parent.name}/{folder.name}/cover.jpg"
                with DB_LOCK:
                    db.execute(
                        "UPDATE posts SET cover = ? WHERE shortcode = ?",
                        (f"{folder.name}/cover.jpg", code),
                    )
                    db.commit()
                return True
            except requests.RequestException:
                continue
        return False

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, task) for task in tasks]
        for future in as_completed(futures):
            with lock:
                count += 1
                if not future.result():
                    failed += 1
                if count % 100 == 0:
                    print(f"  {count}/{len(tasks)} (failed {failed})", flush=True)

    db.close()
    print(f"done: {count - failed} covers, {failed} failed")


if __name__ == "__main__":
    main()
