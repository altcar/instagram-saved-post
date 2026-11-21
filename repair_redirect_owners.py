#!/usr/bin/env python3
"""Recover owners for deleted posts from Instagram's profile redirect.

When a post is gone, ``/p/<shortcode>/`` returns a 302 to
``/<owner>/``. That is enough to replace ``unknown`` with the real handle,
without needing the logged-in media-info endpoint.

    python repair_redirect_owners.py --workers 8
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple

import requests

import archive_db as adb

LOCK = threading.Lock()
LOCATION_RE = re.compile(r"^https?://www\.instagram\.com/([^/?#]+)/?$")


def lookup(session: requests.Session, shortcode: str) -> Tuple[str, Optional[str]]:
    try:
        response = session.get(
            f"https://www.instagram.com/p/{shortcode}/",
            allow_redirects=False,
            timeout=20,
        )
    except requests.RequestException:
        return shortcode, None
    location = response.headers.get("location") or ""
    match = LOCATION_RE.match(location)
    if response.status_code in (301, 302, 303, 307, 308) and match:
        owner = match.group(1)
        if owner not in {"explore", "reels", "accounts", "p"}:
            return shortcode, owner
    return shortcode, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover owners from 302 redirects")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    conn = adb.connect(Path(args.db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, shortcode FROM posts "
        "WHERE owner_username IS NULL OR trim(owner_username)='' "
        "ORDER BY sort_order, id"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"{len(rows)} owner gaps")

    local = threading.local()

    def session() -> requests.Session:
        if not hasattr(local, "session"):
            value = requests.Session()
            value.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://www.instagram.com/",
                }
            )
            local.session = value
        return local.session

    def work(row):
        return lookup(session(), row["shortcode"])

    fixed = 0
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(work, row) for row in rows]
        for future in as_completed(futures):
            code, owner = future.result()
            done += 1
            if owner:
                with LOCK:
                    conn.execute(
                        "UPDATE posts SET owner_username=? "
                        "WHERE shortcode=? AND (owner_username IS NULL OR trim(owner_username)='')",
                        (owner, code),
                    )
                    conn.commit()
                fixed += 1
            if done % 100 == 0 or done == len(rows):
                print(f"  {done}/{len(rows)} recovered={fixed}", flush=True)
    conn.close()
    print(f"done: {fixed} owners recovered from redirects")


if __name__ == "__main__":
    main()
