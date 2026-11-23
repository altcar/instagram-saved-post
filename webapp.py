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
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageOps, ImageStat

from rednote_source import RedNoteArchive
from rednote_source import safe_dir as rn_safe_dir
from rednote_source import safe_file as rn_safe_file
from threads_source import ThreadsArchive

try:
    from cloud_store import CloudStore
except Exception:  # pragma: no cover - sync is optional
    CloudStore = None

try:
    import progress_gui as dashboard_module
except Exception:  # pragma: no cover - dashboard is optional
    dashboard_module = None

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover
    imageio_ffmpeg = None

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
HUB_BASE = os.environ.get("HUB_BASE", "http://127.0.0.1:8891").rstrip("/")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

# ---- RedNote archive ----
REDNOTE_ROOT = Path(os.environ.get("REDNOTE_ARCHIVE", "/home/over/rednote-favorites"))
REDNOTE_NOTES = REDNOTE_ROOT / "notes"
REDNOTE_THUMBS = Path(os.environ.get("REDNOTE_THUMBS", "/tmp/opencode/rednote-thumbs"))
REDNOTE_CATS = REDNOTE_ROOT / "categories.json"
REDNOTE_JOB = REDNOTE_ROOT / "job.json"
REDNOTE_JOBLOG = REDNOTE_ROOT / "job.log"
REDNOTE_TRASH = REDNOTE_ROOT / "trash"
RETRY_SCRIPT = Path("/tmp/opencode/xhs/retry_missing.py")
MEDIA_SCRIPT = Path("/tmp/opencode/xhs/repair_media.py")
UNSAVE_SCRIPT = Path("/tmp/opencode/xhs/unsave_note.js")
REDNOTE_CACHE = {"t": 0.0, "data": None}

CATEGORIES = [
    "art", "sexy", "erotic", "ai-robotics", "ai-skills", "artist",
    "meme", "music", "ad", "food", "travel", "fitness", "tech",
    "fashion", "design", "news", "other",
]

ACCOUNTS_FILE = ROOT / "data" / "accounts.json"
DEFAULT_ACCOUNTS = {
    "accounts": [
        {
            "id": "instagram-main",
            "platform": "instagram",
            "label": "Instagram",
            "handle": "shalevfrfr",
            "db": "data/archive.db",
            "media": "data/posts",
            "cache": "data/.cache",
        },
        {
            "id": "rednote-main",
            "platform": "rednote",
            "label": "RedNote",
            "handle": "小红薯6AA5FCA7",
            "root": str(REDNOTE_ROOT),
        },
    ]
}

_thumb_locks: Dict[str, threading.Lock] = {}
_thumb_locks_guard = threading.Lock()


def clean_owner(value: Any, shortcode: Any = "") -> Optional[str]:
    """Return a real handle, or None for placeholder / numeric junk values.

    Older scrapers occasionally stored an owner *id* (all digits) or a
    shortcode in owner_username. Those are not handles: rendering them as
    instagram.com/<number> produces a broken ownerLink. Real handles may start
    with digits (e.g. 1840miracle1011), so only reject values without a letter.
    """
    text = str(value or "").strip().lstrip("@")
    if not text:
        return None
    low = text.lower()
    if low in {"unknown", "none", "null", "post"} or low.startswith("post "):
        return None
    code = str(shortcode or "").strip()
    if code and text == code:
        return None
    if not re.search(r"[A-Za-z]", text):
        return None
    if "://" in text or "/" in text or " " in text:
        return None
    return text


BAD_OWNER_SQL = (
    "(p.owner_username IS NULL OR trim(p.owner_username)='' "
    "OR lower(p.owner_username) IN ('unknown','none','null','post') "
    "OR lower(p.owner_username) LIKE 'post %' "
    "OR p.owner_username NOT GLOB '*[A-Za-z]*')"
)


def _pipeline_sources() -> List[Dict[str, Any]]:
    """Accounts managed by the existing multi-account pipeline (accounts.py)."""
    try:
        import accounts as accounts_module
        registry = accounts_module.load_registry()
    except Exception:
        return []
    sources: List[Dict[str, Any]] = []
    for account in registry.accounts:
        try:
            db_path = account.path("archive_db")
        except Exception:
            continue
        if not db_path.exists():
            # A disabled account that has never downloaded anything has no
            # gallery to show yet.
            continue
        try:
            sources.append({
                "id": account.id,
                "platform": account.platform or "instagram",
                "label": account.username or account.id,
                "handle": "",
                "db": str(db_path),
                "media": str(account.path("media")),
                "cache": str(account.path("cache")),
                "home": str(account.home),
                "logs": str(account.path("logs")),
                "enabled": bool(account.enabled),
                "managed": "pipeline",
            })
        except Exception:
            continue
    return sources


def load_accounts_default() -> Dict[str, Any]:
    data = json.loads(json.dumps(DEFAULT_ACCOUNTS))
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    return data


def load_accounts() -> Dict[str, Any]:
    try:
        raw = json.loads(ACCOUNTS_FILE.read_text("utf-8"))
    except Exception:
        raw = {}
    extras = raw.get("accounts") if isinstance(raw, dict) else []
    if not isinstance(extras, list):
        extras = []

    pipeline = _pipeline_sources()
    pipeline_ids = {a["id"] for a in pipeline}
    have_instagram = any(str(a.get("platform", "instagram")).lower() == "instagram"
                         for a in pipeline)
    accounts: List[Dict[str, Any]] = list(pipeline)

    for item in extras:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        entry = dict(item)
        if entry["id"] in pipeline_ids:
            continue
        # Older UI registry used instagram-main for the legacy DB. When the
        # pipeline registry exists it already exposes that account.
        if have_instagram and entry["id"] == "instagram-main":
            continue
        entry.setdefault("platform", "instagram")
        entry.setdefault("label", entry["platform"].title())
        entry["managed"] = "builtin" if entry["id"] == "rednote-main" else "ui"
        accounts.append(entry)

    if not any(str(a.get("platform", "")).lower() == "rednote" for a in accounts):
        accounts.append({
            "id": "rednote-main",
            "platform": "rednote",
            "label": "RedNote",
            "handle": "小红薯6AA5FCA7",
            "root": str(REDNOTE_ROOT),
            "managed": "builtin",
        })
    threads_root = Path(os.environ.get(
        "THREADS_ARCHIVE", "/home/over/opencode-linux-x64/threads-archiver"))
    if (threads_root / "data" / "archive.db").is_file():
        if not any(str(a.get("platform", "")).lower() == "threads" for a in accounts):
            accounts.append({
                "id": "threads",
                "platform": "threads",
                "label": "Threads",
                "handle": "",
                "root": str(threads_root),
                "managed": "builtin",
            })
    x_root = Path(os.environ.get(
        "X_ARCHIVE", "/home/over/opencode-linux-x64/x-archiver"))
    if (x_root / "data" / "archive.db").is_file():
        if not any(str(a.get("platform", "")).lower() in ("twitter", "x")
                   for a in accounts):
            accounts.append({
                "id": "twitter",
                "platform": "twitter",
                "label": "X / Twitter",
                "handle": "",
                "root": str(x_root),
                "managed": "builtin",
            })
    if not accounts:
        return load_accounts_default()

    seen = set()
    unique = []
    for account in accounts:
        if account["id"] in seen:
            continue
        seen.add(account["id"])
        unique.append(account)
    data = {"accounts": unique}
    return data


def save_accounts(data: Dict[str, Any]) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ui_only = [
        a for a in (data.get("accounts") or [])
        if isinstance(a, dict) and a.get("managed") == "ui"
    ]
    tmp = ACCOUNTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"accounts": ui_only}, ensure_ascii=False, indent=1),
                   "utf-8")
    tmp.replace(ACCOUNTS_FILE)


def resolve_path(value: str, default: str) -> Path:
    text = str(value or default or "")
    path = Path(text)
    return path if path.is_absolute() else (ROOT / path)


CLOUD = None  # optional CloudStore, configured in main()


def notify_cloud_change() -> None:
    if CLOUD is not None:
        try:
            CLOUD.mark_dirty()
        except Exception:
            pass


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
        self.source_id = ""

    def _src(self) -> str:
        if not self.source_id:
            return ""
        return "&source=" + urllib.parse.quote(self.source_id)

    def _refresh_cover_flags(self) -> None:
        """Cheaply mark covers whose file exists, so fresh account DBs do not
        report every post as a broken cover just because cover_ok defaulted 0."""
        now = time.time()
        if getattr(self, "_cover_checked_at", 0) and now - self._cover_checked_at < 60:
            return
        self._cover_checked_at = now
        try:
            conn = sqlite3.connect(self.db_path, timeout=2)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=1500")
            rows = conn.execute(
                "SELECT id, cover FROM posts WHERE COALESCE(cover_ok,0)=0 "
                "AND cover IS NOT NULL AND trim(cover)<>'' LIMIT 5000"
            ).fetchall()
            updates = [(row["id"],) for row in rows
                       if (self.media_root / row["cover"]).is_file()]
            if updates:
                conn.executemany("UPDATE posts SET cover_ok=1 WHERE id=?", updates)
                conn.commit()
            conn.close()
        except Exception:
            # A downloading pipeline may hold the write lock. Never block the
            # gallery for a cosmetic flag; the next call will retry.
            return

    def quick_stats(self) -> Dict[str, int]:
        """Read-only post counts without migrations or writes (for the switcher)."""
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            conn.execute("PRAGMA busy_timeout=1000")
            total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            videos = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE is_video=1").fetchone()[0]
            return {"total": int(total), "videos": int(videos)}
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        last: Optional[Exception] = None
        for attempt in range(4):
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = sqlite3.connect(self.db_path, timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=15000")
                if not getattr(self, "_schema_ready", False):
                    self._migrate(conn)
                    self._schema_ready = True
                return conn
            except sqlite3.OperationalError as exc:
                last = exc
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                time.sleep(0.2 * (attempt + 1))
        if last is not None:
            raise last
        raise sqlite3.OperationalError("could not open archive database")

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for column in ("cover_ok INTEGER", "comments_count INTEGER DEFAULT 0",
                       "is_ad INTEGER DEFAULT 0"):
            try:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "CREATE TABLE IF NOT EXISTS post_categories ("
            "media_id TEXT NOT NULL, category TEXT NOT NULL, "
            "confidence REAL DEFAULT 1.0, source TEXT DEFAULT 'manual', "
            "PRIMARY KEY (media_id, category))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS categories ("
            "name TEXT PRIMARY KEY, position INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT DEFAULT '')"
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
        has_categories = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        if not has_categories:
            rows = conn.execute(
                "SELECT category, COUNT(*) n FROM post_categories "
                "WHERE trim(category) <> '' GROUP BY category "
                "ORDER BY n DESC, category ASC"
            ).fetchall()
            conn.executemany(
                "INSERT OR IGNORE INTO categories (name, position, updated_at) "
                "VALUES (?, ?, '')",
                [(row["category"], index * 100) for index, row in enumerate(rows)],
            )
            conn.commit()
        else:
            missing = conn.execute(
                "SELECT pc.category, COUNT(*) n FROM post_categories pc "
                "WHERE trim(pc.category) <> '' AND pc.category NOT IN "
                "(SELECT name FROM categories) GROUP BY pc.category "
                "ORDER BY n DESC, pc.category ASC"
            ).fetchall()
            if missing:
                base = conn.execute(
                    "SELECT COALESCE(MAX(position), -100) + 100 FROM categories"
                ).fetchone()[0]
                conn.executemany(
                    "INSERT OR IGNORE INTO categories (name, position, updated_at) "
                    "VALUES (?, ?, '')",
                    [(row["category"], base + index * 100)
                     for index, row in enumerate(missing)],
                )
                conn.commit()

    # ---------------- listing ----------------
    def list_posts(self, *, offset: int, limit: int, q: str, category: str,
                   tag: str, sort: str, owner: str = "",
                   flags: str = "", categories: str = "") -> Tuple[List[Dict[str, Any]], int]:
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
        flag_set = {f.strip() for f in (flags or "").split(",") if f.strip()}
        if "no_video" in flag_set:
            where.append("p.is_video = 0")
        if "no_image" in flag_set:
            where.append(
                "NOT EXISTS (SELECT 1 FROM post_media im "
                "WHERE im.media_id=p.media_id AND im.kind='image')"
            )
        if "no_ads" in flag_set:
            where.append("COALESCE(p.is_ad,0) = 0")
        if "bad_owner" in flag_set:
            where.append(BAD_OWNER_SQL)
        if "bad_cover" in flag_set:
            where.append(
                "(p.cover IS NULL OR trim(p.cover)='' OR COALESCE(p.cover_ok,0)=0)"
            )
        if "white_cover" in flag_set:
            where.append("COALESCE(p.cover_ok,0)=0")
        if "bad_caption" in flag_set or "caption_empty" in flag_set:
            where.append("(p.caption IS NULL OR trim(p.caption)='')")
        if "caption_error" in flag_set:
            where.append(
                "(p.caption LIKE '429%' OR p.caption LIKE '%Please wait%' "
                "OR lower(trim(p.caption)) LIKE 'error%')"
            )
        if "bad_shortcode" in flag_set:
            where.append(
                "(p.shortcode IS NULL OR trim(p.shortcode)='' "
                "OR p.shortcode NOT GLOB '*[A-Za-z]*')"
            )
        if "bad_comments" in flag_set or "comments_error" in flag_set:
            where.append("p.comments_fetched_at IS NULL")
        if "comments_empty" in flag_set:
            where.append(
                "p.comments_fetched_at IS NOT NULL "
                "AND COALESCE(p.comments_count,0)=0"
            )
        if "still_saved" in flag_set:
            where.append("p.unsaved_at IS NULL")
        if "already_unsaved" in flag_set:
            where.append("p.unsaved_at IS NOT NULL")
        if "uncategorised" in flag_set:
            where.append(
                "NOT EXISTS (SELECT 1 FROM post_categories pc "
                "WHERE pc.media_id=p.media_id)"
            )
        cat_filter = [c.strip() for c in (categories or "").split(",") if c.strip()]
        if cat_filter:
            marks = ",".join("?" for _ in cat_filter)
            where.append(
                f"EXISTS (SELECT 1 FROM post_categories pc "
                f"WHERE pc.media_id=p.media_id AND pc.category IN ({marks}))"
            )
            params.extend(cat_filter)
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        self._refresh_cover_flags()

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
            cover_path = self.media_root / row["cover"] if row["cover"] else None
            try:
                version = int(cover_path.stat().st_mtime) if cover_path and cover_path.is_file() else row["id"]
            except OSError:
                version = row["id"]
            media = [
                {
                    "i": index,
                    "kind": media_row["kind"],
                    "thumb": f"/api/thumb/{row['id']}?i={index}&v={version}{self._src()}",
                    "url": f"/api/media/{row['id']}/{index}?v={version}{self._src()}",
                }
                for index, media_row in enumerate(media_rows)
            ]
            owner = clean_owner(row["owner_username"], row["shortcode"])
            items.append(
                {
                    "id": row["id"],
                    "source": "instagram",
                    "shortcode": row["shortcode"],
                    "owner": owner,
                    "owner_url": f"https://www.instagram.com/{owner}/" if owner else None,
                    "permalink": f"https://www.instagram.com/p/{row['shortcode']}/",
                    "caption": (row["caption"] or "")[:280],
                    "likes": row["likes"],
                    "taken_at": row["taken_at"],
                    "is_video": bool(row["is_video"]),
                    "typename": row["typename"],
                    "location": row["location"] or "",
                    "category": (
                        (row["category_list"] or "").split("|")[0]
                        if row["category_list"] else row["category"]
                    ),
                    "categories": [
                        c for c in (row["category_list"] or "").split("|") if c
                    ],
                    "is_ad": bool(row["is_ad"]) if "is_ad" in row.keys() else False,
                    "confidence": row["category_confidence"],
                    "source_label": row["category_source"],
                    "tags": (row["tag_list"] or "").split("|") if row["tag_list"] else [],
                    "notes": row["notes"] or "",
                    "media_count": row["media_count"] or 0,
                    "media": media,
                    "thumb": f"/api/thumb/{row['id']}?i=0&v={version}{self._src()}",
                    "cover": f"/api/cover/{row['id']}?v={version}{self._src()}",
                    "saved_state": "unsaved" if row["unsaved_at"] else "saved",
                    "comments_count": row["comments_count"] or 0,
                    "has_comments": row["comments_fetched_at"] is not None,
                }
            )
        conn.close()
        return items, total

    def expected_total(self, total: int) -> int:
        """Current archive + items still in the live saved feed, deduplicated."""
        feed = self.db_path.parent / "raw" / "items.jsonl"
        if not feed.is_file():
            return total
        codes: set[str] = set()
        try:
            with feed.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    media = item.get("media") or item
                    code = media.get("code")
                    if code:
                        codes.add(str(code))
        except OSError:
            return total
        if not codes:
            return total
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            existing = {
                str(row[0])
                for row in conn.execute(
                    "SELECT shortcode FROM posts WHERE shortcode IS NOT NULL"
                )
            }
        except sqlite3.Error:
            return total
        finally:
            conn.close()
        return max(total, len(existing | codes))

    def facets(self) -> Dict[str, Any]:
        self._refresh_cover_flags()
        conn = self.connect()
        try:
            cat_counts = {
                row["category"]: row["n"]
                for row in conn.execute(
                    "SELECT category, COUNT(*) n FROM post_categories "
                    "WHERE trim(category) <> '' GROUP BY category"
                )
            }
            cat_order = [
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM categories ORDER BY position ASC, name ASC"
                )
            ]
            categories = [
                {"name": name, "count": int(cat_counts.get(name, 0))}
                for name in cat_order
            ]
            for name, count in sorted(cat_counts.items(),
                                      key=lambda kv: (-kv[1], kv[0])):
                if name not in cat_order:
                    categories.append({"name": name, "count": int(count)})
            tags = conn.execute(
                "SELECT tag, COUNT(*) n FROM post_tags GROUP BY tag ORDER BY n DESC LIMIT 200"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            videos = conn.execute("SELECT COUNT(*) FROM posts WHERE is_video=1").fetchone()[0]
            ads = conn.execute("SELECT COUNT(*) FROM posts WHERE is_ad=1").fetchone()[0]
            counts = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN is_video = 0 THEN 1 ELSE 0 END) AS no_video,
                  SUM(CASE WHEN NOT EXISTS (
                        SELECT 1 FROM post_media im
                        WHERE im.media_id = posts.media_id AND im.kind = 'image'
                      ) THEN 1 ELSE 0 END) AS no_image,
                  SUM(CASE WHEN COALESCE(is_ad,0) = 0 THEN 1 ELSE 0 END) AS no_ads,
                  SUM(CASE WHEN owner_username IS NULL
                        OR trim(owner_username) = ''
                        OR lower(owner_username) IN ('unknown','none','null','post')
                        OR lower(owner_username) LIKE 'post %'
                        OR owner_username NOT GLOB '*[A-Za-z]*'
                      THEN 1 ELSE 0 END) AS bad_owner,
                  SUM(CASE WHEN cover IS NULL OR trim(cover) = ''
                        OR COALESCE(cover_ok,0) = 0
                      THEN 1 ELSE 0 END) AS bad_cover,
                  SUM(CASE WHEN caption IS NULL OR trim(caption) = ''
                      THEN 1 ELSE 0 END) AS caption_empty,
                  SUM(CASE WHEN caption LIKE '429%'
                        OR caption LIKE '%Please wait%'
                        OR lower(trim(caption)) LIKE 'error%'
                      THEN 1 ELSE 0 END) AS caption_error,
                  SUM(CASE WHEN shortcode IS NULL OR trim(shortcode) = ''
                        OR shortcode NOT GLOB '*[A-Za-z]*'
                      THEN 1 ELSE 0 END) AS bad_shortcode,
                  SUM(CASE WHEN comments_fetched_at IS NULL
                      THEN 1 ELSE 0 END) AS comments_error,
                  SUM(CASE WHEN comments_fetched_at IS NOT NULL
                        AND COALESCE(comments_count,0) = 0
                      THEN 1 ELSE 0 END) AS comments_empty,
                  SUM(CASE WHEN unsaved_at IS NULL THEN 1 ELSE 0 END) AS still_saved,
                  SUM(CASE WHEN unsaved_at IS NOT NULL THEN 1 ELSE 0 END) AS already_unsaved,
                  SUM(CASE WHEN NOT EXISTS (
                        SELECT 1 FROM post_categories pc
                        WHERE pc.media_id = posts.media_id
                      ) THEN 1 ELSE 0 END) AS uncategorised,
                  SUM(CASE WHEN comments_fetched_at IS NOT NULL
                      THEN 1 ELSE 0 END) AS comments_fetched
                FROM posts
                """
            ).fetchone()
        finally:
            conn.close()
        filter_counts = {
            key: int(counts[key] or 0)
            for key in (
                "no_video", "no_image", "no_ads", "bad_owner", "bad_cover",
                "caption_empty", "caption_error", "bad_shortcode",
                "comments_empty", "comments_error", "still_saved",
                "already_unsaved", "uncategorised",
            )
        }
        return {
            "total": total,
            "expected": self.expected_total(total),
            "videos": videos,
            "ads": ads,
            "filter_counts": filter_counts,
            "categories": categories,
            "category_list": categories,
            "tags": [{"name": t["tag"], "count": t["n"]} for t in tags],
            "presets": [c["name"] for c in categories] or CATEGORIES,
            "saved": {
                "still_saved": filter_counts["still_saved"],
                "already_unsaved": filter_counts["already_unsaved"],
            },
            "stats": {
                "comments": int(counts["comments_fetched"] or 0),
            },
        }

    def get_post(self, post_id: int, with_comments: bool = False,
                 comments_offset: int = 0,
                 comments_limit: int = 30) -> Optional[Dict[str, Any]]:
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
        cover_path = self.media_root / row["cover"] if row["cover"] else None
        try:
            version = int(cover_path.stat().st_mtime) if cover_path and cover_path.is_file() else row["id"]
        except OSError:
            version = row["id"]
        owner = clean_owner(row["owner_username"], row["shortcode"])
        data: Dict[str, Any] = {
            "id": row["id"],
            "source": "instagram",
            "media_id": row["media_id"],
            "shortcode": row["shortcode"],
            "permalink": f"https://www.instagram.com/p/{row['shortcode']}/",
            "owner": owner,
            "owner_url": f"https://www.instagram.com/{owner}/" if owner else None,
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
            "source_label": row["source"],
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
                    "url": f"/api/media/{row['id']}/{i}?v={version}{self._src()}",
                    "thumb": f"/api/thumb/{row['id']}?i={i}&v={version}{self._src()}",
                    "i": i,
                }
                for i, m in enumerate(media)
            ],
            "thumb": f"/api/thumb/{row['id']}?i=0&v={version}{self._src()}",
            "cover": f"/api/cover/{row['id']}?v={version}{self._src()}",
            "saved_state": "unsaved" if row["unsaved_at"] else "saved",
            "comments_count": row["comments_count"] or 0,
            "has_comments": row["comments_fetched_at"] is not None,
        }
        if with_comments:
            data["comments_data"] = self.comments(post_id, comments_offset, comments_limit)
            data["comments"] = data["comments_data"]["items"]
        return data

    def comments(self, post_id: int, offset: int = 0, limit: int = 30) -> Dict[str, Any]:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT folder, comments_count FROM posts WHERE id=?", (post_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"total": 0, "items": [], "hasMore": False, "offset": offset}
        folder = self.media_root / row["folder"] if row["folder"] else None
        raw: Any = []
        if folder:
            comments_file = folder / "comments.json"
            if comments_file.is_file():
                try:
                    raw = json.loads(comments_file.read_text(encoding="utf-8"))
                except Exception:
                    raw = []
        if isinstance(raw, dict):
            raw = raw.get("comments") or raw.get("items") or []
        if not isinstance(raw, list):
            raw = []
        items = []
        for entry in raw[offset: offset + limit]:
            if not isinstance(entry, dict):
                continue
            items.append({
                "id": entry.get("id") or entry.get("pk") or "",
                "owner": entry.get("owner") or entry.get("username") or "",
                "text": entry.get("text") or entry.get("comment") or "",
                "created_at": entry.get("created_at") or entry.get("createdAt") or "",
                "likes": entry.get("likes") or 0,
                "replies": [],
            })
        return {
            "total": max(int(row["comments_count"] or 0), len(raw)),
            "rootCount": len(raw),
            "offset": offset,
            "limit": limit,
            "hasMore": offset + limit < len(raw),
            "items": items,
        }

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
                for value in values:
                    self._register_category(conn, value)
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
                    self._register_category(conn, value)
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
            notify_cloud_change()
            return True
        finally:
            conn.close()

    @staticmethod
    def _register_category(conn: sqlite3.Connection, name: str) -> None:
        if not name:
            return
        exists = conn.execute(
            "SELECT 1 FROM categories WHERE name=?", (name,)
        ).fetchone()
        if exists:
            return
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -100) + 100 FROM categories"
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, position, updated_at) "
            "VALUES (?, ?, ?)",
            (name, position, time.strftime("%Y-%m-%dT%H:%M:%S")),
        )

    def bulk_category(self, ids: List[int], category: str) -> int:
        category = (category or "").strip()
        if not ids or not category:
            return 0
        conn = self.connect()
        try:
            self._register_category(conn, category)
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
            notify_cloud_change()
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

    # ---------------- category registry ----------------
    def category_list(self) -> List[Dict[str, Any]]:
        return self.facets()["category_list"]

    def category_op(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        op = str(payload.get("op") or "").strip()
        name = str(payload.get("name") or "").strip()
        conn = self.connect()
        try:
            if op == "add" and name:
                self._register_category(conn, name)
            elif op == "rename" and name:
                new_name = str(payload.get("new_name") or "").strip()
                if new_name and new_name != name:
                    conn.execute(
                        "UPDATE OR IGNORE categories SET name=?, updated_at=? "
                        "WHERE name=?",
                        (new_name, time.strftime("%Y-%m-%dT%H:%M:%S"), name),
                    )
                    conn.execute("DELETE FROM categories WHERE name=?", (name,))
                    conn.execute(
                        "UPDATE OR IGNORE post_categories SET category=? "
                        "WHERE category=?",
                        (new_name, name),
                    )
                    conn.execute(
                        "DELETE FROM post_categories WHERE category=?", (name,)
                    )
                    conn.execute(
                        "UPDATE posts SET category=? WHERE category=?",
                        (new_name, name),
                    )
            elif op == "remove" and name:
                conn.execute("DELETE FROM categories WHERE name=?", (name,))
                conn.execute("DELETE FROM post_categories WHERE category=?", (name,))
                conn.execute(
                    "UPDATE posts SET category=NULL WHERE category=?", (name,)
                )
            elif op == "reorder":
                names = [str(n).strip() for n in (payload.get("names") or [])
                         if str(n).strip()]
                for index, value in enumerate(names):
                    conn.execute(
                        "UPDATE categories SET position=? WHERE name=?",
                        (index * 100, value),
                    )
            conn.commit()
        finally:
            conn.close()
        notify_cloud_change()
        return {
            "categories": self.category_list(),
            "uncategorised": self.facets()["filter_counts"]["uncategorised"],
        }

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
            if index == 0:
                # The cover itself is flat/white: fall back to a useful slide.
                source = self._better_thumb_source(post_id, media, index)
            elif kind == "video" and rel is not None:
                # A video slide may have a flat poster. Use a frame from that
                # same video, not another carousel image (which used to make
                # several slides show the identical thumbnail).
                video = self.media_root / rel
                if video.is_file():
                    total = self._video_duration(video)
                    times = [total * 0.25, total * 0.5, total * 0.75] if total else [1.0, 0.5]
                    for seconds in times:
                        frame = self._video_frame_at(video, seconds)
                        if frame and not self._image_is_unhelpful(frame):
                            source = frame
                            break

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



def _rn_safe_dir(name: str):
    name = urllib.parse.unquote(name)
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    folder = (REDNOTE_NOTES / name)
    try:
        if folder.resolve().parent != REDNOTE_NOTES.resolve() or not folder.is_dir():
            return None
    except OSError:
        return None
    return folder


def _rn_safe_file(folder: Path, name: str):
    name = urllib.parse.unquote(name)
    if not name or name in {".", ".."} or Path(name).name != name:
        return None
    path = folder / name
    if not path.is_file():
        return None
    if path.suffix.lower() not in (IMAGE_EXTS | VIDEO_EXTS):
        return None
    return path


def _rn_num(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    mult = 1
    if text.endswith("万"):
        mult, text = 10000, text[:-1]
    elif text.endswith("w") or text.endswith("W"):
        mult, text = 10000, text[:-1]
    elif text.endswith("k") or text.endswith("K"):
        mult, text = 1000, text[:-1]
    try:
        return int(float(text) * mult)
    except Exception:
        return 0


def _rn_account() -> Dict[str, Any]:
    path = REDNOTE_ROOT / "account.json"
    data: Dict[str, Any] = {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:
        data = {}
    return {
        "saved_count": int(data.get("saved_count", 0) or 0),
        "archived": int(data.get("archived", 0) or 0),
        "remaining_saved": int(data.get("remaining_saved", data.get("saved_count", 0)) or 0),
        "total_seen": int(data.get("total_seen", 327) or 327),
        "accessible": int(data.get("accessible", 0) or 0),
        "unavailable": int(data.get("unavailable", 0) or 0),
        "synced_at": data.get("synced_at", ""),
        "nickname": data.get("nickname", ""),
        "verification_required": bool(data.get("verification_required")),
        "verification_url": data.get("verification_url", ""),
        "last_retry": data.get("last_retry", ""),
        "last_retry_result": data.get("last_retry_result", ""),
    }


def _rn_cover(note: Dict[str, Any], files: List[str]) -> str:
    if "cover.jpg" in files:
        return "cover.jpg"
    images = sorted(f for f in files if re.match(r"image_\d+\.(jpg|jpeg|png|webp)$", f))
    if images:
        return images[0]
    return ""


def rn_build_index(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if not force and REDNOTE_CACHE["data"] and now - REDNOTE_CACHE["t"] < 5:
        return REDNOTE_CACHE["data"]

    items: List[Dict[str, Any]] = []
    comments_total = 0
    media_files = 0
    media_bytes = 0
    replies_ok = 0
    if REDNOTE_NOTES.is_dir():
        for folder in sorted(REDNOTE_NOTES.iterdir()):
            if not folder.is_dir():
                continue
            state: Dict[str, Any] = {}
            note: Dict[str, Any] = {}
            comments: Dict[str, Any] = {}
            try:
                state = json.loads((folder / "_state.json").read_text("utf-8"))
            except Exception:
                pass
            try:
                note = json.loads((folder / "note.json").read_text("utf-8")).get("note", {})
            except Exception:
                note = {}
            try:
                comments = json.loads((folder / "comments.json").read_text("utf-8"))
            except Exception:
                comments = {}

            files: List[str] = []
            size = 0
            try:
                for entry in folder.iterdir():
                    if entry.is_file() and entry.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS):
                        files.append(entry.name)
                        size += entry.stat().st_size
            except OSError:
                pass
            files.sort()
            media_files += len(files)
            media_bytes += size

            ctotal = int(comments.get("total") or 0)
            comments_total += ctotal
            if comments.get("subsComplete") and ctotal:
                replies_ok += 1

            user = note.get("user") or {}
            stats = note.get("interactInfo") or {}
            tags = [str(t.get("name")) for t in (note.get("tagList") or []) if t and t.get("name")]
            stamp = note.get("time") or 0
            date = time.strftime("%Y-%m-%d", time.gmtime(int(stamp) / 1000)) if stamp else (state.get("date") or "")
            title = note.get("title") or state.get("title") or folder.name.split("_", 1)[-1]
            desc = note.get("desc") or ""
            items.append({
                "dir": folder.name,
                "id": note.get("noteId") or folder.name.split("_")[0],
                "title": title,
                "desc": desc[:700],
                "type": note.get("type") or "normal",
                "author": user.get("nickname") or user.get("nickName") or "",
                "authorId": user.get("userId") or "",
                "date": date,
                "tags": tags,
                "likes": _rn_num(stats.get("likedCount") or stats.get("liked_count")),
                "collects": _rn_num(stats.get("collectedCount") or stats.get("collected_count")),
                "comments": ctotal,
                "repliesComplete": bool(comments.get("subsComplete")),
                "status": state.get("status") or "unknown",
                "cover": _rn_cover(note, files),
                "files": files,
                "size": size,
            })

    items.sort(key=lambda x: (x["date"], x["id"]), reverse=True)
    account = _rn_account()
    account["accessible"] = len(items)
    account["archived"] = len(items)
    data = {
        "items": items,
        "total": len(items),
        "account": account,
        "stats": {
            "comments": comments_total,
            "mediaFiles": media_files,
            "mediaBytes": media_bytes,
            "repliesComplete": replies_ok,
        },
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    REDNOTE_CACHE.update(t=now, data=data)
    return data


def rn_detail(folder: Path, offset: int = 0, limit: int = 20) -> Dict[str, Any]:
    note: Dict[str, Any] = {}
    state: Dict[str, Any] = {}
    comments: Dict[str, Any] = {}
    try:
        note = json.loads((folder / "note.json").read_text("utf-8")).get("note", {})
    except Exception:
        pass
    try:
        state = json.loads((folder / "_state.json").read_text("utf-8"))
    except Exception:
        pass
    try:
        comments = json.loads((folder / "comments.json").read_text("utf-8"))
    except Exception:
        comments = {}
    roots = comments.get("comments") or []
    sliced = []
    for root in roots[offset: offset + limit]:
        root = dict(root)
        subs = root.get("subComments") or []
        root["subTotal"] = len(subs)
        root["subComments"] = subs[:8]
        sliced.append(root)
    files = sorted(entry.name for entry in folder.iterdir()
                   if entry.is_file() and entry.suffix.lower() in (IMAGE_EXTS | VIDEO_EXTS))
    return {
        "dir": folder.name,
        "note": note,
        "state": state,
        "files": files,
        "comments": {
            "total": int(comments.get("total") or 0),
            "rootCount": len(roots),
            "offset": offset,
            "limit": limit,
            "hasMore": offset + limit < len(roots),
            "subsComplete": bool(comments.get("subsComplete")),
            "items": sliced,
        },
    }


def rn_thumb(path: Path, width: int = 480) -> Optional[Path]:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    key = hashlib.md5(f"{path}|{stamp}|{width}".encode("utf-8")).hexdigest()
    out = REDNOTE_THUMBS / f"{key}.jpg"
    if out.is_file():
        return out
    try:
        REDNOTE_THUMBS.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((width, width * 2))
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(out, "JPEG", quality=82)
        return out
    except Exception:
        return None



def rn_load_cats() -> Dict[str, Any]:
    try:
        data = json.loads(REDNOTE_CATS.read_text("utf-8"))
    except Exception:
        data = {}
    cats = [str(c) for c in data.get("categories", []) if str(c).strip()]
    assignments = data.get("assignments", {}) or {}
    if not isinstance(assignments, dict):
        assignments = {}
    return {"categories": cats, "assignments": assignments}


def rn_save_cats(data: Dict[str, Any]) -> None:
    tmp = REDNOTE_CATS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(REDNOTE_CATS)
    REDNOTE_CACHE["t"] = 0


def rn_cat_summary() -> Dict[str, Any]:
    data = rn_load_cats()
    items = rn_build_index()["items"]
    counts: Dict[str, int] = {c: 0 for c in data["categories"]}
    unsorted = 0
    for item in items:
        assigned = [c for c in (data["assignments"].get(item["dir"]) or []) if c in counts]
        if not assigned:
            unsorted += 1
        for c in assigned:
            counts[c] += 1
    return {
        "categories": data["categories"],
        "counts": counts,
        "unsorted": unsorted,
        "total": len(items),
        "assignments": data["assignments"],
    }


def _rn_pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text("utf-8")
        state = stat.rsplit(")", 1)[1].split()[0]
        if state == "Z":
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass
            return False
        return True
    except OSError:
        return False


def rn_job_running() -> Optional[Dict[str, Any]]:
    try:
        job = json.loads(REDNOTE_JOB.read_text("utf-8"))
    except Exception:
        return None
    pid = int(job.get("pid") or 0)
    if not pid or not _rn_pid_alive(pid):
        return None
    return job


def rn_start_job(kind: str, env_extra: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    running = rn_job_running()
    if running:
        return {"ok": False, "error": "a job is already running", "job": running}
    env = os.environ.copy()
    env.setdefault("HOME", str(Path.home()))
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items() if v is not None})
    if kind in ("download_all", "retry"):
        cmd = ["python3", str(RETRY_SCRIPT)]
    elif kind in ("repair_all", "media"):
        cmd = ["python3", "/tmp/opencode/xhs/repair_all.py"]
    elif kind == "comments_all":
        cmd = ["python3", "/tmp/opencode/xhs/comments_all.py"]
    elif kind == "unsave_validated":
        cmd = ["xvfb-run", "-a", "env", "-u", "WAYLAND_DISPLAY", "node", "/tmp/opencode/xhs/unsave_validated.js"]
    elif kind in ("unsave", "unsave_all"):
        script = UNSAVE_SCRIPT if kind == "unsave" else Path("/tmp/opencode/xhs/unsave_all.js")
        cmd = ["xvfb-run", "-a", "env", "-u", "WAYLAND_DISPLAY", "node", str(script)]
    else:
        return {"ok": False, "error": "unknown job type"}
    REDNOTE_CACHE["t"] = 0
    log = REDNOTE_JOBLOG.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True, cwd="/tmp/opencode/xhs")
    finally:
        log.close()
    job = {"kind": kind, "pid": proc.pid, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
           "log": str(REDNOTE_JOBLOG)}
    if env_extra and env_extra.get("RN_DIR"):
        job["dir"] = env_extra["RN_DIR"]
    REDNOTE_JOB.write_text(json.dumps(job, ensure_ascii=False, indent=1), "utf-8")
    return {"ok": True, "job": job}


def rn_job_status() -> Dict[str, Any]:
    job = None
    try:
        raw = json.loads(REDNOTE_JOB.read_text("utf-8"))
    except Exception:
        raw = None
    if raw:
        pid = int(raw.get("pid") or 0)
        job = dict(raw); job["running"] = _rn_pid_alive(pid)
    lines: List[str] = []
    try:
        with REDNOTE_JOBLOG.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16000))
            lines = fh.read().decode("utf-8", "replace").splitlines()[-40:]
    except Exception:
        pass
    cron_lines: List[str] = []
    cron_file = REDNOTE_ROOT / "cron.log"
    try:
        with cron_file.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8000))
            cron_lines = fh.read().decode("utf-8", "replace").splitlines()[-12:]
    except Exception:
        pass
    scan = {}
    try:
        scan = json.loads((REDNOTE_ROOT / "scan_status.json").read_text("utf-8"))
    except Exception:
        scan = {}
    unsave = {}
    try:
        unsave = json.loads((REDNOTE_ROOT / "unsave_status.json").read_text("utf-8"))
    except Exception:
        unsave = {}
    job_progress = {}
    try:
        job_progress = json.loads((REDNOTE_ROOT / "job_progress.json").read_text("utf-8"))
    except Exception:
        job_progress = {}
    return {"job": job, "log": lines, "cronLog": cron_lines, "scan": scan, "unsave": unsave, "progress": job_progress,
            "cronLast": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cron_file.stat().st_mtime)) if cron_file.exists() else ""}


_source_cache: Dict[str, Any] = {}
_source_lock = threading.Lock()


def get_source(source_id: str = ""):
    """Return (account, adapter) for a registry source id.

    Adapters expose the same methods: list_posts, facets, get_post, update_post,
    bulk_category, reorder, comments, category_op.
    """
    registry = load_accounts()
    accounts = registry["accounts"]
    account = None
    if source_id:
        account = next((a for a in accounts if a.get("id") == source_id), None)
    if account is None:
        account = next(
            (a for a in accounts if a.get("platform") == "instagram"), accounts[0]
        )
    key = str(account.get("id"))
    with _source_lock:
        adapter = _source_cache.get(key)
        if adapter is None:
            platform = str(account.get("platform") or "instagram").lower()
            if platform == "rednote":
                root = resolve_path(account.get("root", ""), str(REDNOTE_ROOT))
                thumbs = account.get("thumbs")
                adapter = RedNoteArchive(
                    root,
                    Path(thumbs) if thumbs else None,
                )
            elif platform == "threads":
                root = resolve_path(account.get("root", ""),
                                    "/home/over/opencode-linux-x64/threads-archiver")
                adapter = ThreadsArchive(
                    root,
                    cache_dir=ROOT / "data" / ".cache" / "threads-thumbs",
                    meta_path=ROOT / "data" / "threads_meta.json",
                )
            elif platform in ("twitter", "x"):
                root = resolve_path(account.get("root", ""),
                                    "/home/over/opencode-linux-x64/x-archiver")
                adapter = ThreadsArchive(
                    root,
                    cache_dir=ROOT / "data" / ".cache" / "x-thumbs",
                    meta_path=ROOT / "data" / "x_meta.json",
                    platform="twitter",
                    domain="x.com",
                )
            else:
                adapter = Archive(
                    resolve_path(account.get("db", ""), "data/archive.db"),
                    resolve_path(account.get("media", ""), "data/posts"),
                    resolve_path(account.get("cache", ""), "data/.cache"),
                )
            adapter.source_id = key
            _source_cache[key] = adapter
    return account, adapter


def account_summary(account: Dict[str, Any]) -> Dict[str, Any]:
    item = {
        "id": account.get("id"),
        "platform": account.get("platform") or "instagram",
        "label": account.get("label") or account.get("platform") or "Source",
        "handle": account.get("handle") or "",
        "managed": account.get("managed") or "",
        "enabled": account.get("enabled", True),
        "total": None,
        "videos": None,
        "error": None,
    }
    try:
        _, adapter = get_source(str(account.get("id")))
        if isinstance(adapter, RedNoteArchive):
            facets = adapter.facets()
            item["total"] = facets.get("total", 0)
            item["videos"] = facets.get("videos", 0)
        else:
            stats = adapter.quick_stats()
            item["total"] = stats.get("total", 0)
            item["videos"] = stats.get("videos", 0)
    except Exception as exc:
        item["error"] = str(exc)
    return item


def build_cloud_state() -> Dict[str, Any]:
    """Snapshot every source's personal state (categories, notes, tags)."""
    state: Dict[str, Any] = {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": {},
    }
    for account in load_accounts()["accounts"]:
        try:
            _, adapter = get_source(str(account.get("id")))
        except Exception:
            continue
        if isinstance(adapter, ThreadsArchive):
            meta = adapter.meta()
            state["sources"][account["id"]] = {
                "platform": adapter.platform,
                "categories": meta["categories"],
                "assignments": meta["assignments"],
                "notes": meta["notes"],
                "tags": meta["tags"],
                "manual_order": meta["manual_order"],
            }
            continue
        if isinstance(adapter, RedNoteArchive):
            cats = adapter.load_categories()
            state["sources"][account["id"]] = {
                "platform": "rednote",
                "categories": cats["categories"],
                "assignments": cats["assignments"],
                "order": cats["order"],
                "meta": adapter.load_meta(),
                "note_order": adapter.load_order(),
            }
            continue
        conn = adapter.connect()
        try:
            categories = [r["name"] for r in conn.execute(
                "SELECT name FROM categories ORDER BY position, name")]
            notes = {r["media_id"]: r["notes"] for r in conn.execute(
                "SELECT media_id, notes FROM posts "
                "WHERE notes IS NOT NULL AND trim(notes) <> ''")}
            tags: Dict[str, List[str]] = {}
            for row in conn.execute("SELECT media_id, tag FROM post_tags"):
                tags.setdefault(row["media_id"], []).append(row["tag"])
            assignments: Dict[str, List[str]] = {}
            for row in conn.execute("SELECT media_id, category FROM post_categories"):
                assignments.setdefault(row["media_id"], []).append(row["category"])
        finally:
            conn.close()
        state["sources"][account["id"]] = {
            "platform": "instagram",
            "categories": categories,
            "notes": notes,
            "tags": tags,
            "assignments": assignments,
        }
    return state


def apply_cloud_state(state: Dict[str, Any]) -> bool:
    """Restore a pulled snapshot. Used only by an explicit pull request."""
    sources = state.get("sources") or {}
    if not isinstance(sources, dict):
        return False
    applied = False
    for account in load_accounts()["accounts"]:
        remote = sources.get(account.get("id"))
        if not isinstance(remote, dict):
            continue
        try:
            _, adapter = get_source(str(account.get("id")))
        except Exception:
            continue
        if isinstance(adapter, ThreadsArchive):
            adapter.save_meta({
                "categories": [str(c) for c in (remote.get("categories") or [])],
                "assignments": remote.get("assignments") or {},
                "notes": remote.get("notes") or {},
                "tags": remote.get("tags") or {},
                "manual_order": remote.get("manual_order") or [],
            })
            adapter.scan(force=True)
            applied = True
            continue
        if isinstance(adapter, RedNoteArchive):
            cats = {
                "categories": [str(c) for c in (remote.get("categories") or [])],
                "assignments": remote.get("assignments") or {},
                "order": remote.get("order") or remote.get("categories") or [],
            }
            adapter.save_categories(cats)
            if isinstance(remote.get("meta"), dict):
                adapter.save_meta(remote["meta"])
            if isinstance(remote.get("note_order"), list):
                adapter.save_order([str(x) for x in remote["note_order"]])
            adapter.scan(force=True)
            applied = True
            continue
        conn = adapter.connect()
        try:
            names = [str(c) for c in (remote.get("categories") or []) if str(c)]
            conn.execute("DELETE FROM categories")
            conn.executemany(
                "INSERT OR IGNORE INTO categories (name, position, updated_at) "
                "VALUES (?, ?, ?)",
                [(name, index * 100, time.strftime("%Y-%m-%dT%H:%M:%S"))
                 for index, name in enumerate(names)],
            )
            for media_id, notes in (remote.get("notes") or {}).items():
                conn.execute("UPDATE posts SET notes=? WHERE media_id=?",
                             (str(notes), media_id))
            for media_id, tags in (remote.get("tags") or {}).items():
                conn.execute("DELETE FROM post_tags WHERE media_id=?", (media_id,))
                conn.executemany(
                    "INSERT OR IGNORE INTO post_tags (media_id, tag) VALUES (?,?)",
                    [(media_id, str(tag)) for tag in tags],
                )
            for media_id, cats in (remote.get("assignments") or {}).items():
                conn.execute("DELETE FROM post_categories WHERE media_id=?", (media_id,))
                conn.executemany(
                    "INSERT OR IGNORE INTO post_categories "
                    "(media_id, category, confidence, source) VALUES (?,?,?,?)",
                    [(media_id, str(cat), 1.0, "manual") for cat in cats],
                )
            conn.commit()
            applied = True
        finally:
            conn.close()
    return applied


def live_snapshot(account: Dict[str, Any], adapter: Any) -> Dict[str, Any]:
    platform = str(account.get("platform") or "instagram").lower()
    if platform in ("threads", "twitter", "x"):
        is_x = platform in ("twitter", "x")
        port = 8772 if is_x else 8771
        state: Dict[str, Any] = {}
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state",
                                        timeout=3) as response:
                state = json.loads(response.read().decode("utf-8"))
        except Exception:
            state = {}
        totals = state.get("totals") or {}
        scraper = state.get("scraper") or {}
        try:
            stats = adapter.quick_stats()
        except Exception:
            stats = {}
        done = int(scraper.get("done") or 0)
        total = int(scraper.get("total") or totals.get("posts") or 0)
        log = []
        for entry in state.get("log") or []:
            if isinstance(entry, dict):
                log.append(f"{entry.get('ts','')} {entry.get('level','')} {entry.get('msg','')}".strip())
        total_posts = int(stats.get("total") or totals.get("posts") or 0)
        unsaved = int(totals.get("unsaved") or 0) if is_x else 0
        phase = str(scraper.get("phase") or "scrape")
        running = phase not in ("", "done", "finished", "idle") and not scraper.get("finished")
        return {
            "kind": "twitter" if is_x else "threads",
            "source": account.get("id"),
            "title": account.get("label") or ("X / Twitter" if is_x else "Threads"),
            "stats": {
                "total": total_posts,
                "videos": int(stats.get("videos") or 0),
                "comments": int(totals.get("comments") or 0),
                "media_files": int(totals.get("media_done") or 0),
                "still_saved": max(0, total_posts - unsaved) if is_x else 0,
                "already_unsaved": unsaved,
                "archived": int(totals.get("archived") or 0),
                "pending": int(totals.get("pending") or 0),
            },
            "job": {
                "kind": phase,
                "running": running,
                "started": "",
            },
            "progress": {
                "kind": phase,
                "done": done,
                "total": total,
                "percent": round(100 * done / total) if total else 0,
                "current": str(scraper.get("current") or ""),
                "message": str(scraper.get("lastErr") or ""),
                "running": running,
            },
            "log": log[-30:],
            "actions": [],
            "dashboard": None,
            "verify": {"required": False, "url": ""},
        }
    if platform == "rednote":
        status = rn_job_status()
        progress = status.get("progress") or {}
        account_info = _rn_account()
        facets = adapter.facets()
        total = progress.get("total") or 0
        done = progress.get("done") or 0
        job = status.get("job") or {}
        return {
            "kind": "rednote",
            "source": account.get("id"),
            "title": "RedNote",
            "stats": {
                "total": facets.get("total", 0),
                "videos": facets.get("videos", 0),
                "comments": facets.get("stats", {}).get("comments", 0),
                "media_files": facets.get("stats", {}).get("media_files", 0),
                "still_saved": facets.get("saved", {}).get("still_saved", 0),
                "already_unsaved": facets.get("saved", {}).get("already_unsaved", 0),
                "live_saved": account_info.get("saved_count", 0),
                "remaining": account_info.get("remaining_saved", 0),
            },
            "job": {
                "kind": job.get("kind") or progress.get("kind") or "",
                "running": bool(job.get("running")) or bool(progress.get("running")),
                "started": job.get("started") or "",
            },
            "progress": {
                "kind": progress.get("kind") or "",
                "done": done,
                "total": total,
                "percent": round(100 * done / total) if total else 0,
                "current": progress.get("current") or "",
                "message": progress.get("message") or "",
                "running": bool(progress.get("running")),
            },
            "log": (status.get("log") or [])[-30:],
            "actions": [
                {"type": "download_all", "label": "Download all"},
                {"type": "comments_all", "label": "Download comments"},
                {"type": "repair_all", "label": "Repair media"},
                {"type": "unsave_validated", "label": "Unsave validated"},
            ],
            "dashboard": None,
            "verify": {
                "required": bool(account_info.get("verification_required")),
                "url": account_info.get("verification_url") or "",
            },
        }
    # Instagram: the legacy archive has the full dashboard on 8766. Accounts
    # managed by run_accounts.py have their own log/lock, so read those.
    db_path = Path(str(account.get("db") or ""))
    is_legacy = db_path.resolve() == (ROOT / "data" / "archive.db").resolve()
    if is_legacy:
        dashboard = "http://127.0.0.1:8766/"
        live: Dict[str, Any] = {}
        try:
            with urllib.request.urlopen(dashboard + "api/status", timeout=3) as resp:
                live = json.loads(resp.read().decode("utf-8"))
        except Exception:
            live = {}
        pipeline = live.get("pipeline") or {}
        if not pipeline and dashboard_module is not None:
            try:
                pipeline = dashboard_module.db_progress(adapter.db_path)
            except Exception:
                pipeline = {}
        try:
            quick = adapter.quick_stats()
        except Exception:
            quick = {}
        total_count = int(pipeline.get("total") or quick.get("total") or 0)
        video_count = int(quick.get("videos") or 0)
        progress = {
            "kind": "download",
            "done": live.get("position") or 0,
            "total": live.get("total") or total_count,
            "percent": round(100 * (live.get("position") or 0) /
                              max(1, live.get("total") or total_count or 1)),
            "current": live.get("shortcode") or "",
            "message": live.get("last_line") or "",
            "running": live.get("status") == "running",
        }
        return {
            "kind": "instagram",
            "source": account.get("id"),
            "title": account.get("label") or "Instagram",
            "stats": {
                "total": total_count,
                "videos": video_count,
                "comments": pipeline.get("comments", 0),
                "media_files": total_count,
                "still_saved": pipeline.get("still_saved", 0),
                "already_unsaved": pipeline.get("unsaved", 0),
                "missing_media": pipeline.get("missing_media", 0),
                "covers": pipeline.get("covers", 0),
            },
            "job": {
                "kind": live.get("status") or "",
                "running": live.get("status") == "running",
                "started": "",
            },
            "progress": progress,
            "log": (live.get("tail") or [])[-20:],
            "actions": [],
            "dashboard": dashboard,
            "verify": {"required": False, "url": ""},
        }

    facets = adapter.facets()
    logs_dir = Path(str(account.get("logs") or ""))
    home_dir = Path(str(account.get("home") or ""))
    report: Dict[str, Any] = {}
    try:
        report = json.loads((logs_dir / "last_run.json").read_text("utf-8"))
    except Exception:
        report = {}
    log_lines: List[str] = []
    try:
        with (logs_dir / "runner.log").open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 12000))
            log_lines = fh.read().decode("utf-8", "replace").splitlines()[-22:]
    except Exception:
        log_lines = []
    lock: Dict[str, Any] = {}
    try:
        lock = json.loads((home_dir / ".pipeline.lock").read_text("utf-8"))
    except Exception:
        lock = {}
    running = _rn_pid_alive(int(lock.get("pid") or 0))
    done = sum(1 for line in log_lines if line.startswith("Downloading"))
    if report:
        progress = {
            "kind": str(report.get("status") or "pipeline"),
            "done": int(report.get("downloaded") or done),
            "total": int(report.get("total") or report.get("saved_total") or 0),
            "percent": 0,
            "current": "",
            "message": str(report.get("status") or ""),
            "running": running,
        }
    else:
        progress = {
            "kind": "pipeline",
            "done": done,
            "total": 0,
            "percent": 0,
            "current": "",
            "message": "running account pipeline" if running else
                       ("last log: " + (log_lines[-1] if log_lines else "no runs yet")),
            "running": running,
        }
    return {
        "kind": "instagram",
        "source": account.get("id"),
        "title": account.get("label") or "Instagram",
        "stats": {
            "total": facets.get("total", 0),
            "videos": facets.get("videos", 0),
            "comments": facets.get("stats", {}).get("comments", 0),
            "media_files": facets.get("total", 0),
            "still_saved": facets.get("saved", {}).get("still_saved", 0),
            "already_unsaved": facets.get("saved", {}).get("already_unsaved", 0),
        },
        "job": {
            "kind": "pipeline",
            "running": running,
            "started": lock.get("since") or "",
        },
        "progress": progress,
        "log": log_lines,
        "actions": [],
        "dashboard": None,
        "verify": {"required": False, "url": ""},
    }


def dashboard_config(account: Dict[str, Any], adapter: Any) -> Optional[Dict[str, Any]]:
    """Paths for the native 8766-style dashboard for any Instagram archive."""
    if dashboard_module is None or not isinstance(adapter, Archive):
        return None
    try:
        legacy = adapter.db_path.resolve() == (ROOT / "data" / "archive.db").resolve()
    except OSError:
        legacy = False
    logs_dir = Path(str(account.get("logs") or ""))
    if legacy:
        return {
            "data_dir": adapter.media_root,
            "log_path": Path("/tmp/opencode/incr2.log"),
            "reference_total": 12028,
            "db_path": adapter.db_path,
            "comments_log": Path("/tmp/opencode/comments_all.log"),
        }
    return {
        "data_dir": adapter.media_root,
        "log_path": logs_dir / "runner.log",
        "reference_total": adapter.quick_stats().get("total", 0),
        "db_path": adapter.db_path,
        "comments_log": None,
    }


def dashboard_is_legacy(adapter: Any) -> bool:
    if not isinstance(adapter, Archive):
        return False
    try:
        return adapter.db_path.resolve() == (ROOT / "data" / "archive.db").resolve()
    except OSError:
        return False


def dashboard_logs(account: Dict[str, Any], adapter: Any) -> List[Dict[str, Any]]:
    config = dashboard_config(account, adapter)
    if config is None:
        return []
    if dashboard_is_legacy(adapter):
        return dashboard_module.log_catalog(config["log_path"], config["comments_log"])
    logs_dir = Path(str(account.get("logs") or ""))
    entries = [
        ("runner", logs_dir / "runner.log"),
        ("pipeline", logs_dir / "pipeline.log"),
    ]
    out = []
    for name, path in entries:
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        out.append({"name": name, "path": str(path), "exists": path.is_file(), "size": size})
    return out


_dashboard_cache: Dict[str, Any] = {}
_dashboard_lock = threading.Lock()
_accounts_cache: Tuple[float, Any] = (0.0, None)


def dashboard_status(account: Dict[str, Any], adapter: Any) -> Optional[Dict[str, Any]]:
    key = str(account.get("id") or "")
    now = time.time()
    with _dashboard_lock:
        cached = _dashboard_cache.get(key)
        if cached and now - cached[0] < 2.0:
            return cached[1]
    config = dashboard_config(account, adapter)
    if config is None:
        return None
    legacy = dashboard_is_legacy(adapter)
    try:
        status = dashboard_module.build_status(
            **config, detect_process=legacy, dir_ttl=60)
    except Exception:
        return None
    status["source"] = account.get("id")
    status["startable"] = legacy
    if not legacy:
        # The multi-account runner owns these jobs. Show the same information
        # but do not offer Start/Pause buttons that would target another tree.
        home = Path(str(account.get("home") or ""))
        lock: Dict[str, Any] = {}
        try:
            lock = json.loads((home / ".pipeline.lock").read_text("utf-8"))
        except Exception:
            lock = {}
        running = _rn_pid_alive(int(lock.get("pid") or 0))
        status["status"] = "running" if running else "stopped"
        status["pid"] = lock.get("pid") if running else None
        status["elapsed"] = None
        status["rate"] = 0.0
        status["eta"] = None
        for job in status.get("jobs", []):
            job["available"] = False
            job["rate"] = 0.0
            job["eta"] = None
            job["pid"] = None
            job["elapsed"] = None
            if job.get("name") == "download" and running:
                job["status"] = "running"
            elif job.get("name") == "unsave":
                count = int(job.get("count") or 0)
                total = int(job.get("total") or 0)
                job["status"] = "verified" if total and count >= total else "partial"
            else:
                count = int(job.get("count") or 0)
                total = int(job.get("total") or 0)
                job["status"] = "verified" if total and count >= total else "partial"
        status["logs"] = dashboard_logs(account, adapter)
    with _dashboard_lock:
        _dashboard_cache[key] = (now, status)
    return status


def dashboard_read_log(catalog: List[Dict[str, Any]], name: str, lines: int) -> List[str]:
    if name == "all":
        chunks: List[str] = []
        for item in catalog:
            path = Path(item["path"])
            if not path.is_file():
                continue
            try:
                tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            except OSError:
                continue
            chunks.append(f"===== {item['name']} =====")
            chunks.extend(tail)
        return chunks
    path = next((Path(item["path"]) for item in catalog if item["name"] == name), None)
    if not path or not path.is_file():
        return [f"log not found: {name}"]
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return [f"cannot read {path}"]


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
        self.send_header("Access-Control-Allow-Origin", "*")
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
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # keep one bad request from killing the app
            try:
                self._json(500, {"error": str(exc)})
            except Exception:
                pass

    def _do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if (path == "/" or path == "/index.html"
                or path in ("/rednote", "/rednote/")
                or path in ("/gallery", "/gallery/", "/gallery/index.html")):
            # One shared gallery shell for every platform/account. The former
            # standalone RedNote page now uses the same UI, so filters,
            # selection, dragging and the drawer cannot drift apart again.
            self._static("index.html")
            return
        if path.startswith("/static/"):
            self._static(path[len("/static/"):])
            return

        source_id = query.get("source", [""])[0]
        try:
            account, adapter = get_source(source_id)
        except Exception as exc:
            self._json(500, {"error": f"source unavailable: {exc}"})
            return

        if path == "/api/accounts":
            global _accounts_cache
            accounts = load_accounts()["accounts"]
            now = time.time()
            if _accounts_cache[1] is not None and now - _accounts_cache[0] < 10:
                summaries = list(_accounts_cache[1])
            else:
                summaries = [account_summary(a) for a in accounts]
                _accounts_cache = (now, summaries)
            active_id = account.get("id")
            # Empty placeholder DBs created by the multi-account runner should
            # not clutter the switcher until they contain posts. Keep the active
            # source even if it is still empty.
            summaries = [s for s in summaries
                         if (s.get("total") or 0) > 0 or s.get("id") == active_id]
            summaries.sort(key=lambda a: (a["error"] is not None,
                                           -(a["total"] or 0),
                                           str(a["label"])))
            self._json(200, {
                "accounts": summaries,
                "active": active_id,
                "hub": HUB_BASE,
                "services": [
                    {
                        "id": "telegram",
                        "platform": "telegram",
                        "label": "Telegram",
                        "handle": "",
                        "href": f"{HUB_BASE}/telegram",
                    },
                ],
            })
            return

        if path == "/api/posts":
            offset = int(query.get("offset", ["0"])[0])
            limit = min(int(query.get("limit", ["60"])[0]), 120)
            items, total = adapter.list_posts(
                offset=offset,
                limit=limit,
                q=query.get("q", [""])[0],
                category=query.get("category", ["all"])[0],
                tag=query.get("tag", [""])[0],
                sort=query.get("sort", ["manual"])[0],
                owner=query.get("owner", [""])[0],
                flags=query.get("flags", [""])[0],
                categories=query.get("categories", [""])[0],
            )
            self._json(200, {"items": items, "total": total, "offset": offset,
                             "source": account.get("id")})
            return

        if path == "/api/facets":
            self._json(200, adapter.facets())
            return

        if path == "/api/live":
            self._json(200, live_snapshot(account, adapter))
            return

        if path == "/api/dashboard":
            status = dashboard_status(account, adapter)
            if status is None:
                self._json(404, {"error": "dashboard is not available for this source"})
                return
            self._json(200, status)
            return

        if path == "/api/dashboard/log":
            if dashboard_config(account, adapter) is None:
                self._json(404, {"error": "dashboard is not available for this source"})
                return
            name = query.get("name", ["download"])[0] or "download"
            lines = max(10, min(400, int(query.get("lines", ["160"])[0])))
            catalog = dashboard_logs(account, adapter)
            self._json(200, {"name": name,
                             "lines": dashboard_read_log(catalog, name, lines)})
            return

        if path == "/api/categories":
            self._json(200, {
                "categories": adapter.category_list(),
                "uncategorised": adapter.facets()["filter_counts"].get("uncategorised", 0),
            })
            return

        if path == "/api/cloud":
            if CLOUD is None:
                self._json(200, {"enabled": False, "provider": "none"})
            else:
                self._json(200, CLOUD.status())
            return

        match = re.match(r"^/api/post/(\d+)$", path)
        if match:
            post = adapter.get_post(
                int(match.group(1)),
                with_comments=query.get("comments", ["0"])[0] == "1",
                comments_offset=max(0, int(query.get("offset", ["0"])[0])),
                comments_limit=min(100, max(1, int(query.get("limit", ["30"])[0]))),
            )
            self._json(200 if post else 404, post or {"error": "not found"})
            return

        match = re.match(r"^/api/comments/(\d+)$", path)
        if match:
            offset = max(0, int(query.get("offset", ["0"])[0]))
            limit = min(100, max(1, int(query.get("limit", ["30"])[0])))
            self._json(200, adapter.comments(int(match.group(1)), offset, limit))
            return

        match = re.match(r"^/api/thumb/(\d+)$", path)
        if match:
            if not hasattr(adapter, "media_file"):
                self._json(404, {"error": "no thumbnail"})
                return
            width = int(query.get("w", ["360"])[0])
            index = int(query.get("i", ["0"])[0])
            thumb = adapter.thumbnail(int(match.group(1)), width, index)
            if thumb:
                self._file(thumb, "image/jpeg")
            else:
                self._json(404, {"error": "no thumbnail"})
            return

        match = re.match(r"^/api/cover/(\d+)$", path)
        if match:
            if not hasattr(adapter, "_cover_path"):
                self._json(404, {"error": "no cover"})
                return
            cover = adapter._cover_path(int(match.group(1)))
            if cover:
                self._file(cover)
            else:
                self._json(404, {"error": "no cover"})
            return

        match = re.match(r"^/api/comment_media/(\d+)/([^/]+)/(\d+)$", path)
        if match:
            if not hasattr(adapter, "comment_media_file"):
                self._json(404, {"error": "no comment media"})
                return
            media = adapter.comment_media_file(
                int(match.group(1)), match.group(2), int(match.group(3)))
            if media:
                self._file(media)
            else:
                self._json(404, {"error": "no comment media"})
            return

        match = re.match(r"^/api/media/(\d+)/(\d+)$", path)
        if match:
            if not hasattr(adapter, "media_file"):
                self._json(404, {"error": "no media"})
                return
            media = adapter.media_file(int(match.group(1)), int(match.group(2)))
            if media:
                self._file(media)
            else:
                self._json(404, {"error": "no media"})
            return

        # ---------------- RedNote saved posts ----------------
        if path in ("/rednote", "/rednote/"):
            self._static("rednote.html")
            return

        if path == "/api/rednote/notes":
            force = query.get("refresh", ["0"])[0] == "1"
            self._json(200, rn_build_index(force=force))
            return

        match = re.match(r"^/api/rednote/note/([^/]+)$", path)
        if match:
            folder = _rn_safe_dir(match.group(1))
            if not folder:
                self._json(404, {"error": "note not found"})
                return
            offset = max(0, int(query.get("offset", ["0"])[0]))
            limit = min(100, max(1, int(query.get("limit", ["20"])[0])))
            self._json(200, rn_detail(folder, offset, limit))
            return

        match = re.match(r"^/api/rednote/comments/([^/]+)$", path)
        if match:
            folder = _rn_safe_dir(match.group(1))
            if not folder:
                self._json(404, {"error": "note not found"})
                return
            offset = max(0, int(query.get("offset", ["0"])[0]))
            limit = min(100, max(1, int(query.get("limit", ["20"])[0])))
            self._json(200, rn_detail(folder, offset, limit)["comments"])
            return

        if path == "/api/rednote/categories":
            self._json(200, rn_cat_summary())
            return

        if path == "/api/rednote/jobs/status":
            self._json(200, rn_job_status())
            return

        match = re.match(r"^/api/rednote/(thumb|media)/([^/]+)/([^/]+)$", path)
        if match:
            kind, dir_token, file_token = match.groups()
            if isinstance(adapter, RedNoteArchive):
                if kind == "thumb":
                    width = min(1200, max(120, int(query.get("w", ["480"])[0])))
                    thumb = adapter.thumbnail(dir_token, file_token, width)
                    if thumb:
                        self._file(thumb, "image/jpeg")
                    else:
                        self._json(404, {"error": "no thumbnail"})
                else:
                    folder = rn_safe_dir(adapter.root, dir_token)
                    target = rn_safe_file(folder, file_token) if folder else None
                    if target:
                        self._file(target)
                    else:
                        self._json(404, {"error": "file not found"})
                return
            folder = _rn_safe_dir(dir_token)
            if not folder:
                self._json(404, {"error": "note not found"})
                return
            target = _rn_safe_file(folder, file_token)
            if not target:
                if kind == "thumb" and "cover.jpg" in {x.name for x in folder.iterdir()}:
                    target = folder / "cover.jpg"
                else:
                    self._json(404, {"error": "file not found"})
                    return
            if kind == "thumb":
                width = min(1200, max(120, int(query.get("w", ["480"])[0])))
                thumb = rn_thumb(target, width)
                if thumb:
                    self._file(thumb, "image/jpeg")
                else:
                    self._json(404, {"error": "no thumbnail"})
            else:
                self._file(target)
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._do_POST()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self._json(500, {"error": str(exc)})
            except Exception:
                pass

    def _do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid json"})
            return

        try:
            account, adapter = get_source(query.get("source", [""])[0])
        except Exception as exc:
            self._json(500, {"error": f"source unavailable: {exc}"})
            return

        config = dashboard_config(account, adapter)
        if parsed.path in ("/api/dashboard/job", "/api/dashboard/unsave/preview",
                           "/api/dashboard/unsave/run") and (
                config is None or not dashboard_is_legacy(adapter)):
            self._json(404, {"error": "job controls are only available for the legacy archive"})
            return

        if parsed.path == "/api/dashboard/job":
            name = str(payload.get("name") or "")
            action = str(payload.get("action") or "start")
            try:
                pipeline = dashboard_module.db_progress(config["db_path"])
                folders, _ = dashboard_module.dir_stats(config["data_dir"])
                specs = dashboard_module.job_specs(pipeline, folders)
                if action == "pause":
                    ok, message = dashboard_module.pause_job(specs, name)
                else:
                    ok, message = dashboard_module.start_job(specs, name)
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
                return
            with _dashboard_lock:
                _dashboard_cache.pop(str(account.get("id") or ""), None)
            self._json(200 if ok else 409, {
                "ok": ok, "message": message, "name": name, "action": action})
            return

        if parsed.path == "/api/dashboard/unsave/preview":
            criteria = str(payload.get("criteria") or "").strip()
            if not criteria:
                self._json(400, {"error": "select at least one criterion"})
                return
            try:
                output = subprocess.check_output(
                    [dashboard_module.PY, "unsave_validated.py", "--preview",
                     "--criteria", criteria],
                    cwd=ROOT, text=True, timeout=240,
                )
                self._json(200, json.loads(output))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/dashboard/unsave/run":
            criteria = str(payload.get("criteria") or "").strip()
            if not criteria:
                self._json(400, {"ok": False, "error": "select at least one criterion"})
                return
            log = Path("/tmp/opencode/unsave.log")
            log.parent.mkdir(parents=True, exist_ok=True)
            handle = open(log, "ab", buffering=0)
            try:
                subprocess.Popen(
                    [dashboard_module.PY, "unsave_validated.py", "--run",
                     "--criteria", criteria, "--confirm"],
                    cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                handle.close()
            self._json(200, {"ok": True, "message": "unsave started",
                             "criteria": criteria})
            return

        match = re.match(r"^/api/post/(\d+)$", parsed.path)
        if match:
            ok = adapter.update_post(int(match.group(1)), payload)
            if ok:
                notify_cloud_change()
            self._json(200 if ok else 404, {"ok": ok})
            return

        match = re.match(r"^/api/open_folder/(\d+)$", parsed.path)
        if match:
            folder = None
            if hasattr(adapter, "folder_path"):
                folder = adapter.folder_path(int(match.group(1)))
            elif isinstance(adapter, RedNoteArchive):
                item = adapter.get_post(int(match.group(1)), with_comments=False)
                if item:
                    folder = adapter.notes_root / item["dir"]
            elif isinstance(adapter, ThreadsArchive):
                item = adapter.get_post(int(match.group(1)), with_comments=False)
                if item:
                    folder = adapter.data_dir / "media" / item["media_id"]
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
            ok = adapter.bulk_category(
                [int(value) for value in payload.get("ids", [])],
                str(payload.get("category") or ""),
            )
            notify_cloud_change()
            self._json(200, {"ok": bool(ok), "count": ok})
            return

        if parsed.path == "/api/reorder":
            ok = adapter.reorder(
                int(payload.get("id", 0)),
                payload.get("before_id"),
                payload.get("after_id"),
            )
            self._json(200 if ok else 400, {"ok": ok})
            return

        if parsed.path == "/api/categories":
            result = adapter.category_op(payload)
            notify_cloud_change()
            self._json(200, result)
            return

        if parsed.path == "/api/accounts":
            registry = load_accounts()
            op = str(payload.get("op") or "add")
            accounts = registry["accounts"]
            if op == "remove":
                target_id = str(payload.get("id") or "")
                target = next((a for a in accounts if a.get("id") == target_id), None)
                if target and str(target.get("managed")) in ("pipeline", "builtin"):
                    self._json(409, {"error": "managed account; remove it in accounts.py"})
                    return
                registry["accounts"] = [a for a in accounts if a.get("id") != target_id]
                _source_cache.pop(target_id, None)
            else:
                target_id = str(payload.get("id") or "").strip()
                if not target_id:
                    base = re.sub(r"[^a-z0-9]+", "-",
                                  str(payload.get("label") or payload.get("platform") or "source").lower()).strip("-")
                    target_id = base or "source"
                    existing = {a.get("id") for a in accounts}
                    suffix = 2
                    while target_id in existing:
                        target_id = f"{base}-{suffix}"
                        suffix += 1
                entry = {
                    "id": target_id,
                    "platform": str(payload.get("platform") or "instagram"),
                    "label": str(payload.get("label") or "").strip() or target_id,
                    "handle": str(payload.get("handle") or "").strip(),
                    "managed": "ui",
                }
                if entry["platform"] == "rednote":
                    if payload.get("root"):
                        entry["root"] = str(payload["root"])
                else:
                    if payload.get("db"):
                        entry["db"] = str(payload["db"])
                    if payload.get("media"):
                        entry["media"] = str(payload["media"])
                    if payload.get("cache"):
                        entry["cache"] = str(payload["cache"])
                accounts = [a for a in accounts if a.get("id") != target_id]
                accounts.append(entry)
                _source_cache.pop(target_id, None)
            registry["accounts"] = accounts
            save_accounts(registry)
            globals()["_accounts_cache"] = (0.0, None)
            self._json(200, {
                "accounts": [account_summary(a) for a in accounts],
            })
            return

        if parsed.path == "/api/live/job":
            kind = str(payload.get("type") or "")
            if not isinstance(adapter, RedNoteArchive):
                self._json(400, {"error": "no jobs for this source"})
                return
            res = rn_start_job(kind)
            self._json(200 if res.get("ok") else 409, res)
            return

        if parsed.path == "/api/cloud":
            if CLOUD is None:
                self._json(200, {"enabled": False, "provider": "none"})
                return
            op = str(payload.get("op") or "status")
            try:
                if op == "push":
                    self._json(200, CLOUD.push_now(build_cloud_state()))
                elif op == "pull":
                    remote = CLOUD.pull_now()
                    applied = apply_cloud_state(remote) if remote else False
                    self._json(200, {"ok": bool(remote), "applied": applied,
                                     "state": CLOUD.status()})
                else:
                    self._json(200, CLOUD.status())
            except Exception as exc:
                self._json(500, {"error": str(exc), "state": CLOUD.status()})
            return

        if parsed.path == "/api/rednote/categories":
            op = str(payload.get("op") or "").strip()
            name = str(payload.get("name") or "").strip()
            data = rn_load_cats()
            if op == "add" and name:
                if name not in data["categories"]:
                    data["categories"].append(name)
            elif op == "remove" and name:
                data["categories"] = [c for c in data["categories"] if c != name]
                data["assignments"] = {d: [c for c in cats if c != name]
                                       for d, cats in data["assignments"].items()}
                data["assignments"] = {d: cats for d, cats in data["assignments"].items() if cats}
            rn_save_cats(data)
            self._json(200, rn_cat_summary())
            return

        if parsed.path == "/api/rednote/assign":
            folder = _rn_safe_dir(str(payload.get("dir") or ""))
            if not folder:
                self._json(404, {"error": "note not found"})
                return
            data = rn_load_cats()
            cats = [str(c) for c in (payload.get("categories") or []) if str(c) in data["categories"]]
            if cats:
                data["assignments"][folder.name] = cats
            else:
                data["assignments"].pop(folder.name, None)
            rn_save_cats(data)
            self._json(200, rn_cat_summary())
            return

        if parsed.path == "/api/rednote/remove":
            folder = _rn_safe_dir(str(payload.get("dir") or ""))
            if not folder:
                self._json(404, {"error": "note not found"})
                return
            REDNOTE_TRASH.mkdir(parents=True, exist_ok=True)
            target = REDNOTE_TRASH / (time.strftime("%Y%m%d-%H%M%S_") + folder.name)
            try:
                folder.rename(target)
            except Exception as exc:
                self._json(500, {"error": str(exc)})
                return
            data = rn_load_cats()
            data["assignments"].pop(folder.name, None)
            rn_save_cats(data)
            self._json(200, {"ok": True, "movedTo": str(target)})
            return

        if parsed.path == "/api/rednote/unsave":
            folder = _rn_safe_dir(str(payload.get("dir") or ""))
            if not folder:
                self._json(404, {"error": "note not found"})
                return
            note = {}
            token = ""
            try:
                saved = json.loads((folder / "note.json").read_text("utf-8"))
                note = saved.get("note", {})
                token = (saved.get("favorite") or {}).get("xsecToken") or note.get("xsecToken") or ""
            except Exception:
                pass
            note_id = note.get("noteId") or folder.name.split("_")[0]
            res = rn_start_job("unsave", {"RN_NOTE_ID": note_id, "RN_TOKEN": token, "RN_DIR": folder.name})
            self._json(200 if res.get("ok") else 409, res)
            return

        if parsed.path == "/api/rednote/jobs/start":
            kind = str(payload.get("type") or "retry")
            if kind not in ("download_all", "comments_all", "repair_all", "unsave_validated", "retry", "media", "unsave_all"):
                self._json(400, {"error": "bad job type"})
                return
            res = rn_start_job(kind)
            self._json(200 if res.get("ok") else 409, res)
            return

        if parsed.path == "/api/rednote/sync":
            data = rn_build_index(force=True)
            account = data["account"]
            account["synced_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                (REDNOTE_ROOT / "account.json").write_text(
                    json.dumps(account, ensure_ascii=False, indent=1), "utf-8"
                )
            except Exception:
                pass
            self._json(200, {"ok": True, "total": data["total"], "account": account})
            return

        match = re.match(r"^/api/rednote/open/([^/]+)$", parsed.path)
        if match:
            folder = _rn_safe_dir(match.group(1))
            if not folder:
                self._json(404, {"ok": False, "error": "note not found"})
                return
            try:
                subprocess.Popen(["xdg-open", str(folder)])
                self._json(200, {"ok": True, "path": str(folder)})
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    global CLOUD
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
    if CloudStore is not None:
        CLOUD = CloudStore(ROOT, builder=build_cloud_state)
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
