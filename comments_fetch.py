#!/usr/bin/env python3
"""Fetch comments from the post page DOM and save them per folder.

Instagram's old comments API is retired; the comments are rendered in the post
page, so this uses Playwright with Chrome cookies, expands the comment list,
and extracts each comment row.

    python comments_fetch.py --shortcode Df7r0X2ivOB
    python comments_fetch.py --missing --limit 20
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from chrome_cookies import playwright_cookies

EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const containers = new Set();
  for (const a of document.querySelectorAll('a[href^="/"][role="link"]')) {
    const href = a.getAttribute('href') || '';
    if (!href.endsWith('/') || href.startsWith('/p/') || href.includes('/explore/')
        || href.includes('/reels/') || href.includes('/accounts/')) continue;
    let node = a;
    for (let depth = 0; depth < 10 && node; depth++, node = node.parentElement) {
      const text = (node.innerText || '').trim();
      const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
      const times = lines.filter(l => /^\d+[smhdw]$/.test(l));
      const reply = lines.filter(l => l === 'Reply');
      if (times.length === 1 && reply.length === 1 && lines.length >= 3) {
        if (containers.has(node)) break;
        containers.add(node);
        const user = lines[0];
        const ti = lines.findIndex(l => /^\d+[smhdw]$/.test(l));
        const body = lines.slice(ti + 1)
          .filter(l => !/^\d+\s+likes?$/.test(l) && l !== 'Reply'
                    && l !== 'See translation' && !/^View \d+ repl/.test(l))
          .join(' ');
        if (user && body) {
          const key = user + '|' + body;
          if (!seen.has(key)) { seen.add(key); out.push({owner: user, text: body}); }
        }
        break;
      }
    }
  }
  return out;
}
"""


def comments_text(comments: List[Dict[str, Any]]) -> str:
    return "".join(f"{c.get('owner','?')}: {c.get('text','')}\n" for c in comments)


def merge_comments(existing: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = {(c.get("owner"), c.get("text")) for c in existing}
    result = list(existing)
    for comment in new:
        key = (comment.get("owner"), comment.get("text"))
        if key not in seen:
            seen.add(key)
            result.append(comment)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch post comments from the page")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--shortcode", action="append", default=[])
    parser.add_argument("--missing", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--wait-ms", type=int, default=4000)
    parser.add_argument("--max-comments", type=int, default=0,
                        help="Stop after this many DOM comments (0 = current view)")
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    rows: List[sqlite3.Row] = []
    if args.shortcode:
        for code in args.shortcode:
            row = conn.execute(
                "SELECT id, shortcode, folder, owner_username, caption FROM posts WHERE shortcode=?",
                (code,),
            ).fetchone()
            if row:
                rows.append(row)
    if args.missing:
        # Mark posts that already have comments (from old JSON migrations) as done.
        for row in conn.execute(
            "SELECT id, folder FROM posts WHERE folder IS NOT NULL"
        ).fetchall():
            cfile = Path(args.posts) / row["folder"] / "comments.json"
            if cfile.is_file() and cfile.stat().st_size > 4:
                conn.execute(
                    "UPDATE posts SET comments_fetched_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), row["id"]),
                )
        conn.commit()
        rows = conn.execute(
            "SELECT id, shortcode, folder, owner_username, caption FROM posts "
            "WHERE folder IS NOT NULL AND comments_fetched_at IS NULL "
            "ORDER BY sort_order"
        ).fetchall()
    conn.close()

    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("Nothing to do. Use --shortcode or --missing.")
        return

    folders = {row["shortcode"]: row["folder"] for row in rows}
    owners = {row["shortcode"]: row["owner_username"] for row in rows}
    captions = {row["shortcode"]: (row["caption"] or "")[:80] for row in rows}

    saved = 0
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
        for index, row in enumerate(rows, 1):
            code = row["shortcode"]
            folder = Path(args.posts) / folders[code]
            if not folder.is_dir():
                continue
            page = context.new_page()
            try:
                page.goto(
                    f"https://www.instagram.com/p/{code}/",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_timeout(args.wait_ms)
                try:
                    button = page.get_by_text("View all", exact=False)
                    if button.count():
                        button.first.click()
                        page.wait_for_timeout(1500)
                except Exception:
                    pass
                comments: List[Dict[str, Any]] = []
                seen_comments = set()
                rounds = 3 if not args.max_comments else 20
                for _ in range(rounds):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(450)
                    for comment in page.evaluate(EXTRACT_JS):
                        key = (comment.get("owner"), comment.get("text"))
                        if key in seen_comments:
                            continue
                        seen_comments.add(key)
                        comments.append(comment)
                    if args.max_comments and len(comments) >= args.max_comments:
                        break
                owner = owners.get(code)
                caption = captions.get(code) or ""
                cleaned = []
                for comment in comments:
                    if args.max_comments and len(cleaned) >= args.max_comments:
                        break
                    text = comment.get("text") or ""
                    if len(text) > 2000:
                        continue
                    if owner and comment.get("owner") == owner and len(text) > 500:
                        continue
                    if caption and caption in text and len(text) > 500:
                        continue
                    cleaned.append(comment)
                cfile = folder / "comments.json"
                existing = []
                if cfile.is_file():
                    try:
                        existing = json.loads(cfile.read_text(encoding="utf-8"))
                    except Exception:
                        existing = []
                merged = merge_comments(existing, cleaned)
                if len(merged) > len(existing):
                    cfile.write_text(
                        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    (folder / "comments.txt").write_text(
                        comments_text(merged), encoding="utf-8"
                    )
                    saved += 1
                try:
                    conn2 = sqlite3.connect(args.db, timeout=30)
                    conn2.execute(
                        "UPDATE posts SET comments_fetched_at=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), row["id"]),
                    )
                    conn2.commit()
                    conn2.close()
                except Exception:
                    pass
                print(f"{index}/{len(rows)} {code}: {len(cleaned)} comments", flush=True)
            except Exception as exc:
                print(f"{code}: {exc}", file=sys.stderr)
            finally:
                page.close()
        browser.close()
    print(f"saved comments for {saved} posts")


if __name__ == "__main__":
    main()
