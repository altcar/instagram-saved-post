#!/usr/bin/env python3
"""Move captions/comments from old saved_posts JSON files into data/posts.

Old runs kept comments only in saved_posts.json (or .tmp). This writes them into
the portable per-post folders before the old source directories are deleted.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from import_loose import parse_myinsta


def records_from(path: Path) -> List[Dict[str, Any]]:
    try:
        if path.parent.name == "MyInsta" and path.name == "saved_posts.json":
            return parse_myinsta(path)
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def comments_text(comments: List[Dict[str, Any]]) -> str:
    lines = []
    for comment in comments:
        owner = comment.get("owner") or "?"
        created = comment.get("created_at") or ""
        text = (comment.get("text") or "").replace("\n", " ")
        likes = comment.get("likes")
        suffix = f"  [{likes} likes]" if likes else ""
        lines.append(f"{owner} {created}: {text}{suffix}")
    return "\n".join(lines) + ("\n" if lines else "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate old comments/captions")
    parser.add_argument("--data", default="data")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--db", default="data/archive.db")
    args = parser.parse_args()

    data_root = Path(args.data)
    posts_root = Path(args.posts)
    conn = sqlite3.connect(args.db, timeout=60)
    folders = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT shortcode, folder FROM posts WHERE folder IS NOT NULL"
        )
    }
    conn.close()

    records: List[Dict[str, Any]] = []
    for path in data_root.glob("*/saved_posts.json*"):
        records.extend(records_from(path))
    records.extend(records_from(data_root / "saved_posts.json"))

    migrated = 0
    captions = 0
    for rec in records:
        code = rec.get("shortcode")
        if not code:
            continue
        folder_name = folders.get(code)
        if not folder_name:
            matches = list(posts_root.glob(f"*_{code}")) or list(posts_root.glob(code))
            if matches:
                folder_name = matches[0].name
        if not folder_name:
            continue
        folder = posts_root / folder_name
        if not folder.is_dir():
            continue

        comments = rec.get("comments") or []
        if isinstance(comments, str):
            try:
                comments = json.loads(comments)
            except Exception:
                comments = []
        if comments:
            cfile = folder / "comments.json"
            existing = []
            if cfile.is_file():
                try:
                    existing = json.loads(cfile.read_text(encoding="utf-8"))
                except Exception:
                    existing = []
            if not existing:
                cfile.write_text(
                    json.dumps(comments, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                (folder / "comments.txt").write_text(
                    comments_text(comments), encoding="utf-8"
                )
                migrated += 1

        caption = rec.get("caption") or ""
        capfile = folder / "caption.txt"
        if caption and (not capfile.is_file() or capfile.stat().st_size == 0):
            capfile.write_text(caption + "\n", encoding="utf-8")
            captions += 1

    print(f"migrated comments for {migrated} folders; captions for {captions}")


if __name__ == "__main__":
    main()
