#!/usr/bin/env python3
"""Prove the current browser-session can download, comment and unsave posts.

No Instaloader: this uses Playwright with the Chrome cookie jar and the same
REST endpoints the Instagram web app calls.

Examples:
    python playwright_probe.py --shortcode DRheMYTEdbQ --comments 50
    python playwright_probe.py --shortcode Dfw85NLRrHi --unsave-check
    python playwright_probe.py --shortcode Dfw85NLRrHi --unsave-check --perform-unsave
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from playwright.sync_api import sync_playwright

from chrome_cookies import playwright_cookies
from download_saved import saved_api_headers

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
      const lines = text.split('\n').map((s) => s.trim()).filter(Boolean);
      const times = lines.filter((l) => /^\d+[smhdw]$/.test(l));
      const reply = lines.filter((l) => l === 'Reply');
      if (times.length === 1 && reply.length === 1 && lines.length >= 3) {
        if (containers.has(node)) break;
        containers.add(node);
        const user = lines[0];
        const ti = lines.findIndex((l) => /^\d+[smhdw]$/.test(l));
        const body = lines.slice(ti + 1)
          .filter((l) => !/^\d+\s+likes?$/.test(l) && l !== 'Reply'
                     && l !== 'See translation' && !/^View \d+ repl/.test(l))
          .join(' ');
        if (user && body) {
          const key = user + '|' + body;
          if (!seen.has(key)) { seen.add(key); out.push({ owner: user, text: body }); }
        }
        break;
      }
    }
  }
  return out;
}
"""


def media_id_for(shortcode: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    value = 0
    for char in shortcode:
        value = value * 64 + alphabet.index(char)
    return str(value)


def csrf_from(cookies: List[dict]) -> str:
    for cookie in cookies:
        if cookie.get("name") == "csrftoken":
            return cookie.get("value") or ""
    return ""


def saved_has(context, username: str, shortcode: str, pages: int = 8) -> bool:
    headers = saved_api_headers(username)
    max_id = None
    for _ in range(pages):
        url = "https://www.instagram.com/api/v1/feed/saved/posts/"
        if max_id:
            url += "?max_id=" + requests.utils.quote(str(max_id))
        response = context.request.get(url, headers=headers, timeout=45000)
        if response.status != 200:
            return False
        payload = response.json()
        for item in payload.get("items") or []:
            media = item.get("media") or item
            if media.get("code") == shortcode:
                return True
        if not payload.get("more_available") or not payload.get("next_max_id"):
            return False
        max_id = payload["next_max_id"]
    return False


def run(args: argparse.Namespace) -> None:
    username = args.username
    shortcode = args.shortcode
    media_id = media_id_for(shortcode)
    cookies = playwright_cookies()
    csrf = csrf_from(cookies)

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
        if cookies and not args.guest:
            context.add_cookies(cookies)
        page = context.new_page()

        # 1. Post page + media URLs from the network / DOM.
        media_urls: List[str] = []
        video_urls: List[str] = []

        def on_response(response):
            if "/api/v1/media/" not in response.url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            for item in payload.get("items") or []:
                if str(item.get("code") or "") != shortcode:
                    continue
                parts = item.get("carousel_media") or [item]
                for part in parts:
                    versions = part.get("video_versions") or []
                    if versions:
                        versions = sorted(
                            versions,
                            key=lambda v: (v.get("width") or 0) * (v.get("height") or 0),
                            reverse=True,
                        )
                        video_urls.append(versions[0]["url"])
                    else:
                        candidates = (part.get("image_versions2") or {}).get("candidates") or []
                        if candidates:
                            media_urls.append(candidates[0]["url"])

        page.on("response", on_response)
        response = page.goto(
            f"https://www.instagram.com/p/{shortcode}/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(3000)
        print(f"post page: HTTP {response.status if response else '?'} {page.url}")

        if not media_urls and not video_urls:
            media_urls = page.evaluate(
                """() => [...document.querySelectorAll('main img')]
                     .filter((img) => img.naturalWidth > 300)
                     .map((img) => img.currentSrc || img.src)
                     .slice(0, 20)"""
            )
        print(f"media urls: {len(media_urls)} images, {len(video_urls)} videos")
        if args.download_media and (media_urls or video_urls):
            folder = Path(args.posts) / args.folder if args.folder else Path(args.posts) / media_id
            folder.mkdir(parents=True, exist_ok=True)
            for index, url in enumerate(media_urls + video_urls, 1):
                suffix = ".mp4" if url in video_urls else ".jpg"
                target = folder / f"{shortcode}_web_{index}{suffix}"
                r = context.request.get(url, timeout=60000)
                if r.status == 200:
                    target.write_bytes(r.body())
                    print(f"downloaded {target.name} ({target.stat().st_size} bytes)")

        # 2. First N comments from the rendered DOM.
        if args.comments:
            try:
                button = page.get_by_text("View all", exact=False)
                if button.count():
                    button.first.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass
            comments: List[Dict[str, str]] = []
            seen = set()
            for _ in range(25):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(500)
                for comment in page.evaluate(EXTRACT_JS):
                    key = (comment.get("owner"), comment.get("text"))
                    if key in seen:
                        continue
                    seen.add(key)
                    comments.append(comment)
                if len(comments) >= args.comments:
                    break
            comments = comments[: args.comments]
            print(f"comments collected: {len(comments)}")
            if args.comments_file:
                Path(args.comments_file).write_text(
                    json.dumps(comments, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            print(json.dumps(comments[:5], ensure_ascii=False, indent=1))

        # 3. Unsave readiness. By default this only checks the saved feed and
        # reports whether the CSRF/session material is present.
        if args.unsave_check and not args.guest:
            headers = saved_api_headers(username)
            headers["X-CSRFToken"] = csrf
            is_saved = saved_has(context, username, shortcode, pages=args.saved_pages)
            print(f"saved before: {is_saved}")
            print(f"csrf token present: {bool(csrf)}")
            if args.perform_unsave and is_saved and csrf:
                unsave = context.request.post(
                    f"https://www.instagram.com/api/v1/media/{media_id}/unsave/",
                    headers=headers,
                    timeout=45000,
                )
                print(f"unsave: HTTP {unsave.status} {unsave.text()[:200]}")
                time.sleep(2)
                print(
                    "saved after unsave: "
                    f"{saved_has(context, username, shortcode, pages=args.saved_pages)}"
                )
                save = context.request.post(
                    f"https://www.instagram.com/api/v1/media/{media_id}/save/",
                    headers=headers,
                    timeout=45000,
                )
                print(f"re-save: HTTP {save.status} {save.text()[:200]}")
                time.sleep(2)
                print(
                    "saved after re-save: "
                    f"{saved_has(context, username, shortcode, pages=args.saved_pages)}"
                )
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Playwright capability probe")
    parser.add_argument("--username", default="shalevfrfr")
    parser.add_argument("--shortcode", required=True)
    parser.add_argument("--folder", default="")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--comments", type=int, default=0)
    parser.add_argument("--comments-file", default="")
    parser.add_argument("--download-media", action="store_true")
    parser.add_argument("--unsave-check", action="store_true")
    parser.add_argument("--perform-unsave", action="store_true")
    parser.add_argument("--saved-pages", type=int, default=8)
    parser.add_argument("--guest", action="store_true",
                        help="Public page only; no cookies or unsave")
    parser.add_argument("--headful", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
