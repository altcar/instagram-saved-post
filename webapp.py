#!/usr/bin/env python3
"""Local web app to browse the saved Instagram archive.

Designed to stay light on CPU, RAM and bandwidth:
  * the SQLite index answers listings (no folder scans);
  * only 36-60 cards are fetched per page;
  * thumbnails are generated once, on demand, at 360 px and cached;
  * full media (and video bytes) are served only when a post is opened;
  * video uses HTTP Range requests.

Run:
    python webapp.py --db data/archive.db --media data/posts \
        --cache data/.cache --port 8770
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageOps, ImageStat

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover
    imageio_ffmpeg = None

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

CATEGORIES = [
    "art", "sexy", "erotic", "ai-robotics", "ai-skills", "artist",
    "meme", "music", "ad", "food", "travel", "fitness", "tech",
    "fashion", "design", "news", "other",
]

_thumb_locks: Dict[str, threading.Lock] = {}
_thumb_locks_guard = threading.Lock()


def lock_for(key: str) -> threading.Lock:
    with _thumb_locks_guard:
        return _thumb_locks.setdefault(key, threading.Lock())


class Archive:
    def __init__(self, db_path: Path, media_root: Path, cache_dir: Path,
                 page_size: int = 60):
        self.db_path = db_path
        self.media_root = media_root
        self.cache_dir = cache_dir
        self.thumb_dir = cache_dir / "thumbs"
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.page_size = page_size

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS post_categories ("
            "media_id TEXT NOT NULL, category TEXT NOT NULL, "
            "confidence REAL DEFAULT 1.0, source TEXT DEFAULT 'manual', "
            "PRIMARY KEY (media_id, category))"
        )
        migrated = conn.execute(
            "SELECT value FROM meta WHERE key='categories_migrated'"
        ).fetchone()
        if not migrated:
            conn.execute(
                "INSERT OR IGNORE INTO post_categories "
                "(media_id, category, confidence, source) "
                "SELECT media_id, category, COALESCE(category_confidence, 1.0), "
                "COALESCE(category_source, 'legacy') FROM posts "
                "WHERE category IS NOT NULL AND trim(category) <> ''"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('categories_migrated','1')"
            )
            conn.commit()
        return conn

    # ---------------- listing ----------------
    def list_posts(self, *, offset: int, limit: int, q: str, category: str,
                   tag: str, sort: str, owner: str = "") -> Tuple[List[Dict[str, Any]], int]:
        where: List[str] = []
        params: List[Any] = []
        if q:
            like = f"%{q}%"
            where.append(
                "(p.caption LIKE ? OR p.owner_username LIKE ? OR p.shortcode LIKE ?"
                " OR p.notes LIKE ? OR p.vision_caption LIKE ? OR p.transcript LIKE ?"
                " OR EXISTS (SELECT 1 FROM post_tags t WHERE t.media_id=p.media_id AND t.tag LIKE ?))"
            )
            params.extend([like] * 7)
        if category and category not in ("all", ""):
            where.append(
                "EXISTS (SELECT 1 FROM post_categories pc "
                "WHERE pc.media_id=p.media_id AND pc.category = ?)"
            )
            params.append(category)
        if tag:
            where.append(
                "EXISTS (SELECT 1 FROM post_tags t WHERE t.media_id=p.media_id AND t.tag = ?)"
            )
            params.append(tag)
        if owner:
            where.append("p.owner_username = ?")
            params.append(owner)
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        order = {
            "manual": "p.sort_order ASC, p.id ASC",
            "newest": "(p.taken_at IS NULL) ASC, p.taken_at DESC, p.id DESC",
            "oldest": "(p.taken_at IS NULL) ASC, p.taken_at ASC, p.id ASC",
            "likes": "p.likes DESC, p.id DESC",
            "uncategorised": (
                "NOT EXISTS (SELECT 1 FROM post_categories pc "
                "WHERE pc.media_id=p.media_id) DESC, p.sort_order ASC"
            ),
            "random": "RANDOM()",
        }.get(sort, "p.sort_order ASC, p.id ASC")

        conn = self.connect()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM posts p {clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT p.*, (
                        SELECT GROUP_CONCAT(tag, '|') FROM post_tags
                        WHERE media_id = p.media_id
                    ) AS tag_list,
                    (SELECT GROUP_CONCAT(category, '|') FROM post_categories
                        WHERE media_id = p.media_id) AS category_list,
                    (SELECT COUNT(*) FROM post_media m WHERE m.media_id = p.media_id) AS media_count
                    FROM posts p {clause}
                    ORDER BY {order}
                    LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        except Exception:
            conn.close()
            raise

        items = []
        for row in rows:
            media_rows = conn.execute(
                "SELECT path, kind FROM post_media WHERE media_id=? ORDER BY path",
                (row["media_id"],),
            ).fetchall()
            media = [
                {
                    "i": index,
                    "kind": media_row["kind"],
                    "thumb": f"/api/thumb/{row['id']}?i={index}",
                    "url": f"/api/media/{row['id']}/{index}",
                }
                for index, media_row in enumerate(media_rows)
            ]
            items.append(
                {
                    "id": row["id"],
                    "shortcode": row["shortcode"],
                    "owner": row["owner_username"],
                    "owner_url": f"https://www.instagram.com/{row['owner_username']}/" if row["owner_username"] else None,
                    "permalink": f"https://www.instagram.com/p/{row['shortcode']}/",
                    "caption": (row["caption"] or "")[:280],
                    "likes": row["likes"],
                    "taken_at": row["taken_at"],
                    "is_video": bool(row["is_video"]),
                    "category": (
                        (row["category_list"] or "").split("|")[0]
                        if row["category_list"] else row["category"]
                    ),
                    "categories": [
                        c for c in (row["category_list"] or "").split("|") if c
                    ],
                    "is_ad": bool(row["is_ad"]) if "is_ad" in row.keys() else False,
                    "confidence": row["category_confidence"],
                    "source": row["category_source"],
                    "tags": (row["tag_list"] or "").split("|") if row["tag_list"] else [],
                    "notes": row["notes"] or "",
                    "media_count": row["media_count"] or 0,
                    "media": media,
                    "thumb": f"/api/thumb/{row['id']}",
                }
            )
        conn.close()
        return items, total

    def facets(self) -> Dict[str, Any]:
        conn = self.connect()
        try:
            cats = conn.execute(
                "SELECT category c, COUNT(*) n FROM post_categories "
                "GROUP BY category ORDER BY n DESC"
            ).fetchall()
            tags = conn.execute(
                "SELECT tag, COUNT(*) n FROM post_tags GROUP BY tag ORDER BY n DESC LIMIT 200"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            videos = conn.execute("SELECT COUNT(*) FROM posts WHERE is_video=1").fetchone()[0]
            ads = conn.execute("SELECT COUNT(*) FROM posts WHERE is_ad=1").fetchone()[0]
        finally:
            conn.close()
        return {
            "total": total,
            "videos": videos,
            "ads": ads,
            "categories": [{"name": c["c"], "count": c["n"]} for c in cats],
            "tags": [{"name": t["tag"], "count": t["n"]} for t in tags],
            "presets": CATEGORIES,
        }

    def get_post(self, post_id: int, with_comments: bool = False) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
            if not row:
                return None
            media = conn.execute(
                "SELECT path, kind FROM post_media WHERE media_id = ? ORDER BY path",
                (row["media_id"],),
            ).fetchall()
            tags = [
                r["tag"]
                for r in conn.execute(
                    "SELECT tag FROM post_tags WHERE media_id = ? ORDER BY tag",
                    (row["media_id"],),
                )
            ]
            categories = [
                r["category"]
                for r in conn.execute(
                    "SELECT category FROM post_categories "
                    "WHERE media_id = ? ORDER BY category",
                    (row["media_id"],),
                )
            ]
        finally:
            conn.close()

        folder = self.media_root / row["folder"] if row["folder"] else None
        data: Dict[str, Any] = {
            "id": row["id"],
            "media_id": row["media_id"],
            "shortcode": row["shortcode"],
            "permalink": f"https://www.instagram.com/p/{row['shortcode']}/",
            "owner": row["owner_username"],
            "owner_id": row["owner_id"],
            "caption": row["caption"] or "",
            "likes": row["likes"],
            "taken_at": row["taken_at"],
            "is_video": bool(row["is_video"]),
            "typename": row["typename"],
            "location": row["location"],
            "hashtags": json.loads(row["hashtags"] or "[]"),
            "category": row["category"],
            "categories": categories,
            "category_confidence": row["category_confidence"],
            "category_source": row["category_source"],
            "source": row["source"],
            "notes": row["notes"] or "",
            "tags": tags,
            "vision_caption": row["vision_caption"],
            "transcript": row["transcript"],
            "ocr_text": row["ocr_text"],
            "folder": row["folder"],
            "media": [
                {
                    "kind": m["kind"],
                    "name": Path(m["path"]).name,
                    "url": f"/api/media/{row['id']}/{i}",
                }
                for i, m in enumerate(media)
            ],
            "thumb": f"/api/thumb/{row['id']}",
            "cover": f"/api/cover/{row['id']}",
        }
        if with_comments and folder:
            comments_file = folder / "comments.json"
            if comments_file.is_file():
                try:
                    data["comments"] = json.loads(
                        comments_file.read_text(encoding="utf-8")
                    )
                except Exception:
                    data["comments"] = []
            else:
                data["comments"] = []
        return data

    def folder_path(self, post_id: int) -> Optional[Path]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT folder FROM posts WHERE id=?", (post_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row or not row["folder"]:
            return None
        path = self.media_root / row["folder"]
        return path if path.is_dir() else None

    def update_post(self, post_id: int, payload: Dict[str, Any]) -> bool:
        allowed = {"notes": "notes"}
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT media_id FROM posts WHERE id = ?", (post_id,)
            ).fetchone()
            if not row:
                return False
            media_key = row["media_id"]
            for key, column in allowed.items():
                if key in payload:
                    conn.execute(
                        f"UPDATE posts SET {column} = ? WHERE id = ?",
                        (payload[key], post_id),
                    )
            if "categories" in payload:
                values = payload["categories"] or []
                if isinstance(values, str):
                    values = [v.strip() for v in values.split(",")]
                values = sorted({str(v).strip() for v in values if str(v).strip()})
                conn.execute("DELETE FROM post_categories WHERE media_id=?", (media_key,))
                conn.executemany(
                    "INSERT OR IGNORE INTO post_categories "
                    "(media_id, category, confidence, source) VALUES (?,?,?,?)",
                    [(media_key, v, 1.0, "manual") for v in values],
                )
                conn.execute(
                    "UPDATE posts SET category=? WHERE id=?",
                    (values[0] if values else None, post_id),
                )
            elif "category" in payload:
                value = (payload["category"] or "").strip()
                if value:
                    conn.execute(
                        "INSERT OR IGNORE INTO post_categories "
                        "(media_id, category, confidence, source) VALUES (?,?,?,?)",
                        (media_key, value, 1.0, "manual"),
                    )
                    conn.execute(
                        "UPDATE posts SET category=CASE "
                        "WHEN category IS NULL OR trim(category)='' THEN ? "
                        "ELSE category END WHERE id=?",
                        (value, post_id),
                    )
                else:
                    conn.execute("DELETE FROM post_categories WHERE media_id=?", (media_key,))
                    conn.execute("UPDATE posts SET category=NULL WHERE id=?", (post_id,))
            if "tags" in payload:
                tags = payload["tags"]
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",")]
                tags = sorted({str(t).strip().lstrip("#").lower() for t in tags if str(t).strip()})
                conn.execute("DELETE FROM post_tags WHERE media_id = ?", (media_key,))
                conn.executemany(
                    "INSERT OR IGNORE INTO post_tags (media_id, tag) VALUES (?,?)",
                    [(media_key, t) for t in tags],
                )
            conn.commit()
            return True
        finally:
            conn.close()

    def bulk_category(self, ids: List[int], category: str) -> int:
        if not ids:
            return 0
        conn = self.connect()
        try:
            for post_id in ids:
                row = conn.execute(
                    "SELECT media_id FROM posts WHERE id=?", (post_id,)
                ).fetchone()
                if not row:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO post_categories "
                    "(media_id, category, confidence, source) VALUES (?,?,?,?)",
                    (row["media_id"], category, 1.0, "manual"),
                )
                conn.execute(
                    "UPDATE posts SET category=CASE "
                    "WHEN category IS NULL OR trim(category)='' THEN ? "
                    "ELSE category END, category_source='manual', "
                    "category_confidence=1.0 WHERE id=?",
                    (category, post_id),
                )
            conn.commit()
            return len(ids)
        finally:
            conn.close()

    def reorder(self, post_id: int, before_id: Optional[int], after_id: Optional[int]) -> bool:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT id FROM posts ORDER BY sort_order ASC, id ASC"
            ).fetchall()
            ids = [r["id"] for r in rows]
            if post_id not in ids:
                return False
            ids.remove(post_id)
            if before_id and before_id in ids:
                ids.insert(ids.index(before_id), post_id)
            elif after_id and after_id in ids:
                ids.insert(ids.index(after_id) + 1, post_id)
            else:
                ids.append(post_id)
            conn.executemany(
                "UPDATE posts SET sort_order = ? WHERE id = ?",
                [(i * 100, pid) for i, pid in enumerate(ids)],
            )
            conn.commit()
            return True
        finally:
            conn.close()

    # ---------------- thumbnails ----------------
    def _media_paths(self, post_id: int) -> List[Tuple[str, str]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT m.path, m.kind FROM post_media m
                   JOIN posts p ON p.media_id = m.media_id WHERE p.id = ? ORDER BY m.path""",
                (post_id,),
            ).fetchall()
            return [(r["path"], r["kind"]) for r in rows]
        finally:
            conn.close()

    def _cover_path(self, post_id: int) -> Optional[Path]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT p.cover, p.folder FROM posts p WHERE p.id = ?", (post_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        if row["cover"]:
            candidate = self.media_root / row["cover"]
            if candidate.is_file():
                return candidate
        return None

    def thumbnail(self, post_id: int, width: int = 360, index: int = 0) -> Optional[Path]:
        cache = self.thumb_dir / f"{post_id}_{index}_{width}.jpg"
        media = self._media_paths(post_id)
        source: Optional[Path] = None
        kind = None
        rel = None
        if 0 <= index < len(media):
            rel, kind = media[index]
        if index == 0:
            source = self._cover_path(post_id)
        if source is None and rel is not None:
            candidate = self.media_root / rel
            if kind == "image":
                source = candidate
            elif kind == "video":
                source = self._video_frame(candidate)
        if source is None:
            source = self._cover_path(post_id)
        if source is not None and self._image_is_unhelpful(source):
            source = self._better_thumb_source(post_id, media, index)

        def cache_is_fresh() -> bool:
            if not cache.is_file() or cache.stat().st_size <= 0:
                return False
            if source is None or not source.is_file():
                return True
            return cache.stat().st_mtime >= source.stat().st_mtime

        if cache_is_fresh():
            return cache
        with lock_for(f"{post_id}_{index}_{width}"):
            if cache_is_fresh():
                return cache
            try:
                if source is None:
                    self._placeholder(cache, width)
                else:
                    with Image.open(source) as img:
                        img = ImageOps.exif_transpose(img)
                        if img.mode in ("RGBA", "LA", "P"):
                            bg = Image.new("RGB", img.size, (18, 20, 28))
                            img = img.convert("RGBA")
                            bg.paste(img, mask=img.split()[-1])
                            img = bg
                        else:
                            img = img.convert("RGB")
                        img.thumbnail((width, width * 2))
                        img.save(cache, "JPEG", quality=82, optimize=True)
            except Exception:
                self._placeholder(cache, width)
            return cache if cache.is_file() else None

    def _video_duration(self, video: Path) -> Optional[float]:
        if imageio_ffmpeg is None:
            return None
        try:
            proc = subprocess.run(
                [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(video)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
        except Exception:
            return None
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def _video_frame_at(self, video: Path, seconds: float) -> Optional[Path]:
        if imageio_ffmpeg is None:
            return None
        target = self.cache_dir / f"frame_{abs(hash(str(video) + ':' + str(seconds)))}.jpg"
        if target.is_file() and target.stat().st_size > 0:
            return target
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        try:
            subprocess.run(
                [
                    ffmpeg, "-y", "-loglevel", "error",
                    "-ss", f"{seconds:.3f}", "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=480:-1",
                    str(target),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
            )
        except Exception:
            return None
        return target if target.is_file() else None

    def _video_frame(self, video: Path) -> Optional[Path]:
        duration = self._video_duration(video)
        return self._video_frame_at(video, duration / 2 if duration else 1.0)

    @staticmethod
    def _image_is_unhelpful(path: Path) -> bool:
        """True for all-white / all-black / flat images that make useless thumbs."""
        try:
            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail((64, 64))
                stat = ImageStat.Stat(img)
                mean = sum(stat.mean) / 3
                std = sum(stat.stddev) / 3
                return std < 4 or (mean > 235 and std < 12)
        except Exception:
            return False

    def _better_thumb_source(
        self, post_id: int, media: List[Tuple[str, str]], index: int
    ) -> Optional[Path]:
        """Find a non-flat image or alternate video frame for an unhelpful cover."""
        # Other carousel images are often the useful one.
        for rel, kind in media:
            if kind != "image":
                continue
            candidate = self.media_root / rel
            if candidate.is_file() and not self._image_is_unhelpful(candidate):
                return candidate
        # Try several video frames instead of a white/black poster.
        for rel, kind in media:
            if kind != "video":
                continue
            video = self.media_root / rel
            if not video.is_file():
                continue
            total = self._video_duration(video)
            times = [1.0, 0.25, 0.75]
            if total:
                times = [total * 0.25, total * 0.5, total * 0.75, 1.0]
            for seconds in times:
                frame = self._video_frame_at(video, seconds)
                if frame and not self._image_is_unhelpful(frame):
                    return frame
        return None

    @staticmethod
    def _placeholder(path: Path, width: int) -> None:
        img = Image.new("RGB", (width, width), (20, 24, 34))
        draw = ImageDraw.Draw(img)
        draw.ellipse((width * 0.36, width * 0.36, width * 0.64, width * 0.64),
                     fill=(40, 46, 62))
        draw.text((width * 0.46, width * 0.47), "IG", fill=(140, 150, 175))
        img.save(path, "JPEG", quality=80)

    def media_file(self, post_id: int, index: int) -> Optional[Path]:
        media = self._media_paths(post_id)
        if 0 <= index < len(media):
            path = self.media_root / media[index][0]
            if path.is_file():
                return path
        return None


class Handler(BaseHTTPRequestHandler):
    archive: Archive

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _json(self, code: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: Optional[str] = None,
              cache: bool = True) -> None:
        size = path.stat().st_size
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                if match.group(1):
                    start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
                if start <= end:
                    status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-cache")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _static(self, name: str) -> None:
        path = (WEB_ROOT / name).resolve()
        if not str(path).startswith(str(WEB_ROOT.resolve())) or not path.is_file():
            self._json(404, {"error": "not found"})
            return
        self._file(path, cache=False)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._static("index.html")
            return
        if path.startswith("/static/"):
            self._static(path[len("/static/"):])
            return

        if path == "/api/posts":
            offset = int(query.get("offset", ["0"])[0])
            limit = min(int(query.get("limit", ["60"])[0]), 120)
            items, total = self.archive.list_posts(
                offset=offset,
                limit=limit,
                q=query.get("q", [""])[0],
                category=query.get("category", ["all"])[0],
                tag=query.get("tag", [""])[0],
                sort=query.get("sort", ["manual"])[0],
                owner=query.get("owner", [""])[0],
            )
            self._json(200, {"items": items, "total": total, "offset": offset})
            return

        if path == "/api/facets":
            self._json(200, self.archive.facets())
            return

        match = re.match(r"^/api/post/(\d+)$", path)
        if match:
            post = self.archive.get_post(
                int(match.group(1)),
                with_comments=query.get("comments", ["0"])[0] == "1",
            )
            self._json(200 if post else 404, post or {"error": "not found"})
            return

        match = re.match(r"^/api/thumb/(\d+)$", path)
        if match:
            width = int(query.get("w", ["360"])[0])
            index = int(query.get("i", ["0"])[0])
            thumb = self.archive.thumbnail(int(match.group(1)), width, index)
            if thumb:
                self._file(thumb, "image/jpeg")
            else:
                self._json(404, {"error": "no thumbnail"})
            return

        match = re.match(r"^/api/cover/(\d+)$", path)
        if match:
            cover = self.archive._cover_path(int(match.group(1)))
            if cover:
                self._file(cover)
            else:
                self._json(404, {"error": "no cover"})
            return

        match = re.match(r"^/api/media/(\d+)/(\d+)$", path)
        if match:
            media = self.archive.media_file(int(match.group(1)), int(match.group(2)))
            if media:
                self._file(media)
            else:
                self._json(404, {"error": "no media"})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid json"})
            return

        match = re.match(r"^/api/post/(\d+)$", parsed.path)
        if match:
            ok = self.archive.update_post(int(match.group(1)), payload)
            self._json(200 if ok else 404, {"ok": ok})
            return

        match = re.match(r"^/api/open_folder/(\d+)$", parsed.path)
        if match:
            folder = self.archive.folder_path(int(match.group(1)))
            if not folder:
                self._json(404, {"ok": False, "error": "folder missing"})
                return
            try:
                subprocess.Popen(["xdg-open", str(folder)])
                self._json(200, {"ok": True, "path": str(folder)})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/api/bulk_category":
            ok = self.archive.bulk_category(
                [int(value) for value in payload.get("ids", [])],
                str(payload.get("category") or ""),
            )
            self._json(200, {"ok": bool(ok), "count": ok})
            return

        if parsed.path == "/api/reorder":
            ok = self.archive.reorder(
                int(payload.get("id", 0)),
                payload.get("before_id"),
                payload.get("after_id"),
            )
            self._json(200 if ok else 400, {"ok": ok})
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Browse saved Instagram archive")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--media", default="data/posts")
    parser.add_argument("--cache", default="data/.cache")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()

    Handler.archive = Archive(
        Path(args.db), Path(args.media), Path(args.cache)
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Archive browser: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
