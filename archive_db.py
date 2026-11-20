#!/usr/bin/env python3
"""SQLite archive index and per-post file layout for saved Instagram posts.

The database is the fast lookup layer used by the web app and the downloader,
so browsing never has to scan thousands of folders on disk. Each post folder
remains self-contained for portability:

    <media>/NNNN_<shortcode>/
        <original media files>
        cover.jpg            (video/cover image)
        caption.txt
        comments.json
        comments.txt
        meta.json

Usage:
    python archive_db.py import --db data/1911/archive.db \
        --json data/1911/saved_posts.json --media data/1911/saved_media
    python archive_db.py write-files --db data/1911/archive.db \
        --media data/1911/saved_media
    python archive_db.py stats --db data/1911/archive.db
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover - optional
    imageio_ffmpeg = None

MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp",
    ".mp4", ".mov", ".m4v", ".webm", ".mkv",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".bmp"}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id TEXT UNIQUE,
    shortcode TEXT UNIQUE,
    taken_at TEXT,
    owner_username TEXT,
    owner_id TEXT,
    caption TEXT DEFAULT '',
    likes INTEGER DEFAULT 0,
    is_video INTEGER DEFAULT 0,
    typename TEXT,
    location TEXT,
    hashtags TEXT DEFAULT '[]',
    folder TEXT,
    cover TEXT,
    category TEXT,
    category_confidence REAL,
    category_source TEXT,
    notes TEXT DEFAULT '',
    sort_order INTEGER,
    downloaded_at TEXT,
    unsaved_at TEXT,
    source TEXT DEFAULT 'api',
    vision_caption TEXT,
    transcript TEXT,
    ocr_text TEXT
);

CREATE TABLE IF NOT EXISTS post_media (
    media_id TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT,
    PRIMARY KEY (media_id, path)
);

CREATE TABLE IF NOT EXISTS post_tags (
    media_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (media_id, tag)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_taken ON posts(taken_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_owner ON posts(owner_username);
CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category);
CREATE INDEX IF NOT EXISTS idx_posts_order ON posts(sort_order);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON post_tags(tag);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def list_media(folder: Path) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    if not folder.is_dir():
        return items
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTS:
            continue
        if path.name.lower() == "cover.jpg":
            continue
        items.append(
            {
                "type": "video" if path.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".mkv"} else "image",
                "path": f"{folder.name}/{path.name}",
            }
        )
    return items


def find_cover(folder: Path) -> Optional[str]:
    cover = folder / "cover.jpg"
    if cover.is_file():
        return f"{folder.name}/{cover.name}"
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
            return f"{folder.name}/{path.name}"
    return None


def upsert_post(
    conn: sqlite3.Connection,
    *,
    media_id: Optional[str],
    shortcode: str,
    media_root: Optional[Path] = None,
    taken_at: Optional[str] = None,
    owner_username: Optional[str] = None,
    owner_id: Optional[str] = None,
    caption: str = "",
    likes: int = 0,
    is_video: bool = False,
    typename: Optional[str] = None,
    location: Optional[str] = None,
    hashtags: Optional[List[str]] = None,
    folder: Optional[str] = None,
    cover: Optional[str] = None,
    category: Optional[str] = None,
    sort_order: Optional[int] = None,
    source: str = "api",
    downloaded_at: Optional[str] = None,
) -> int:
    media_key = media_id or f"folder:{shortcode}"
    row = conn.execute(
        "SELECT id FROM posts WHERE media_id = ? OR shortcode = ?",
        (media_key, shortcode),
    ).fetchone()
    payload = {
        "media_id": media_key,
        "shortcode": shortcode,
        "taken_at": taken_at,
        "owner_username": owner_username,
        "owner_id": str(owner_id) if owner_id is not None else None,
        "caption": caption or "",
        "likes": likes or 0,
        "is_video": 1 if is_video else 0,
        "typename": typename,
        "location": location,
        "hashtags": json.dumps(hashtags or [], ensure_ascii=False),
        "folder": folder,
        "cover": cover,
        "category": category,
        "sort_order": sort_order,
        "source": source,
        "downloaded_at": downloaded_at or now_iso(),
    }
    if row:
        post_id = row["id"]
        update_payload = dict(payload)
        # Re-importing a run must not wipe vision/CLIP categories or manual order.
        if category is None:
            update_payload.pop("category", None)
        update_payload.pop("sort_order", None)
        assignments = ", ".join(f"{key} = ?" for key in update_payload)
        conn.execute(
            f"UPDATE posts SET {assignments} WHERE id = ?",
            (*update_payload.values(), post_id),
        )
    else:
        columns = ", ".join(payload)
        marks = ", ".join("?" for _ in payload)
        cur = conn.execute(
            f"INSERT INTO posts ({columns}) VALUES ({marks})",
            tuple(payload.values()),
        )
        post_id = cur.lastrowid
    conn.execute("DELETE FROM post_media WHERE media_id = ?", (media_key,))
    if folder and media_root is not None:
        for item in list_media(media_root / folder):
            conn.execute(
                "INSERT OR REPLACE INTO post_media (media_id, path, kind) VALUES (?,?,?)",
                (media_key, item["path"], item["type"]),
            )
    return post_id


def import_json(conn: sqlite3.Connection, json_path: Path, media_root: Path) -> int:
    records = json.loads(json_path.read_text(encoding="utf-8"))
    count = 0
    for index, rec in enumerate(records):
        shortcode = rec.get("shortcode")
        if not shortcode:
            continue
        folder_name = None
        if rec.get("media"):
            folder_name = Path(rec["media"][0]["path"]).parts[0]
        else:
            matches = list(media_root.glob(f"*_{shortcode}"))
            if matches:
                folder_name = matches[0].name
        folder_path = media_root / folder_name if folder_name else None
        media = list_media(folder_path) if folder_path else []
        cover = find_cover(folder_path) if folder_path else None
        upsert_post(
            conn,
            media_id=str(rec["media_id"]) if rec.get("media_id") else None,
            shortcode=shortcode,
            media_root=media_root,
            taken_at=rec.get("taken_at"),
            owner_username=rec.get("owner_username"),
            owner_id=rec.get("owner_id"),
            caption=rec.get("caption") or "",
            likes=rec.get("likes") or 0,
            is_video=bool(rec.get("is_video")),
            typename=rec.get("typename"),
            location=rec.get("location"),
            hashtags=rec.get("hashtags") or [],
            folder=folder_name,
            cover=cover,
            sort_order=index,
            source="api",
        )
        count += 1
        if count % 200 == 0:
            conn.commit()
            print(f"  {count} records", flush=True)
    conn.commit()
    return count


def sync_folders(conn: sqlite3.Connection, media_root: Path) -> int:
    added = 0
    max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM posts").fetchone()[0]
    known = {
        row["folder"]
        for row in conn.execute("SELECT folder FROM posts WHERE folder IS NOT NULL")
    }
    for folder in sorted(media_root.iterdir()):
        if not folder.is_dir() or folder.name in known:
            continue
        if "_" not in folder.name:
            continue
        shortcode = folder.name.split("_", 1)[1]
        caption = ""
        for txt in sorted(folder.glob("*.txt")):
            caption = txt.read_text(encoding="utf-8", errors="replace").strip()
        max_order += 1
        upsert_post(
            conn,
            media_id=None,
            shortcode=shortcode,
            media_root=media_root,
            caption=caption,
            folder=folder.name,
            cover=find_cover(folder),
            is_video=any(p.suffix.lower() in {".mp4", ".mov", ".m4v"} for p in folder.iterdir()),
            sort_order=max_order,
            source="folder",
        )
        added += 1
        if added % 200 == 0:
            conn.commit()
    conn.commit()
    return added


def comments_text(comments: List[Dict[str, Any]]) -> str:
    lines = []
    for comment in comments:
        owner = comment.get("owner") or "?"
        created = comment.get("created_at") or ""
        likes = comment.get("likes")
        text = (comment.get("text") or "").replace("\n", " ")
        suffix = f"  [{likes} likes]" if likes else ""
        lines.append(f"{owner} {created}: {text}{suffix}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_post_files(
    conn: sqlite3.Connection,
    media_root: Path,
    limit: Optional[int] = None,
    make_covers: bool = False,
) -> int:
    query = "SELECT * FROM posts WHERE folder IS NOT NULL ORDER BY sort_order"
    rows = conn.execute(query).fetchall()
    if limit:
        rows = rows[:limit]
    written = 0
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe() if imageio_ffmpeg else None
    for row in rows:
        folder = media_root / row["folder"]
        if not folder.is_dir():
            continue
        caption = row["caption"] or ""
        (folder / "caption.txt").write_text(caption + ("\n" if caption else ""), encoding="utf-8")

        comments = json.loads(
            (folder / "comments.json").read_text(encoding="utf-8")
            if (folder / "comments.json").exists()
            else "[]"
        )
        (folder / "comments.json").write_text(
            json.dumps(comments, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (folder / "comments.txt").write_text(comments_text(comments), encoding="utf-8")

        media = list_media(folder)
        meta = {
            "media_id": row["media_id"],
            "shortcode": row["shortcode"],
            "permalink": f"https://www.instagram.com/p/{row['shortcode']}/",
            "taken_at": row["taken_at"],
            "owner_username": row["owner_username"],
            "owner_id": row["owner_id"],
            "caption": caption,
            "likes": row["likes"],
            "is_video": bool(row["is_video"]),
            "typename": row["typename"],
            "location": row["location"],
            "hashtags": json.loads(row["hashtags"] or "[]"),
            "media": media,
            "cover": Path(row["cover"]).name if row["cover"] else None,
            "category": row["category"],
        }
        (folder / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        if make_covers and not (folder / "cover.jpg").is_file():
            cover_src = None
            for item in media:
                if item["type"] == "image":
                    cover_src = folder / Path(item["path"]).name
                    break
            if cover_src and cover_src.is_file():
                try:
                    shutil.copyfile(cover_src, folder / "cover.jpg")
                except OSError:
                    pass
            elif ffmpeg:
                video = next(
                    (folder / Path(i["path"]).name for i in media if i["type"] == "video"),
                    None,
                )
                if video and video.is_file():
                    subprocess.run(
                        [
                            ffmpeg, "-y", "-loglevel", "error", "-ss", "0.5",
                            "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=720:-1", str(folder / "cover.jpg"),
                        ],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=60,
                    )
            cover = find_cover(folder)
            if cover:
                conn.execute(
                    "UPDATE posts SET cover = ? WHERE id = ?", (cover, row["id"])
                )
        written += 1
        if written % 500 == 0:
            conn.commit()
            print(f"  {written} folders written", flush=True)
    conn.commit()
    return written


def export_json(conn: sqlite3.Connection, media_root: Path, json_path: Path) -> int:
    rows = conn.execute("SELECT * FROM posts ORDER BY sort_order").fetchall()
    records = []
    for row in rows:
        folder = media_root / row["folder"] if row["folder"] else None
        media = list_media(folder) if folder else []
        comments = []
        if folder:
            comments_file = folder / "comments.json"
            if comments_file.is_file():
                try:
                    comments = json.loads(comments_file.read_text(encoding="utf-8"))
                except Exception:
                    comments = []
        tags = [
            r["tag"]
            for r in conn.execute(
                "SELECT tag FROM post_tags WHERE media_id=? ORDER BY tag",
                (row["media_id"],),
            )
        ]
        records.append(
            {
                "media_id": row["media_id"],
                "shortcode": row["shortcode"],
                "permalink": f"https://www.instagram.com/p/{row['shortcode']}/",
                "taken_at": row["taken_at"],
                "caption": row["caption"] or "",
                "owner_username": row["owner_username"],
                "owner_id": row["owner_id"],
                "likes": row["likes"],
                "is_video": bool(row["is_video"]),
                "typename": row["typename"],
                "media": media,
                "comments": comments,
                "location": row["location"],
                "hashtags": json.loads(row["hashtags"] or "[]"),
                "cover": row["cover"],
                "category": row["category"],
                "category_confidence": row["category_confidence"],
                "category_source": row["category_source"],
                "notes": row["notes"] or "",
                "tags": tags,
                "vision_caption": row["vision_caption"],
                "ocr_text": row["ocr_text"],
                "transcript": row["transcript"],
                "source": row["source"],
            }
        )
    json_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return len(records)


def stats(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    with_folder = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE folder IS NOT NULL"
    ).fetchone()[0]
    videos = conn.execute("SELECT COUNT(*) FROM posts WHERE is_video=1").fetchone()[0]
    covers = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE cover IS NOT NULL"
    ).fetchone()[0]
    categories = conn.execute(
        "SELECT COALESCE(category,'(none)') c, COUNT(*) n FROM posts GROUP BY c ORDER BY n DESC"
    ).fetchall()
    print(f"posts: {total}  with folder: {with_folder}  videos: {videos}  covers: {covers}")
    print("categories:", ", ".join(f"{r['c']}={r['n']}" for r in categories))


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive SQLite index")
    parser.add_argument("--db", default="data/1911/archive.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("import")
    p_import.add_argument("--db", default=argparse.SUPPRESS)
    p_import.add_argument("--json", default="data/1911/saved_posts.json")
    p_import.add_argument("--media", default="data/1911/saved_media")

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--db", default=argparse.SUPPRESS)
    p_sync.add_argument("--media", default="data/1911/saved_media")

    p_files = sub.add_parser("write-files")
    p_files.add_argument("--db", default=argparse.SUPPRESS)
    p_files.add_argument("--media", default="data/1911/saved_media")
    p_files.add_argument("--limit", type=int)
    p_files.add_argument("--covers", action="store_true")

    p_stats = sub.add_parser("stats")
    p_stats.add_argument("--db", default=argparse.SUPPRESS)

    p_export = sub.add_parser("export-json")
    p_export.add_argument("--db", default=argparse.SUPPRESS)
    p_export.add_argument("--json", default="data/1911/saved_posts.json")
    p_export.add_argument("--media", default="data/1911/saved_media")

    args = parser.parse_args()
    conn = connect(Path(args.db))

    if args.cmd == "import":
        n = import_json(conn, Path(args.json), Path(args.media))
        print(f"imported {n} records")
        n = sync_folders(conn, Path(args.media))
        print(f"added {n} folder-only records")
    elif args.cmd == "sync":
        n = sync_folders(conn, Path(args.media))
        print(f"added {n} folder-only records")
    elif args.cmd == "write-files":
        n = write_post_files(
            conn, Path(args.media), limit=args.limit, make_covers=args.covers
        )
        print(f"wrote files for {n} folders")
    elif args.cmd == "stats":
        stats(conn)
    elif args.cmd == "export-json":
        n = export_json(conn, Path(args.media), Path(args.json))
        print(f"exported {n} records to {args.json}")
    conn.close()


if __name__ == "__main__":
    main()
