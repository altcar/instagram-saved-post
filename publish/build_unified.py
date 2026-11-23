#!/usr/bin/env python3
"""Build the single static site served from altcar/instagram-saved-post.

Every platform manifest in ``publish/manifests/`` becomes a source in one
combined gallery manifest.  All media/thumbnail/comment URLs point at the
Hugging Face dataset; the repository only carries the app shell, the manifest
and the five legacy Instagram thumbnails, so the GitHub build stays tiny.

    python publish/build_unified.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "publish/site-unified"
SRC = ROOT / "publish/site-test"
MANIFESTS = ROOT / "publish/manifests"
GIST_ID = "5dc24371e7371776648695269b30cfd2"
ASSET_VERSION = 57

# Gallery source id -> (platform, hub route, label, subtitle, id base)
PLATFORMS = [
    ("shalevfrfr", "instagram", "instagram", "Instagram · shalevfrfr",
     "HF-backed media · persistent edits", 0),
    ("threads", "threads", "threads", "Threads",
     "Threads saves · HF-backed media", 300_000),
    ("x", "x", "twitter", "X / Twitter",
     "X bookmarks · HF-backed media", 400_000),
    ("rednote", "rednote", "rednote", "RedNote",
     "RedNote favourites · HF-backed media", 500_000),
    ("reddit", "reddit", "reddit", "Reddit",
     "Reddit saves · HF-backed media", 600_000),
]

PRESETS = ["art", "sexy", "erotic", "ai-robotics", "ai-skills", "artist", "meme",
           "music", "ad", "food", "travel", "fitness", "tech", "fashion",
           "design", "news", "other"]


def stable_id(pk: str, base: int, used: set[int]) -> int:
    """Deterministic numeric id derived from the platform primary key."""
    n = zlib.crc32(pk.encode("utf-8")) % 900_000
    candidate = base + n
    while candidate in used:
        candidate += 1
    used.add(candidate)
    return candidate


def load_manifest(platform: str) -> dict:
    path = MANIFESTS / f"{platform}.json"
    if not path.is_file():
        return {"posts": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"warning: {path} unreadable ({exc})")
        return {"posts": []}


def convert(post: dict, platform: str, base: int, used: set[int]) -> dict:
    media = []
    for item in post.get("media") or []:
        if not item.get("url"):
            continue
        media.append({
            "i": item.get("i", len(media)),
            "kind": item.get("kind", "image"),
            "url": item["url"],
            "thumb": (f"https://huggingface.co/datasets/shalevlau/ig-archive-test/"
                      f"resolve/main/{item['thumb_rel']}"
                      if item.get("thumb_rel") else post.get("thumb") or ""),
        })
    pid = post.get("legacy_id") if platform == "instagram" else None
    if pid is None:
        pid = stable_id(post["pk"], base, used)
    else:
        used.add(pid)
    return {
        "id": pid,
        "shortcode": post.get("shortcode") or post["pk"],
        "owner": post.get("owner") or "",
        "owner_name": post.get("owner_name") or post.get("owner") or "",
        "owner_url": post.get("owner_url") or "",
        "permalink": post.get("permalink") or "",
        "caption": post.get("caption") or "",
        "caption_full": post.get("caption_full") or post.get("caption") or "",
        "likes": post.get("likes") or 0,
        "taken_at": post.get("taken_at") or "",
        "is_video": bool(post.get("is_video")),
        "typename": "GraphVideo" if post.get("is_video") else "GraphImage",
        "location": post.get("owner_name") or "",
        "category": (post.get("categories") or [None])[0],
        "categories": post.get("categories") or [],
        "confidence": None,
        "source": platform if platform != "instagram" else (post.get("source") or "shalevfrfr"),
        "platform": platform,
        "notes": "",
        "tags": post.get("tags") or [],
        "media_count": len(media),
        "media": media,
        "thumb": post.get("thumb") or (media[0]["thumb"] if media else ""),
        "cover": post.get("cover") or (media[0]["url"] if media else ""),
        "avatar": post.get("avatar") or "",
        "comments_url": post.get("comments_url") or "",
        "comments_count": post.get("comments_count") or 0,
        "vision_caption": None,
        "transcript": None,
        "ocr_text": None,
        "folder": post["pk"],
    }


def main() -> None:
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True)
    gallery = SITE / "gallery"
    gallery.mkdir(parents=True)
    # Use the current shared gallery shell, not the old site-test snapshot.
    for name in ("app.js", "style.css"):
        shutil.copyfile(ROOT / "web" / name, gallery / name)
    shutil.copyfile(ROOT / "publish/static-api.js", gallery / "static-api.js")
    overrides_file = ROOT / "publish/static_overrides.json"
    if overrides_file.is_file():
        shutil.copyfile(overrides_file, gallery / "static_overrides.json")
        shutil.copyfile(overrides_file, SITE / "static_overrides.json")
    # The manifest fetch inside static-api.js has a hard-coded cache buster;
    # stamp it per build so a deploy is never served from the Pages CDN cache.
    stamp = time.strftime("%Y%m%d%H%M")
    api_js = gallery / "static-api.js"
    api_js.write_text(
        re.sub(r"manifest\.json\?nc=\d+", f"manifest.json?nc={stamp}",
               api_js.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    for name in ("thumbs", "comments"):
        src = SRC / name
        if src.is_dir():
            shutil.copytree(src, gallery / name)
    index = (ROOT / "web/index.html").read_text(encoding="utf-8")
    index = index.replace("/static/style.css", "style.css")
    index = index.replace("/static/app.js", "app.js")
    index = re.sub(r"style\.css\?v=\d+", f"style.css?v={ASSET_VERSION}", index)
    index = re.sub(r"app\.js\?v=\d+", f"app.js?v={ASSET_VERSION}", index)
    index = index.replace(
        f'<script src="app.js?v={ASSET_VERSION}"></script>',
        f'<script src="config.js"></script>\n'
        f'<script src="static-api.js?v={ASSET_VERSION}"></script>\n'
        f'<script src="app.js?v={ASSET_VERSION}"></script>')
    (gallery / "index.html").write_text(index, encoding="utf-8")

    posts: list[dict] = []
    accounts = []
    per_source: dict[str, int] = {}
    for source_id, platform, route, label, subtitle, base in PLATFORMS:
        if platform == "instagram":
            # Instagram posts already carry their numeric ids and local thumbs.
            manifest = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
            rows = manifest.get("posts", [])
            used: set[int] = set()
            for row in rows:
                row.setdefault("platform", "instagram")
                row["source"] = source_id
                # Covers were never uploaded for the legacy batch; the local
                # 360px thumb is always present, so use it as the cover.
                row["cover"] = row.get("thumb") or row.get("cover") or ""
                # The linked comment file is part of the small IG test set.
                cfile = gallery / "comments" / f"{row['id']}.json"
                if cfile.is_file():
                    try:
                        row["comments_count"] = len(json.loads(cfile.read_text(encoding="utf-8")))
                        row["comments_url"] = f"comments/{row['id']}.json"
                    except Exception:
                        pass
                used.add(row["id"])
                posts.append(row)
            count = len(rows)
        else:
            used = set()
            data = load_manifest(platform)
            rows = [p for p in data.get("posts", []) if p.get("hf_ready")]
            for row in rows:
                posts.append(convert(row, platform, base, used))
            count = len(rows)
        per_source[source_id] = count
        accounts.append({
            "id": source_id,
            "platform": platform,
            "label": label,
            "handle": "",
            "total": count,
            "videos": sum(1 for p in posts if p.get("platform") == platform and p.get("is_video")),
        })

    posts.sort(key=lambda p: (p.get("taken_at") or ""), reverse=True)
    facets = {
        "total": len(posts),
        "videos": sum(1 for p in posts if p.get("is_video")),
        "ads": 0,
        "filter_counts": {
            "no_video": sum(1 for p in posts if not p.get("is_video")),
            "no_image": sum(1 for p in posts if p.get("is_video")),
            "no_ads": len(posts),
            "bad_owner": sum(1 for p in posts if not p.get("owner")),
            "bad_cover": sum(1 for p in posts if not p.get("cover")),
            "caption_empty": sum(1 for p in posts if not (p.get("caption_full") or "").strip()),
            "caption_error": 0,
            "bad_shortcode": sum(1 for p in posts if not p.get("shortcode")),
            "comments_empty": sum(1 for p in posts if not p.get("comments_url")),
            "comments_error": 0,
            "still_saved": 0,
            "already_unsaved": len(posts),
            "uncategorised": sum(1 for p in posts if not p.get("categories")),
        },
        "categories": [],
        "category_list": [],
        "tags": [],
        "presets": PRESETS,
        "saved": {"still_saved": 0, "already_unsaved": len(posts)},
        "sources": per_source,
    }
    manifest = {"generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
                "posts": posts, "facets": facets}
    (gallery / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    accounts_doc = {"accounts": accounts, "active": "shalevfrfr"}
    for target in (SITE / "accounts.json", gallery / "accounts.json"):
        target.write_text(json.dumps(accounts_doc, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    cloud = json.dumps({"gistId": GIST_ID, "gistFile": "static_overrides.json"})
    site_base = os.environ.get("SITE_BASE", "/instagram-saved-post/")
    hub = {
        "siteBase": site_base,
        "accountsApi": "accounts.json",
        "backends": {route: "gallery/?embed=1" for _s, _p, route, _l, _sub, _b in PLATFORMS},
        "platforms": {route: platform for _s, platform, route, _l, _sub, _b in PLATFORMS},
        "defaultSources": {route: source for source, _p, route, _l, _sub, _b in PLATFORMS},
        "titles": {route: label for _s, _p, route, label, _sub, _b in PLATFORMS},
        "subtitles": {route: subtitle for _s, _p, route, _l, subtitle, _b in PLATFORMS},
    }
    # Telegram is not in the Hugging Face manifest: its UI is static, but the
    # messages and media come from the tg-archiver server on the local PC.
    hub["backends"]["telegram"] = "telegram/"
    hub["titles"]["telegram"] = "Telegram"
    hub["subtitles"]["telegram"] = "Local archive · files on this computer"
    telegram_src = ROOT / "publish/telegram-ui"
    telegram_dst = SITE / "telegram"
    shutil.copytree(telegram_src, telegram_dst)

    config_text = (
        "window.SAVED_CLOUD = " + cloud + ";\n"
        + "window.SAVED_ACCOUNTS = " + json.dumps(accounts_doc, ensure_ascii=False) + ";\n"
        + "window.SAVED_HUB = " + json.dumps(hub, ensure_ascii=False) + ";\n"
    )
    for target in (SITE / "config.js", gallery / "config.js"):
        target.write_text(config_text, encoding="utf-8")

    hook = '<script src="config.js"></script>\n'

    # Hub shell at the site root.
    hub_src = ROOT / "hub/public"
    for name in ("index.html", "app.css", "app.js"):
        shutil.copyfile(hub_src / name, SITE / name)
    root_index = (SITE / "index.html").read_text(encoding="utf-8")
    if 'src="config.js"' not in root_index:
        root_index = root_index.replace('<script src="app.js?v=3"></script>',
                                        hook + '<script src="app.js?v=3"></script>')
    (SITE / "index.html").write_text(root_index, encoding="utf-8")
    (SITE / "404.html").write_text(root_index, encoding="utf-8")

    # Static platform routes so GitHub Pages returns 200 instead of custom 404.
    nested = (root_index.replace('href="app.css?v=3"', 'href="../app.css?v=3"')
               .replace('src="app.js?v=3"', 'src="../app.js?v=3"')
               .replace('src="config.js"', 'src="../config.js"'))
    for route in hub["backends"]:
        if route == "telegram":
            continue  # the real Telegram UI is served at /telegram/
        target = SITE / route
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text(nested, encoding="utf-8")
        (target / "404.html").write_text(nested, encoding="utf-8")

    size = sum(p.stat().st_size for p in SITE.rglob("*") if p.is_file())
    print(f"unified site: {SITE} ({len(posts)} posts, {size / 1e6:.2f} MB)")
    for source_id, _p, _r, _l, _s, _b in PLATFORMS:
        print(f"  {source_id:12s} {per_source[source_id]:4d} posts")


if __name__ == "__main__":
    main()
