#!/usr/bin/env python3
"""Threads archive source for the shared Saved Archive gallery.

Reads the existing threads-archiver SQLite database/media tree and exposes the
same normalised API as the Instagram and RedNote sources. Categories, notes,
tags and manual order live in a sidecar JSON file because the Threads schema is
owned by its downloader.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageOps

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover
    imageio_ffmpeg = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def _root() -> Path:
    return Path(os.environ.get(
        "THREADS_ARCHIVE", "/home/over/opencode-linux-x64/threads-archiver"))


def _iso(epoch: Any) -> str:
    try:
        value = int(epoch)
    except Exception:
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _epoch_from_created(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(text)
        return int(parsed.timestamp())
    except Exception:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(parsed.timestamp())
    except Exception:
        return 0


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(path)


class ThreadsArchive:
    def __init__(self, root: Optional[Path] = None,
                 cache_dir: Optional[Path] = None,
                 meta_path: Optional[Path] = None,
                 platform: str = "threads",
                 domain: str = "threads.com"):
        import sqlite3
        self.sqlite = sqlite3
        self.platform = platform
        self.domain = domain
        self.root = Path(root or _root())
        self.data_dir = self.root / "data"
        self.db_path = self.data_dir / "archive.db"
        self.media_root = self.data_dir
        self.cache_dir = Path(cache_dir or self.data_dir / ".cache" / "threads-thumbs")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = Path(meta_path or self.data_dir / "gallery_meta.json")
        self.ttl = 5.0
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_at = 0.0
        self._lock = threading.RLock()
        self.source_id = ""

    # ---------------- sidecar metadata ----------------
    def meta(self) -> Dict[str, Any]:
        data = _read_json(self.meta_path, {})
        if not isinstance(data, dict):
            data = {}
        cats = [str(c).strip() for c in (data.get("categories") or []) if str(c).strip()]
        order = [str(c) for c in (data.get("order") or []) if str(c) in cats]
        order += [c for c in cats if c not in order]
        assignments = data.get("assignments") or {}
        if not isinstance(assignments, dict):
            assignments = {}
        notes = data.get("notes") or {}
        tags = data.get("tags") or {}
        manual = data.get("manual_order") or []
        return {
            "categories": order,
            "assignments": {str(k): [str(c) for c in v] for k, v in assignments.items()
                            if isinstance(v, list)},
            "notes": {str(k): str(v) for k, v in notes.items()} if isinstance(notes, dict) else {},
            "tags": {str(k): [str(t) for t in v] for k, v in tags.items()
                     if isinstance(v, list)} if isinstance(tags, dict) else {},
            "manual_order": [str(x) for x in manual] if isinstance(manual, list) else [],
        }

    def save_meta(self, data: Dict[str, Any]) -> None:
        _write_json(self.meta_path, data)
        self._cache_at = 0.0

    def _invalidate(self) -> None:
        self._cache_at = 0.0

    # ---------------- database ----------------
    def connect(self):
        conn = self.sqlite.connect(
            f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = self.sqlite.Row
        return conn

    def _media_rows(self, conn, pk: str, scope: str = "post") -> List[Any]:
        return conn.execute(
            "SELECT id,type,local_path,status,width,height,bytes FROM media "
            "WHERE post_pk=? AND scope=? ORDER BY id", (pk, scope)).fetchall()

    def scan(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            if not force and self._cache and now - self._cache_at < self.ttl:
                return self._cache
            meta = self.meta()
            posts: List[Dict[str, Any]] = []
            try:
                conn = self.connect()
                rows = conn.execute("SELECT rowid AS _id, * FROM posts").fetchall()
                comment_cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(comments)")}
                if "is_reply" in comment_cols:
                    comment_counts = {
                        row["post_pk"]: row["n"] for row in conn.execute(
                            "SELECT post_pk, COUNT(*) n FROM comments WHERE is_reply=0 "
                            "GROUP BY post_pk")
                    }
                else:
                    comment_counts = {
                        row["post_pk"]: row["n"] for row in conn.execute(
                            "SELECT post_pk, COUNT(*) n FROM comments GROUP BY post_pk")
                    }
                all_comment_counts = {
                    row["post_pk"]: row["n"] for row in conn.execute(
                        "SELECT post_pk, COUNT(*) n FROM comments GROUP BY post_pk")
                }
                for row in rows:
                    post = dict(row)
                    post["id"] = int(post.pop("_id"))
                    post.setdefault("code", "")
                    if not post["code"]:
                        post["code"] = post.get("pk") or str(post["id"])
                    if not post.get("taken_at"):
                        post["taken_at"] = _epoch_from_created(post.get("created_at"))
                    post["media"] = [dict(m) for m in self._media_rows(conn, row["pk"])]
                    post["comments_total"] = int(all_comment_counts.get(row["pk"], 0))
                    post["comments_root"] = int(comment_counts.get(row["pk"], 0))
                    posts.append(post)
                conn.close()
            except Exception:
                posts = []

            by_pk = {p["pk"]: p for p in posts}
            order = [pk for pk in meta["manual_order"] if pk in by_pk]
            rest = [p for p in posts if p["pk"] not in order]
            rest.sort(key=lambda p: (p.get("archived_at") or "", p.get("taken_at") or 0),
                      reverse=True)
            ordered = order + [p["pk"] for p in rest]
            for position, pk in enumerate(ordered):
                if pk in by_pk:
                    by_pk[pk]["sort_order"] = position * 100

            cats = meta["categories"]
            counts = {c: 0 for c in cats}
            uncategorised = 0
            for post in posts:
                assigned = [c for c in meta["assignments"].get(post["pk"], []) if c in cats]
                post["categories"] = assigned
                if assigned:
                    for c in assigned:
                        counts[c] += 1
                else:
                    uncategorised += 1
                post["notes"] = meta["notes"].get(post["pk"], "")
                post["tags"] = meta["tags"].get(post["pk"], [])
            data = {
                "posts": posts,
                "by_id": {p["id"]: p for p in posts},
                "by_pk": by_pk,
                "categories": cats,
                "counts": counts,
                "uncategorised": uncategorised,
                "meta": meta,
                "order": ordered,
            }
            self._cache, self._cache_at = data, now
            return data

    # ---------------- media ----------------
    def _post_media(self, post: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for index, row in enumerate(post.get("media") or []):
            path = self.media_root / (row.get("local_path") or "")
            if row.get("status") != "done" or not path.is_file():
                continue
            kind = "video" if str(row.get("type")) == "video" else "image"
            try:
                version = int(path.stat().st_mtime)
            except OSError:
                version = 0
            out.append({
                "i": len(out), "kind": kind, "name": Path(row["local_path"]).name,
                "path": row["local_path"],
                "url": f"/api/media/{post['id']}/{len(out)}?v={version}{self._src()}",
                "thumb": f"/api/thumb/{post['id']}?i={len(out)}&v={version}{self._src()}",
            })
        return out

    def _src(self) -> str:
        return f"&source={self.source_id}" if self.source_id else ""

    def media_items(self, post: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self._post_media(post)

    def _cover_file(self, post: Dict[str, Any]) -> Optional[Path]:
        for row in post.get("media") or []:
            path = self.media_root / (row.get("local_path") or "")
            if row.get("status") != "done" or not path.is_file():
                continue
            if str(row.get("type")) == "image" or path.suffix.lower() in IMAGE_EXTS:
                return path
        return None

    def _item(self, post: Dict[str, Any]) -> Dict[str, Any]:
        media = self._post_media(post)
        cover = self._cover_file(post)
        try:
            version = int(cover.stat().st_mtime) if cover else 0
        except OSError:
            version = 0
        author = post.get("author") or ""
        if self.platform in ("twitter", "x"):
            saved_state = "unsaved" if post.get("unsave_status") == "done" else "saved"
        else:
            saved_state = "unknown"
        return {
            "id": post["id"],
            "source": self.platform,
            "shortcode": post.get("code") or post.get("pk"),
            "owner": author or None,
            "owner_url": f"https://www.{self.domain}/@{author}" if author else None,
            "permalink": post.get("url") or (
                f"https://www.{self.domain}/@{author}/post/{post.get('code')}"
                if author and post.get("code") else None),
            "caption": (post.get("text") or "")[:280],
            "caption_full": post.get("text") or "",
            "title": (post.get("text") or "").split("\n", 1)[0][:120],
            "likes": int(post.get("like_count") or 0),
            "replies": int(post.get("reply_count") or 0),
            "reposts": int(post.get("repost_count") or 0),
            "views": int(post.get("view_count") or 0),
            "taken_at": _iso(post.get("taken_at")) or post.get("created_at") or "",
            "is_video": any(m["kind"] == "video" for m in media),
            "typename": self.platform,
            "location": "",
            "categories": list(post.get("categories") or []),
            "category": (post.get("categories") or [None])[0],
            "tags": list(post.get("tags") or []),
            "notes": post.get("notes") or "",
            "media_count": len(media),
            "media": media,
            "thumb": f"/api/thumb/{post['id']}?i=0&v={version}{self._src()}",
            "cover": f"/api/cover/{post['id']}?v={version}{self._src()}" if cover else "",
            "saved_state": saved_state,
            "status": post.get("status") or "",
            "comments_count": int(post.get("comments_total") or 0),
            "has_comments": bool(post.get("comments_total")),
            "confidence": None,
            "source_label": self.platform,
            "is_ad": False,
        }

    # ---------------- listing / facets ----------------
    def list_posts(self, *, offset: int, limit: int, q: str, category: str,
                   tag: str, sort: str, owner: str = "", flags: str = "",
                   categories: str = "") -> Tuple[List[Dict[str, Any]], int]:
        data = self.scan()
        flags_set = {f.strip() for f in (flags or "").split(",") if f.strip()}
        only = [c.strip() for c in (categories or "").split(",") if c.strip()]
        needle = (q or "").strip().lower()

        def matches(post: Dict[str, Any]) -> bool:
            text = " ".join([
                post.get("text") or "", post.get("author") or "",
                post.get("author_name") or "", post.get("code") or "",
                post.get("notes") or "", " ".join(post.get("tags") or []),
            ]).lower()
            if needle and needle not in text:
                return False
            if category and category not in ("all", "") and category not in post["categories"]:
                return False
            if tag and tag not in (post.get("tags") or []):
                return False
            if owner and post.get("author") != owner:
                return False
            if only and not any(c in post["categories"] for c in only):
                return False
            media = self._post_media(post)
            if "no_video" in flags_set and any(m["kind"] == "video" for m in media):
                return False
            if "no_image" in flags_set and any(m["kind"] == "image" for m in media):
                return False
            if "no_ads" in flags_set and False:
                pass
            if "bad_owner" in flags_set and post.get("author"):
                return False
            if "bad_cover" in flags_set and self._post_media(post):
                return False
            if "caption_empty" in flags_set and (post.get("text") or "").strip():
                return False
            if "caption_error" in flags_set:
                return False
            if "bad_shortcode" in flags_set and post.get("code"):
                return False
            if "comments_empty" in flags_set and post.get("comments_total"):
                return False
            if "comments_error" in flags_set:
                return False
            if "still_saved" in flags_set:
                if self.platform not in ("twitter", "x"):
                    return False
                if post.get("unsave_status") == "done":
                    return False
            if "already_unsaved" in flags_set:
                if self.platform not in ("twitter", "x"):
                    return False
                if post.get("unsave_status") != "done":
                    return False
            if "uncategorised" in flags_set and post["categories"]:
                return False
            return True

        posts = [p for p in data["posts"] if matches(p)]
        if sort == "newest":
            posts.sort(key=lambda p: (p.get("taken_at") or 0, p["id"]), reverse=True)
        elif sort == "oldest":
            posts.sort(key=lambda p: (p.get("taken_at") or 0, p["id"]))
        elif sort == "likes":
            posts.sort(key=lambda p: (p.get("like_count") or 0, p["id"]), reverse=True)
        elif sort == "random":
            import random
            random.shuffle(posts)
        elif sort == "uncategorised":
            posts.sort(key=lambda p: (1 if p["categories"] else 0,
                                      p.get("sort_order", 0)))
        else:
            posts.sort(key=lambda p: (p.get("sort_order", 0), p["id"]))
        total = len(posts)
        return [self._item(p) for p in posts[offset:offset + limit]], total

    def category_list(self) -> List[Dict[str, Any]]:
        data = self.scan()
        return [{"name": c, "count": int(data["counts"].get(c, 0))}
                for c in data["categories"]]

    def facets(self) -> Dict[str, Any]:
        data = self.scan()
        posts = data["posts"]
        media_items = [(p, self._post_media(p)) for p in posts]
        if self.platform in ("twitter", "x"):
            unsaved = sum(1 for p in posts if p.get("unsave_status") == "done")
            still = len(posts) - unsaved
        else:
            unsaved = 0
            still = 0
        counts = {
            "no_video": sum(1 for _, m in media_items
                            if not any(x["kind"] == "video" for x in m)),
            "no_image": sum(1 for _, m in media_items
                            if not any(x["kind"] == "image" for x in m)),
            "no_ads": len(posts),
            "bad_owner": sum(1 for p in posts if not p.get("author")),
            "bad_cover": sum(1 for p in posts if not self._post_media(p)),
            "caption_empty": sum(1 for p in posts if not (p.get("text") or "").strip()),
            "caption_error": 0,
            "bad_shortcode": sum(1 for p in posts if not p.get("code")),
            "comments_empty": sum(1 for p in posts if not p.get("comments_total")),
            "comments_error": 0,
            "still_saved": still,
            "already_unsaved": unsaved,
            "uncategorised": data["uncategorised"],
        }
        tag_counts: Dict[str, int] = {}
        for p in posts:
            for tag in p.get("tags") or []:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        tags = sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:200]
        comments_total = sum(int(p.get("comments_total") or 0) for p in posts)
        files = sum(len(m) for _, m in media_items)
        cats = self.category_list()
        return {
            "total": len(posts),
            "videos": sum(1 for _, m in media_items
                          if any(x["kind"] == "video" for x in m)),
            "ads": 0,
            "filter_counts": counts,
            "categories": cats,
            "category_list": cats,
            "tags": [{"name": n, "count": c} for n, c in tags],
            "presets": [c["name"] for c in cats],
            "saved": {"still_saved": still, "already_unsaved": unsaved},
            "stats": {"comments": comments_total, "media_files": files},
        }

    def category_op(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        meta = self.meta()
        op = str(payload.get("op") or "")
        name = str(payload.get("name") or "").strip()
        if op == "add" and name and name not in meta["categories"]:
            meta["categories"].append(name)
        elif op == "rename" and name:
            new = str(payload.get("new_name") or "").strip()
            if new and new != name and name in meta["categories"]:
                meta["categories"] = [new if c == name else c for c in meta["categories"]]
                meta["assignments"] = {
                    pk: [new if c == name else c for c in cats]
                    for pk, cats in meta["assignments"].items()
                }
        elif op == "remove" and name:
            meta["categories"] = [c for c in meta["categories"] if c != name]
            meta["assignments"] = {
                pk: [c for c in cats if c != name]
                for pk, cats in meta["assignments"].items()
            }
            meta["assignments"] = {pk: c for pk, c in meta["assignments"].items() if c}
        elif op == "reorder":
            names = [str(n) for n in (payload.get("names") or [])
                     if str(n) in meta["categories"]]
            meta["categories"] = names + [c for c in meta["categories"] if c not in names]
        self.save_meta(meta)
        return {"categories": self.category_list(),
                "uncategorised": self.scan(force=True)["uncategorised"]}

    # ---------------- detail / comments ----------------
    def _find(self, post_id: int) -> Optional[Dict[str, Any]]:
        return self.scan()["by_id"].get(int(post_id))

    def comments(self, post_id: int, offset: int = 0, limit: int = 30) -> Dict[str, Any]:
        post = self._find(post_id)
        if not post:
            return {"total": 0, "items": [], "hasMore": False, "offset": offset}
        try:
            conn = self.connect()
            comment_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(comments)")}
            if "is_reply" in comment_cols:
                roots = conn.execute(
                    "SELECT * FROM comments WHERE post_pk=? AND is_reply=0 "
                    "ORDER BY rank ASC, position ASC", (post["pk"],)).fetchall()
                replies = conn.execute(
                    "SELECT * FROM comments WHERE post_pk=? AND is_reply=1 "
                    "ORDER BY taken_at ASC", (post["pk"],)).fetchall()
            else:
                roots = conn.execute(
                    "SELECT * FROM comments WHERE post_pk=? "
                    "ORDER BY rank ASC, position ASC", (post["pk"],)).fetchall()
                replies = []
            media_rows = conn.execute(
                "SELECT id,comment_id,type,local_path,status FROM media "
                "WHERE post_pk=? AND scope!='post' AND status='done' ORDER BY id",
                (post["pk"],)).fetchall()
            conn.close()
        except Exception:
            roots, replies, media_rows = [], [], []
        by_parent: dict[str, List[Any]] = {}
        for reply in replies:
            by_parent.setdefault(str(reply["parent_id"] or ""), []).append(reply)
        media_by_comment: Dict[str, List[Dict[str, Any]]] = {}
        for row in media_rows:
            key = str(row["comment_id"] or "")
            bucket = media_by_comment.setdefault(key, [])
            index = len(bucket)
            bucket.append({
                "i": index,
                "kind": "video" if str(row["type"]) == "video" else "image",
                "url": (f"/api/comment_media/{post['id']}/{urllib.parse.quote(key)}"
                        f"/{index}"
                        + (f"?source={urllib.parse.quote(self.source_id)}"
                           if self.source_id else "")),
            })

        def reply_item(row) -> Dict[str, Any]:
            return {
                "owner": row["author_name"] or row["author"] or "",
                "text": row["text"] or "",
                "created_at": (_iso(row["taken_at"]) if "taken_at" in row.keys() else "")
                              or row["created_at"] or "",
                "likes": int(row["like_count"] or 0),
                "media": media_by_comment.get(str(row["id"]), []),
            }

        page = []
        for root in roots[offset:offset + limit]:
            subs = [reply_item(r) for r in by_parent.get(str(root["id"]), [])[:8]]
            page.append({
                "id": root["id"],
                "owner": root["author_name"] or root["author"] or "",
                "text": root["text"] or "",
                "created_at": (_iso(root["taken_at"]) if "taken_at" in root.keys() else "")
                              or root["created_at"] or "",
                "likes": int(root["like_count"] or 0),
                "media": media_by_comment.get(str(root["id"]), []),
                "replies": subs,
                "reply_total": int(root["reply_count"] or len(subs)),
            })
        return {
            "total": len(roots) + len(replies),
            "rootCount": len(roots),
            "offset": offset,
            "limit": limit,
            "hasMore": offset + limit < len(roots),
            "items": page,
        }

    def get_post(self, post_id: int, with_comments: bool = True,
                 comments_offset: int = 0, comments_limit: int = 20) -> Optional[Dict[str, Any]]:
        post = self._find(post_id)
        if not post:
            return None
        out = self._item(post)
        out["caption"] = post.get("text") or ""
        out["media_id"] = post["pk"]
        out["folder"] = post["pk"]
        out["comments_data"] = self.comments(
            post_id, comments_offset, comments_limit) if with_comments else None
        return out

    # ---------------- edits ----------------
    def update_post(self, post_id: int, payload: Dict[str, Any]) -> bool:
        post = self._find(post_id)
        if not post:
            return False
        meta = self.meta()
        pk = post["pk"]
        if "categories" in payload:
            values = payload.get("categories") or []
            if isinstance(values, str):
                values = [v.strip() for v in values.split(",")]
            values = [str(v).strip() for v in values if str(v).strip()]
            for value in values:
                if value not in meta["categories"]:
                    meta["categories"].append(value)
            if values:
                meta["assignments"][pk] = sorted(set(values))
            else:
                meta["assignments"].pop(pk, None)
        elif "category" in payload:
            value = str(payload.get("category") or "").strip()
            if value:
                if value not in meta["categories"]:
                    meta["categories"].append(value)
                current = set(meta["assignments"].get(pk) or [])
                current.add(value)
                meta["assignments"][pk] = sorted(current)
            else:
                meta["assignments"].pop(pk, None)
        if "notes" in payload:
            meta["notes"][pk] = str(payload.get("notes") or "")
        if "tags" in payload:
            tags = payload.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
            meta["tags"][pk] = sorted({
                str(t).strip().lstrip("#") for t in tags if str(t).strip()})
        self.save_meta(meta)
        return True

    def bulk_category(self, ids: List[int], category: str) -> int:
        category = (category or "").strip()
        if not category:
            return 0
        data = self.scan()
        meta = self.meta()
        if category not in meta["categories"]:
            meta["categories"].append(category)
        count = 0
        for post_id in ids:
            post = data["by_id"].get(int(post_id))
            if not post:
                continue
            current = set(meta["assignments"].get(post["pk"]) or [])
            current.add(category)
            meta["assignments"][post["pk"]] = sorted(current)
            count += 1
        self.save_meta(meta)
        return count

    def reorder(self, post_id: int, before_id: Optional[int],
                after_id: Optional[int]) -> bool:
        data = self.scan()
        post = data["by_id"].get(int(post_id))
        if not post:
            return False
        meta = self.meta()
        order = [pk for pk in (meta["manual_order"] or data["order"])
                 if pk in data["by_pk"] and pk != post["pk"]]
        target = None
        if before_id and int(before_id) in data["by_id"]:
            target = data["by_id"][int(before_id)]["pk"]
            order.insert(order.index(target) if target in order else len(order), post["pk"])
        elif after_id and int(after_id) in data["by_id"]:
            target = data["by_id"][int(after_id)]["pk"]
            order.insert(order.index(target) + 1 if target in order else len(order), post["pk"])
        else:
            order.append(post["pk"])
        meta["manual_order"] = order
        self.save_meta(meta)
        return True

    # ---------------- files / thumbs ----------------
    def _media_list(self, post_id: int) -> List[Dict[str, Any]]:
        post = self._find(post_id)
        return self._post_media(post) if post else []

    def media_file(self, post_id: int, index: int) -> Optional[Path]:
        post = self._find(post_id)
        if not post:
            return None
        shown = self._post_media(post)
        if 0 <= index < len(shown):
            path = self.media_root / shown[index]["path"]
            return path if path.is_file() else None
        return None

    def comment_media_file(self, post_id: int, comment_id: str,
                           index: int) -> Optional[Path]:
        post = self._find(post_id)
        if not post:
            return None
        try:
            conn = self.connect()
            rows = conn.execute(
                "SELECT local_path FROM media WHERE post_pk=? AND comment_id=? "
                "AND scope!='post' AND status='done' ORDER BY id",
                (post["pk"], urllib.parse.unquote(str(comment_id)))).fetchall()
            conn.close()
        except Exception:
            rows = []
        if 0 <= index < len(rows):
            path = self.media_root / rows[index]["local_path"]
            return path if path.is_file() else None
        return None

    def _cover_path(self, post_id: int) -> Optional[Path]:
        post = self._find(post_id)
        return self._cover_file(post) if post else None

    def _placeholder(self, path: Path, width: int) -> None:
        img = Image.new("RGB", (width, width), (20, 24, 34))
        draw = ImageDraw.Draw(img)
        draw.text((width * 0.42, width * 0.47), "TH", fill=(140, 150, 175))
        img.save(path, "JPEG", quality=80)

    def _video_frame(self, video: Path) -> Optional[Path]:
        if imageio_ffmpeg is None:
            return None
        out = self.cache_dir / f"frame_{hashlib.md5(str(video).encode()).hexdigest()}.jpg"
        if out.is_file():
            return out
        try:
            import subprocess
            subprocess.run(
                [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
                 "-ss", "1.0", "-i", str(video), "-frames:v", "1",
                 "-vf", "scale=480:-1", str(out)],
                check=False, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=45)
        except Exception:
            return None
        return out if out.is_file() else None

    def thumbnail(self, post_id: int, width: int = 360,
                  index: int = 0) -> Optional[Path]:
        cache = self.cache_dir / f"{post_id}_{index}_{width}.jpg"
        post = self._find(post_id)
        if not post:
            return None
        shown = self._post_media(post)
        source: Optional[Path] = None
        kind = "image"
        if 0 <= index < len(shown):
            kind = shown[index]["kind"]
            source = self.media_file(post_id, index)
            if source and kind == "video":
                frame = self._video_frame(source)
                source = frame or source
        if source is None and index == 0:
            source = self._cover_file(post)
        if source is None:
            self._placeholder(cache, width)
            return cache
        try:
            if cache.is_file() and cache.stat().st_mtime >= source.stat().st_mtime:
                return cache
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
                img.save(cache, "JPEG", quality=82)
            return cache
        except Exception:
            self._placeholder(cache, width)
            return cache

    def quick_stats(self) -> Dict[str, int]:
        try:
            conn = self.connect()
            total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            videos = conn.execute(
                "SELECT COUNT(DISTINCT post_pk) FROM media "
                "WHERE scope='post' AND type='video' AND status='done'"
            ).fetchone()[0]
            conn.close()
            return {"total": int(total), "videos": int(videos)}
        except Exception:
            data = self.scan()
            return {
                "total": len(data["posts"]),
                "videos": sum(1 for p in data["posts"]
                              if any(m["kind"] == "video" for m in self._post_media(p))),
            }
