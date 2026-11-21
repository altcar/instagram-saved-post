#!/usr/bin/env python3
"""Fetch the raw saved-post feed (metadata only) and optional cover images.

This mirrors what the Instagram web app requests on the "All posts" saved page,
stores every raw item for ad/cover analysis, and downloads the real cover image
for video posts (the CDN thumbnail, which is often missing from Instaloader's
download). It is resumable.

Examples:
    export IG_SESSIONID='...'
    python fetch_raw.py --out data/raw --media data/posts \
        --db data/archive.db --covers
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from download_saved import SAVED_POSTS_URL, saved_api_headers


def make_session(sessionid: str) -> requests.Session:
    session = requests.Session()
    session.cookies.set("sessionid", sessionid, domain=".instagram.com")
    user_id = sessionid.split("%3A", 1)[0]
    if user_id.isdigit():
        session.cookies.set("ds_user_id", user_id, domain=".instagram.com")
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def load_state(path: Path) -> Dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"page": 0, "next_max_id": None, "count": 0, "done": False}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def cover_candidates(media: Dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for source in (media, *(media.get("carousel_media") or [])):
        versions = source.get("image_versions2") or {}
        for candidate in versions.get("candidates") or []:
            url = candidate.get("url")
            if url:
                urls.append(url)
                break
    return urls


def folder_for(shortcode: str, media_root: Path) -> Optional[Path]:
    matches = sorted(media_root.glob(f"*_{shortcode}"))
    if matches:
        return matches[0]
    return None


def download_cover(
    session: requests.Session,
    media: Dict[str, Any],
    folder: Path,
    db: Optional[sqlite3.Connection],
) -> bool:
    cover = folder / "cover.jpg"
    if cover.is_file() and cover.stat().st_size > 1024:
        return False
    for url in cover_candidates(media):
        try:
            response = session.get(url, timeout=30)
            if response.status_code == 200 and response.content:
                cover.write_bytes(response.content)
                if db is not None:
                    db.execute(
                        "UPDATE posts SET cover = ? WHERE shortcode = ?",
                        (f"{folder.name}/cover.jpg", media.get("code")),
                    )
                    db.commit()
                return True
        except requests.RequestException:
            continue
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch raw saved feed metadata")
    parser.add_argument("--out", default="data/raw")
    parser.add_argument("--media", default="data/posts")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--sessionid", default=os.environ.get("IG_SESSIONID", ""))
    parser.add_argument("--sleep", type=float, default=1.5)
    parser.add_argument("--covers", action="store_true", default=True)
    parser.add_argument("--no-covers", dest="covers", action="store_false")
    parser.add_argument("--fresh", action="store_true", help="Restart from page 1 and replace items.jsonl")
    args = parser.parse_args()

    if not args.sessionid:
        raise SystemExit("Provide --sessionid or IG_SESSIONID")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pages_dir = out / "pages"
    pages_dir.mkdir(exist_ok=True)
    items_path = out / "items.jsonl"
    state_path = out / "state.json"
    media_root = Path(args.media)
    state = load_state(state_path)
    if args.fresh:
        state = {"page": 0, "next_max_id": None, "count": 0, "done": False}
        if items_path.exists():
            items_path.unlink()
        save_state(state_path, state)

    db = None
    if Path(args.db).exists():
        db = sqlite3.connect(args.db)
        db.row_factory = sqlite3.Row

    session = make_session(args.sessionid)
    headers = saved_api_headers("shalevfrfr")

    if state.get("done"):
        print("Raw fetch already complete.")
        return

    items_file = items_path.open("w" if args.fresh else "a", encoding="utf-8")
    covers_done = 0
    try:
        while True:
            page_no = int(state.get("page", 0)) + 1
            params = {"max_id": state["next_max_id"]} if state.get("next_max_id") else {}
            response = session.get(
                SAVED_POSTS_URL, headers=headers, params=params, timeout=30
            )
            if response.status_code == 429:
                print("Rate limited (429); backing off 60s", flush=True)
                time.sleep(60)
                continue
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                raise SystemExit("Instagram returned HTML; session expired.")

            (pages_dir / f"page_{page_no:04d}.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
            items = data.get("items") or []
            for item in items:
                items_file.write(json.dumps(item, ensure_ascii=False) + "\n")
                media = item.get("media") or item
                if args.covers and media.get("media_type") in (2, 8):
                    folder = folder_for(media.get("code", ""), media_root)
                    if folder:
                        if download_cover(session, media, folder, db):
                            covers_done += 1
                            if covers_done % 50 == 0:
                                print(f"  covers: {covers_done}", flush=True)
            items_file.flush()

            state["page"] = page_no
            state["count"] = int(state.get("count", 0)) + len(items)
            state["next_max_id"] = data.get("next_max_id")
            state["done"] = not data.get("more_available") or not state["next_max_id"]
            save_state(state_path, state)
            print(
                f"page {page_no}: +{len(items)} items, total {state['count']}, "
                f"covers {covers_done}",
                flush=True,
            )
            if state["done"]:
                print("Raw fetch complete.")
                break
            time.sleep(args.sleep)
    finally:
        items_file.close()
        if db is not None:
            db.close()


if __name__ == "__main__":
    main()
