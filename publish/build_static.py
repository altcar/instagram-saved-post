#!/usr/bin/env python3
"""Build a read-only static site for GitHub Pages from the archive DB.

Media is expected to live in six public repos (see partition_archive.py). The
site itself only contains the manifest, thumbnails and comments, so it stays
well below the 1 GB Pages soft limit.

    python publish/build_static.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data/archive.db"
POSTS = ROOT / "data/posts"
CACHE_THUMBS = ROOT / "data/.cache/thumbs"
OUT = ROOT / "publish/site"
BUCKETS = ROOT / "publish/buckets.json"
GITHUB_USER = "altcar"

REPO_CAPACITY = [8.6, 9.3, 9.3, 9.3, 9.3, 9.3]  # GiB; repo 1 also carries docs/


def folder_sizes() -> dict[str, int]:
    result = {}
    for folder in POSTS.iterdir():
        if not folder.is_dir():
            continue
        total = 0
        for path in folder.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        result[folder.name] = total
    return result


def make_buckets() -> dict[str, int]:
    if BUCKETS.is_file():
        return json.loads(BUCKETS.read_text())
    sizes = folder_sizes()
    cap = [int(x * 1024**3) for x in REPO_CAPACITY]
    used = [0] * len(cap)
    assigned: dict[str, int] = {}
    for folder, size in sorted(sizes.items(), key=lambda kv: -kv[1]):
        options = [i for i in range(len(cap)) if used[i] + size <= cap[i]]
        target = min(options, key=lambda i: used[i]) if options else min(
            range(len(cap)), key=lambda i: used[i]
        )
        assigned[folder] = target
        used[target] += size
    data = {
        "repos": [f"ig-archive-{i + 1}" for i in range(len(cap))],
        "folders": assigned,
        "used": used,
    }
    BUCKETS.parent.mkdir(parents=True, exist_ok=True)
    BUCKETS.write_text(json.dumps(data, indent=1))
    return data


def raw_url(repo: str, rel: str) -> str:
    safe = "/".join(quote(part) for part in rel.split("/"))
    return f"https://raw.githubusercontent.com/{GITHUB_USER}/{repo}/main/{safe}"


def make_thumb(post_id: int, cover_rel: str | None, target: Path) -> bool:
    if target.is_file():
        return True
    source = CACHE_THUMBS / f"{post_id}_0_360.jpg"
    if source.is_file():
        shutil.copyfile(source, target)
        return True
    if not cover_rel:
        return False
    cover = POSTS / cover_rel
    if not cover.is_file():
        return False
    try:
        with Image.open(cover) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((360, 720))
            img.save(target, "JPEG", quality=82, optimize=True)
        return True
    except Exception:
        return False


def main() -> None:
    buckets = make_buckets()
    repo_names = buckets["repos"]
    folder_repo = {
        folder: repo_names[index] for folder, index in buckets["folders"].items()
    }

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    categories: dict[str, list[str]] = {}
    for row in conn.execute("SELECT media_id, category FROM post_categories"):
        categories.setdefault(row["media_id"], []).append(row["category"])
    tags: dict[str, list[str]] = {}
    for row in conn.execute("SELECT media_id, tag FROM post_tags"):
        tags.setdefault(row["media_id"], []).append(row["tag"])
    media: dict[str, list[dict]] = {}
    for row in conn.execute("SELECT media_id, path, kind FROM post_media ORDER BY path"):
        media.setdefault(row["media_id"], []).append(
            {"path": row["path"], "kind": row["kind"]}
        )
    rows = conn.execute("SELECT * FROM posts ORDER BY sort_order, id").fetchall()

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "thumbs").mkdir(parents=True)
    (OUT / "comments").mkdir(parents=True)

    posts = []
    cat_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    video_count = 0
    ad_count = 0
    for row in rows:
        repo = folder_repo.get(row["folder"] or "", repo_names[0])
        cats = sorted(categories.get(row["media_id"], []))
        own_tags = sorted(tags.get(row["media_id"], []))
        for cat in cats:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        for tag in own_tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        if row["is_video"]:
            video_count += 1
        if "is_ad" in row.keys() and row["is_ad"]:
            ad_count += 1
        files = []
        media_items = []
        for index, item in enumerate(media.get(row["media_id"], [])):
            url = raw_url(repo, f"posts/{item['path']}")
            files.append(url)
            media_items.append(
                {
                    "i": index,
                    "kind": item["kind"],
                    "url": url,
                    "thumb": f"thumbs/{row['id']}_0_360.jpg",
                }
            )
        cover = (
            raw_url(repo, f"posts/{row['cover']}") if row["cover"] else None
        )
        thumb_path = OUT / "thumbs" / f"{row['id']}_0_360.jpg"
        if not make_thumb(row["id"], row["cover"], thumb_path):
            thumb_path = None
        comments_file = POSTS / (row["folder"] or "") / "comments.json"
        has_comments = comments_file.is_file()
        if has_comments:
            shutil.copyfile(comments_file, OUT / "comments" / f"{row['id']}.json")
        posts.append(
            {
                "id": row["id"],
                "shortcode": row["shortcode"],
                "owner": row["owner_username"],
                "owner_url": (
                    f"https://www.instagram.com/{row['owner_username']}/"
                    if row["owner_username"] else None
                ),
                "permalink": f"https://www.instagram.com/p/{row['shortcode']}/",
                "caption": (row["caption"] or "")[:280],
                "caption_full": row["caption"] or "",
                "likes": row["likes"] or 0,
                "taken_at": row["taken_at"],
                "is_video": bool(row["is_video"]),
                "typename": row["typename"],
                "location": row["location"],
                "category": cats[0] if cats else None,
                "categories": cats,
                "confidence": row["category_confidence"],
                "source": row["category_source"],
                "notes": row["notes"] or "",
                "tags": own_tags,
                "media_count": len(media_items),
                "media": media_items,
                "thumb": (
                    f"thumbs/{row['id']}_0_360.jpg"
                    if thumb_path else (media_items[0]["url"] if media_items else "")
                ),
                "cover": cover,
                "comments_url": f"comments/{row['id']}.json" if has_comments else None,
                "vision_caption": row["vision_caption"],
                "transcript": row["transcript"],
                "ocr_text": row["ocr_text"],
                "folder": row["folder"],
            }
        )
    conn.close()

    manifest = {
        "posts": posts,
        "facets": {
            "total": len(posts),
            "videos": video_count,
            "ads": ad_count,
            "categories": [
                {"name": k, "count": v}
                for k, v in sorted(cat_counts.items(), key=lambda kv: -kv[1])
            ],
            "tags": [
                {"name": k, "count": v}
                for k, v in sorted(tag_counts.items(), key=lambda kv: -kv[1])[:200]
            ],
            "presets": [
                "art", "sexy", "erotic", "ai-robotics", "ai-skills", "artist",
                "meme", "music", "ad", "food", "travel", "fitness", "tech",
                "fashion", "design", "news", "other",
            ],
        },
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    for name in ("style.css", "app.js"):
        shutil.copyfile(ROOT / "web" / name, OUT / name)
    shutil.copyfile(ROOT / "publish" / "static-api.js", OUT / "static-api.js")

    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    index = re.sub(r'\s*<button data-view="live"[^>]*>.*?</button>', "", index, flags=re.S)
    index = re.sub(r'\s*<div id="liveView".*?</div>\s*<div id="sentinel"',
                   "\n    <div id=\"sentinel\"", index, flags=re.S)
    index = re.sub(r'href="/static/style\.css[^"]*"', 'href="style.css"', index)
    index = index.replace(
        '<script src="/static/app.js?v=17"></script>',
        '<script src="static-api.js?v=1"></script>\n<script src="app.js?v=1"></script>',
    )
    (OUT / "index.html").write_text(index, encoding="utf-8")
    print(f"site: {OUT} ({len(posts)} posts)")


if __name__ == "__main__":
    main()
