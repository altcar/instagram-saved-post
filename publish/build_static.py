#!/usr/bin/env python3
"""Build a read-only static site for GitHub Pages from the archive DB.

Media is expected to live in six public repos (see partition_archive.py). The
site itself only contains the manifest, thumbnails and comments, so it stays
well below the 1 GB Pages soft limit.

    python publish/build_static.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("IG_DB", str(ROOT / "data/archive.db")))
POSTS = Path(os.environ.get("IG_POSTS", str(ROOT / "data/posts")))
CACHE_THUMBS = Path(os.environ.get("IG_THUMBS", str(ROOT / "data/.cache/thumbs")))
OUT = ROOT / "publish/site"
BUCKETS = ROOT / "publish/buckets.json"
GITHUB_USER = "altcar"
MEDIA_BRANCH = os.environ.get("IG_MEDIA_BRANCH", "gh-pages")

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
    """Media URL for a static build.

    Default: raw GitHub content on the branch that actually exists (gh-pages).
    Set IG_MEDIA_BASE to serve the same files from Hugging Face instead, e.g.
    https://huggingface.co/datasets/<user>/<repo>/resolve/main
    """
    base = os.environ.get("IG_MEDIA_BASE", "").rstrip("/")
    if base:
        return f"{base}/{quote(rel)}"
    safe = "/".join(quote(part) for part in rel.split("/"))
    return (
        f"https://raw.githubusercontent.com/{GITHUB_USER}/{repo}/"
        f"{MEDIA_BRANCH}/{safe}"
    )


def link_or_copy(source: Path, target: Path) -> None:
    """Hardlink when possible (same filesystem) so rebuilds are fast."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists():
            return
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def make_thumb(post_id: int, cover_rel: str | None, target: Path) -> bool:
    if target.is_file():
        return True
    source = CACHE_THUMBS / f"{post_id}_0_360.jpg"
    if source.is_file():
        link_or_copy(source, target)
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


MANIFEST_ONLY = "--manifest-only" in sys.argv


def main() -> None:
    global OUT
    parser = argparse.ArgumentParser(description="Build static Instagram archive")
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="only newest N posts")
    parser.add_argument("--out", default=str(OUT), help="output directory")
    args, _ = parser.parse_known_args()
    OUT = Path(args.out)
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
    rows = conn.execute(
        "SELECT * FROM posts ORDER BY sort_order, id"
        + (" LIMIT ?" if args.limit else ""),
        ([args.limit] if args.limit else []),
    ).fetchall()
    print(f"static: {len(rows)} posts in DB", flush=True)

    saved_git = None
    if not MANIFEST_ONLY and (OUT / ".git").is_dir():
        saved_git = OUT.parent / ".site-git"
        if saved_git.exists():
            shutil.rmtree(saved_git)
        (OUT / ".git").rename(saved_git)
    print("static: clearing site" if not MANIFEST_ONLY else "static: updating in place", flush=True)
    if not MANIFEST_ONLY and OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "thumbs").mkdir(parents=True, exist_ok=True)
    (OUT / "comments").mkdir(parents=True, exist_ok=True)
    existing_thumbs = ({p.name for p in (OUT / "thumbs").iterdir()}
                       if MANIFEST_ONLY else set())
    existing_comments = ({p.name for p in (OUT / "comments").iterdir()}
                         if MANIFEST_ONLY else set())
    if saved_git is not None:
        saved_git.rename(OUT / ".git")

    posts = []
    cat_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    video_count = 0
    ad_count = 0
    filter_counts = {
        "no_video": 0, "no_image": 0, "no_ads": 0, "bad_owner": 0,
        "bad_cover": 0, "caption_empty": 0, "caption_error": 0,
        "bad_shortcode": 0, "comments_empty": 0, "comments_error": 0,
        "still_saved": 0, "already_unsaved": 0, "uncategorised": 0,
    }
    for index, row in enumerate(rows):
        if index and index % 1000 == 0:
            print(f"static: {index}/{len(rows)}", flush=True)
        repo = folder_repo.get(row["folder"] or "", repo_names[0])
        cats = sorted(categories.get(row["media_id"], []))
        own_tags = sorted(tags.get(row["media_id"], []))
        for cat in cats:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        for tag in own_tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        if row["is_video"]:
            video_count += 1
        else:
            filter_counts["no_video"] += 1
        if "is_ad" in row.keys() and row["is_ad"]:
            ad_count += 1
        else:
            filter_counts["no_ads"] += 1
        if not row["owner_username"]:
            filter_counts["bad_owner"] += 1
        if not row["cover"]:
            filter_counts["bad_cover"] += 1
        if not (row["caption"] or "").strip():
            filter_counts["caption_empty"] += 1
        if not (row["shortcode"] or "").strip():
            filter_counts["bad_shortcode"] += 1
        if row["comments_fetched_at"] is None:
            filter_counts["comments_error"] += 1
        elif not (row["comments_count"] or 0):
            filter_counts["comments_empty"] += 1
        if row["unsaved_at"]:
            filter_counts["already_unsaved"] += 1
        else:
            filter_counts["still_saved"] += 1
        if not cats:
            filter_counts["uncategorised"] += 1
        if not any(item["kind"] == "image" for item in media.get(row["media_id"], [])):
            filter_counts["no_image"] += 1
        files = []
        media_items = []
        for index, item in enumerate(media.get(row["media_id"], [])):
            url = raw_url(repo, f"posts/{item['path']}")
            files.append(url)
            thumb_name = f"{row['id']}_{index}_360.jpg"
            thumb_rel = f"thumbs/{thumb_name}"
            if MANIFEST_ONLY:
                item_thumb = (thumb_rel if thumb_name in existing_thumbs
                              else (url if item["kind"] == "image"
                                    else (f"thumbs/{row['id']}_0_360.jpg"
                                          if f"{row['id']}_0_360.jpg" in existing_thumbs
                                          else url)))
            else:
                cached = CACHE_THUMBS / thumb_name
                if cached.is_file():
                    link_or_copy(cached, OUT / thumb_rel)
                    item_thumb = thumb_rel
                elif item["kind"] == "image":
                    # Use the actual slide, never the shared post cover, so every
                    # carousel thumbnail is distinct even without a cached thumb.
                    item_thumb = url
                else:
                    cover_cached = CACHE_THUMBS / f"{row['id']}_0_360.jpg"
                    if cover_cached.is_file():
                        link_or_copy(cover_cached, OUT / f"thumbs/{row['id']}_0_360.jpg")
                        item_thumb = f"thumbs/{row['id']}_0_360.jpg"
                    else:
                        item_thumb = url
            media_items.append(
                {
                    "i": index,
                    "kind": item["kind"],
                    "url": url,
                    "thumb": item_thumb,
                }
            )
        cover = (
            raw_url(repo, f"posts/{row['cover']}") if row["cover"] else None
        )
        thumb_path = OUT / "thumbs" / f"{row['id']}_0_360.jpg"
        if MANIFEST_ONLY:
            if f"{row['id']}_0_360.jpg" not in existing_thumbs:
                thumb_path = None
        elif not make_thumb(row["id"], row["cover"], thumb_path):
            thumb_path = None
        comments_file = POSTS / (row["folder"] or "") / "comments.json"
        if MANIFEST_ONLY:
            has_comments = f"{row['id']}.json" in existing_comments
        else:
            has_comments = comments_file.is_file()
            if has_comments:
                link_or_copy(comments_file, OUT / "comments" / f"{row['id']}.json")
        posts.append(
            {
                "id": row["id"],
                "shortcode": row["shortcode"],
                "owner": row["owner_username"],
                "owner_url": (
                    f"https://www.instagram.com/{row['owner_username']}/"
                    if row["owner_username"] else None
                ),                "permalink": f"https://www.instagram.com/p/{row['shortcode']}/",
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
    cat_order: list[str] = []
    try:
        cat_order = [r["name"] for r in conn.execute(
            "SELECT name FROM categories ORDER BY position ASC, name ASC")]
    except sqlite3.OperationalError:
        cat_order = []
    conn.close()

    ordered_cats = [{"name": name, "count": cat_counts.get(name, 0)}
                    for name in cat_order]
    for name, count in sorted(cat_counts.items(), key=lambda kv: -kv[1]):
        if name not in cat_order:
            ordered_cats.append({"name": name, "count": count})

    manifest = {
        "posts": posts,
        "facets": {
            "total": len(posts),
            "videos": video_count,
            "ads": ad_count,
            "filter_counts": filter_counts,
            "categories": ordered_cats,
            "category_list": ordered_cats,
            "tags": [
                {"name": k, "count": v}
                for k, v in sorted(tag_counts.items(), key=lambda kv: -kv[1])[:200]
            ],
            "presets": [c["name"] for c in ordered_cats] or [
                "art", "sexy", "erotic", "ai-robotics", "ai-skills", "artist",
                "meme", "music", "ad", "food", "travel", "fitness", "tech",
                "fashion", "design", "news", "other",
            ],
            "saved": {
                "still_saved": filter_counts["still_saved"],
                "already_unsaved": filter_counts["already_unsaved"],
            },
        },
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    for name in ("style.css", "app.js"):
        shutil.copyfile(ROOT / "web" / name, OUT / name)
    shutil.copyfile(ROOT / "publish" / "static-api.js", OUT / "static-api.js")
    print("static: writing index", flush=True)

    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    index = re.sub(r'\s*<button data-view="live"[^>]*>.*?</button>', "", index, flags=re.S)
    index = re.sub(r'\s*<!-- LIVE_START -->.*?<!-- LIVE_END -->', "", index, flags=re.S)
    index = re.sub(r'\s*<div id="liveView".*?</div>\s*<div id="sentinel"',
                   "\n    <div id=\"sentinel\"", index, flags=re.S)
    index = re.sub(r'href="/static/style\.css[^"]*"', 'href="style.css"', index)
    index = re.sub(
        r'<script src="/static/app\.js[^"]*"></script>',
        '<script src="static-api.js?v=5"></script>\n<script src="app.js?v=46"></script>',
        index,
    )
    (OUT / "index.html").write_text(index, encoding="utf-8")
    print(f"site: {OUT} ({len(posts)} posts)")


if __name__ == "__main__":
    main()
