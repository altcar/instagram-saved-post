#!/usr/bin/env python3
"""Merge leftover files from old saved_media folders, then empty the sources.

Duplicates that were skipped by the first import often still have a .txt or a
different media variant. This merges anything missing into data/posts/<id>,
then removes the now-empty source folder. Safe to re-run.

    python cleanup_sources.py --dry-run
    python cleanup_sources.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Dict, Optional

import archive_db as adb


def move_file(src: Path, dst: Path) -> bool:
    if dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError:
        shutil.move(str(src), str(dst))
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean old saved_media sources")
    parser.add_argument("--data", default="data")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_root = Path(args.data)
    posts_root = Path(args.posts)
    conn = adb.connect(Path(args.db))
    conn.row_factory = sqlite3.Row
    by_code = {
        row["shortcode"]: row
        for row in conn.execute(
            "SELECT * FROM posts WHERE folder IS NOT NULL"
        )
    }

    merged_files = 0
    merged_folders = 0
    leftovers = 0

    def process(folder: Path, move: bool) -> None:
        nonlocal merged_files, merged_folders, leftovers
        if not folder.is_dir():
            return
        code = folder.name.split("_", 1)[1] if "_" in folder.name else folder.name
        row = by_code.get(code)
        if row is None:
            leftovers += 1
            return
        dest = posts_root / row["folder"]
        added = 0
        for item in list(folder.iterdir()):
            if item.is_dir():
                continue
            if item.suffix.lower() == ".txt":
                # keep the Instaloader caption as caption.txt if we have none
                cap = dest / "caption.txt"
                if not cap.exists() or cap.stat().st_size == 0:
                    if not args.dry_run:
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(item, cap)
                if not args.dry_run and move:
                    item.unlink()
                continue
            if item.suffix.lower() not in adb.MEDIA_EXTS:
                continue
            target = dest / item.name
            if target.exists():
                if not args.dry_run and move:
                    item.unlink()
                continue
            if args.dry_run:
                added += 1
                continue
            dest.mkdir(parents=True, exist_ok=True)
            if move:
                if move_file(item, target):
                    added += 1
            else:
                if not target.exists():
                    shutil.copy2(item, target)
                    added += 1
        if added and not args.dry_run:
            adb.upsert_post(
                conn,
                media_id=row["media_id"],
                shortcode=row["shortcode"],
                media_root=posts_root,
                taken_at=row["taken_at"],
                owner_username=row["owner_username"],
                owner_id=row["owner_id"],
                caption=row["caption"] or "",
                likes=row["likes"] or 0,
                is_video=bool(row["is_video"]),
                typename=row["typename"],
                location=row["location"],
                hashtags=json.loads(row["hashtags"] or "[]"),
                folder=row["folder"],
                cover=adb.find_cover(dest),
                source=row["source"] or "run",
            )
            conn.commit()
        merged_files += added
        merged_folders += 1
        if not args.dry_run and move:
            for child in list(folder.iterdir()):
                if child.is_dir() and not any(child.iterdir()):
                    child.rmdir()
            try:
                folder.rmdir()
            except OSError:
                pass

    for run_dir in sorted(data_root.glob("*/saved_media")):
        for folder in sorted(run_dir.iterdir()):
            if folder.is_dir():
                process(folder, move=True)

    # Expansion copies (read-only source; merge, do not delete the mount)
    for extra in (
        "/run/media/over/Expansion/save/XXXmovie/save/devdrive/instagram save/data/3001/saved_media",
        "/run/media/over/Expansion/save/XXXmovie/save/devdrive/instagram save/data/3101/saved_media",
    ):
        path = Path(extra)
        if not path.is_dir():
            continue
        for folder in sorted(path.iterdir()):
            if folder.is_dir():
                process(folder, move=False)

    conn.close()
    print(
        f"folders merged: {merged_folders}, files added: {merged_files}, "
        f"unknown leftovers: {leftovers}"
    )


if __name__ == "__main__":
    main()
