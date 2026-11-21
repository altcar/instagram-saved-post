#!/usr/bin/env python3
"""Browser fallback for posts whose media-info endpoint returns 400.

Some older Instagram ads and promoted posts exist on the public post page but
are not returned by ``/api/v1/media/<pk>/info/``. Playwright renders the page,
reads the real ``og:*`` metadata, and fills the owner, caption and cover.

This is intentionally separate from ``repair_archive.py`` (API first) and from
the daily pipeline. It only touches rows that still have gaps.

    python repair_archive_browser.py --dry-run
    python repair_archive_browser.py --limit 40 --workers 4
    python repair_archive_browser.py --workers 4
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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image
from playwright.sync_api import sync_playwright

import archive_db as adb
from chrome_cookies import playwright_cookies
from repair_archive import apply_report, invalidate_thumbs, row_gaps

DESC_RE = re.compile(
    r"^\s*(?P<likes>[\d.,]+[KMB]?)\s+likes?,\s+"
    r"(?P<comments>[\d.,]+[KMB]?)\s+comments?\s+-\s+"
    r"(?P<owner>\S+)\s+on\s+.*?:\s*\"(?P<caption>.*)\"",
    re.S,
)
TITLE_RE = re.compile(r"^.*? on Instagram:\s*\"(?P<caption>.*)\"\s*$", re.S)
URL_RE = re.compile(r"instagram\.com/([^/]+)/p/")


def parse_count(value: str) -> int:
    value = value.replace(",", "").strip().upper()
    try:
        if value.endswith("K"):
            return int(float(value[:-1]) * 1_000)
        if value.endswith("M"):
            return int(float(value[:-1]) * 1_000_000)
        if value.endswith("B"):
            return int(float(value[:-1]) * 1_000_000_000)
        return int(float(value))
    except ValueError:
        return 0


def to_jpeg(data: bytes) -> Optional[bytes]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, "JPEG", quality=90)
            return out.getvalue()
    except Exception:
        return None


class BrowserWorker:
    def __init__(self, cookies: List[dict], user_agent: str):
        self.cookies = cookies
        self.user_agent = user_agent

    def __enter__(self):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self.context = self.browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1280, "height": 900},
        )
        if self.cookies:
            self.context.add_cookies(self.cookies)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Referer": "https://www.instagram.com/",
            }
        )
        return self

    def __exit__(self, *exc):
        try:
            self.context.close()
            self.browser.close()
        finally:
            self._pw.stop()

    def repair(self, row: sqlite3.Row, gaps: List[str]) -> Dict[str, Any]:
        code = row["shortcode"]
        report: Dict[str, Any] = {
            "id": row["id"],
            "shortcode": code,
            "gaps": gaps,
            "ok": False,
        }
        page = self.context.new_page()
        try:
            page.goto(
                f"https://www.instagram.com/p/{code}/",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            page.wait_for_timeout(1400)
            meta = page.evaluate(
                """() => {
                  const out = {};
                  document.querySelectorAll('meta[property],meta[name]').forEach((el) => {
                    const key = el.getAttribute('property') || el.getAttribute('name');
                    if (/^(og:|description)/.test(key)) out[key] = el.content;
                  });
                  return out;
                }"""
            )
            final_url = page.url
            desc = meta.get("og:description") or meta.get("description") or ""
            title = meta.get("og:title") or ""
            image = meta.get("og:image") or ""
            profile = re.match(
                r"https?://www\.instagram\.com/([^/?#]+)/?$", final_url
            )
            if profile and "/p/" not in final_url:
                # Deleted/blocked post: Instagram redirects to its owner's
                # profile. The owner is still useful metadata.
                owner = profile.group(1)
                if owner in {"explore", "reels", "accounts", "p"}:
                    report["error"] = "post unavailable"
                    return report
                report.update(
                    {
                        "ok": True,
                        "new_owner": row["owner_username"] or owner,
                        "new_owner_id": row["owner_id"],
                        "new_caption": row["caption"] or "",
                        "new_taken": row["taken_at"],
                        "new_likes": row["likes"] or 0,
                        "new_video": bool(row["is_video"]),
                        "new_typename": row["typename"],
                        "new_location": row["location"],
                        "new_hashtags": json.loads(row["hashtags"] or "[]"),
                        "new_media_id": row["media_id"],
                        "new_cover": row["cover"],
                        "redirect_owner": True,
                    }
                )
                return report
            if "/p/" not in final_url or not image:
                report["error"] = "post unavailable"
                return report

            match = DESC_RE.match(desc)
            owner = match.group("owner") if match else None
            if not owner:
                url_match = URL_RE.search(meta.get("og:url") or final_url)
                owner = url_match.group(1) if url_match else None
            caption = ""
            if match:
                caption = match.group("caption") or ""
            else:
                title_match = TITLE_RE.match(title)
                if title_match:
                    caption = title_match.group("caption") or ""
            if not caption:
                # Caption is often displayed verbatim in a single span.
                caption = page.evaluate(
                    """() => {
                      for (const span of document.querySelectorAll('span[style*="line-height"]')) {
                        const text = (span.innerText || '').trim();
                        if (text.length > 2) return text;
                      }
                      return '';
                    }"""
                ) or ""

            likes = parse_count(match.group("likes")) if match else (row["likes"] or 0)
            folder = Path(row["folder"]) if row["folder"] else None
            report.update(
                {
                    "ok": True,
                    "new_owner": row["owner_username"] or owner,
                    "new_owner_id": row["owner_id"],
                    "new_caption": row["caption"]
                    if (row["caption"] or "").strip()
                    else caption,
                    "new_taken": row["taken_at"],
                    "new_likes": likes,
                    "new_video": bool(row["is_video"]),
                    "new_typename": row["typename"],
                    "new_location": row["location"],
                    "new_hashtags": re.findall(
                        r"#(\w+)",
                        row["caption"] if (row["caption"] or "").strip() else caption,
                    ),
                    "new_media_id": row["media_id"],
                }
            )
            if folder:
                folder = Path("data/posts") / folder
                cover = folder / "cover.jpg"
                if not cover.is_file() or cover.stat().st_size <= 1024:
                    data = None
                    try:
                        response = self.session.get(image, timeout=45)
                        if response.status_code == 200 and response.content:
                            data = response.content
                    except requests.RequestException:
                        pass
                    if data:
                        if not data.startswith(b"\xff\xd8"):
                            data = to_jpeg(data) or data
                        folder.mkdir(parents=True, exist_ok=True)
                        cover.write_bytes(data)
                        report["new_cover"] = f"{folder.name}/cover.jpg"
            report.setdefault("new_cover", row["cover"])
            report["caption_len"] = len(report["new_caption"] or "")
            return report
        except Exception as exc:
            report["error"] = str(exc)[:160]
            return report
        finally:
            page.close()


def worker_chunk(
    tasks: List[Tuple[sqlite3.Row, List[str]]],
    cookies: List[dict],
    user_agent: str,
) -> List[Tuple[sqlite3.Row, Dict[str, Any]]]:
    out = []
    worker = BrowserWorker(cookies, user_agent)
    worker.__enter__()
    try:
        for row, gaps in tasks:
            try:
                report = worker.repair(row, gaps)
            except Exception as exc:
                report = {
                    "id": row["id"],
                    "shortcode": row["shortcode"],
                    "gaps": gaps,
                    "ok": False,
                    "error": f"browser crashed: {str(exc)[:120]}",
                }
                # Chrome occasionally closes a renderer during a long batch;
                # start a fresh browser and carry on with the next post.
                try:
                    worker.__exit__(None, None, None)
                except Exception:
                    pass
                worker = BrowserWorker(cookies, user_agent)
                worker.__enter__()
            out.append((row, report))
            time.sleep(0.2)
    finally:
        try:
            worker.__exit__(None, None, None)
        except Exception:
            pass
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser repair for old posts")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--cache", default="data/.cache")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch", type=int, default=40)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--include-empty-caption", action="store_true")
    parser.add_argument("--guest", action="store_true",
                        help="Use a logged-out browser (public posts only)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
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
        keep = "owner" in gaps or "cover" in gaps or "media" in gaps
        if args.include_empty_caption and "caption" in gaps:
            keep = True
        if keep:
            tasks.append((row, gaps))
    if args.limit:
        tasks = tasks[: args.limit]
    print(f"{len(tasks)} browser repairs pending")
    if args.dry_run or not tasks:
        conn.close()
        return

    cookies = [] if args.guest else playwright_cookies()
    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
    )
    done = 0
    ok = 0
    failed = 0
    for start in range(0, len(tasks), args.batch):
        batch = tasks[start : start + args.batch]
        chunks = [batch[i:: args.workers] for i in range(args.workers)]
        results: List[Tuple[sqlite3.Row, Dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [
                pool.submit(worker_chunk, chunk, cookies, user_agent)
                for chunk in chunks
                if chunk
            ]
            for future in as_completed(futures):
                results.extend(future.result())
        for row, report in results:
            done += 1
            if report.get("ok"):
                ok += 1
                apply_report(conn, row, report, media_root)
                invalidate_thumbs(Path(args.cache), row["id"])
            else:
                failed += 1
                if args.verbose:
                    print(
                        f"  FAIL {report.get('shortcode')} "
                        f"{report.get('error', 'unknown')}",
                        flush=True,
                    )
        print(
            f"  {done}/{len(tasks)} ok={ok} failed={failed} "
            f"(batch {start // args.batch + 1})",
            flush=True,
        )
    conn.close()
    print(f"done: {ok} repaired, {failed} unavailable")


if __name__ == "__main__":
    main()
