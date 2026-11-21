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

from PIL import Image, ImageDraw, ImageOps

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
        return conn

    # ---------------- listing ----------------
    def list_posts(self, *, offset: int, limit: int, q: str, category: str,
                   tag: str, sort: str) -> Tuple[List[Dict[str, Any]], int]:
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
            where.append("p.category = ?")
            params.append(category)
        if tag:
            where.append(
                "EXISTS (SELECT 1 FROM post_tags t WHERE t.media_id=p.media_id AND t.tag = ?)"
            )
            params.append(tag)
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        order = {
            "manual": "p.sort_order ASC, p.id ASC",
            "newest": "p.taken_at DESC, p.id DESC",
            "oldest": "p.taken_at ASC, p.id ASC",
            "likes": "p.likes DESC, p.id DESC",
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
                    (SELECT COUNT(*) FROM post_media m WHERE m.media_id = p.media_id) AS media_count
                    FROM posts p {clause}
                    ORDER BY {order}
                    LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
        finally:
            conn.close()

        items = []
        for row in rows:
            items.append(
                {
                    "id": row["id"],
                    "shortcode": row["shortcode"],
                    "owner": row["owner_username"],
                    "caption": (row["caption"] or "")[:280],
                    "likes": row["likes"],
                    "taken_at": row["taken_at"],
                    "is_video": bool(row["is_video"]),
                    "category": row["category"],
                    "tags": (row["tag_list"] or "").split("|") if row["tag_list"] else [],
                    "notes": row["notes"] or "",
                    "media_count": row["media_count"] or 0,
                    "thumb": f"/api/thumb/{row['id']}",
                }
            )
        return items, total

    def facets(self) -> Dict[str, Any]:
        conn = self.connect()
        try:
            cats = conn.execute(
                "SELECT COALESCE(category,'other') c, COUNT(*) n FROM posts GROUP BY c ORDER BY n DESC"
            ).fetchall()
            tags = conn.execute(
                "SELECT tag, COUNT(*) n FROM post_tags GROUP BY tag ORDER BY n DESC LIMIT 200"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            videos = conn.execute("SELECT COUNT(*) FROM posts WHERE is_video=1").fetchone()[0]
        finally:
            conn.close()
        return {
            "total": total,
            "videos": videos,
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

    def update_post(self, post_id: int, payload: Dict[str, Any]) -> bool:
        allowed = {"category": "category", "notes": "notes"}
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

    def thumbnail(self, post_id: int, width: int = 360) -> Optional[Path]:
        cache = self.thumb_dir / f"{post_id}_{width}.jpg"
        if cache.is_file() and cache.stat().st_size > 0:
            return cache
        with lock_for(f"{post_id}_{width}"):
            if cache.is_file() and cache.stat().st_size > 0:
                return cache
            source = self._cover_path(post_id)
            if source is None:
                media = self._media_paths(post_id)
                image = next(
                    (self.media_root / p for p, k in media if k == "image"),
                    None,
                )
                video = next(
                    (self.media_root / p for p, k in media if k == "video"),
                    None,
                )
                source = image
                if source is None and video is not None:
                    source = self._video_frame(video)
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

    def _video_frame(self, video: Path) -> Optional[Path]:
        if imageio_ffmpeg is None:
            return None
        target = self.cache_dir / f"frame_{abs(hash(str(video)))}.jpg"
        if target.is_file():
            return target
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        try:
            subprocess.run(
                [
                    ffmpeg, "-y", "-loglevel", "error", "-ss", "1",
                    "-i", str(video), "-frames:v", "1", "-vf", "scale=480:-1",
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
            thumb = self.archive.thumbnail(int(match.group(1)), width)
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
