#!/usr/bin/env python3
"""Move a run's post folders into the unified data/posts/<post_id> tree.

Instagram media id is the folder name (falls back to shortcode when unknown).
The SQLite index is rebased and moved to data/archive.db.

    python migrate_to_posts.py --db data/1911/archive.db \
        --media data/1911/saved_media --dest data/posts \
        --new-db data/archive.db
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path


def merge_move(src: Path, dst: Path) -> None:
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(src, dst)
            return
        except OSError:
            shutil.move(str(src), str(dst))
            return
    for item in src.iterdir():
        target = dst / item.name
        if target.exists():
            continue
        try:
            os.rename(item, target)
        except OSError:
            shutil.move(str(item), str(target))
    try:
        src.rmdir()
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Unify post folders by post id")
    parser.add_argument("--db", default="data/1911/archive.db")
    parser.add_argument("--media", default="data/1911/saved_media")
    parser.add_argument("--dest", default="data/posts")
    parser.add_argument("--new-db", default="data/archive.db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--move-db", action="store_true", default=True)
    parser.add_argument("--no-move-db", dest="move_db", action="store_false")
    args = parser.parse_args()

    db_path = Path(args.db)
    media = Path(args.media)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, media_id, shortcode, folder, cover FROM posts "
        "WHERE folder IS NOT NULL ORDER BY sort_order"
    ).fetchall()

    moved = 0
    skipped = 0
    for row in rows:
        old_folder = row["folder"]
        src = media / old_folder
        if not src.is_dir():
            skipped += 1
            continue
        media_key = str(row["media_id"] or "")
        name = media_key if media_key.isdigit() else row["shortcode"]
        target = dest / name
        if args.dry_run:
            print(f"{old_folder} -> posts/{name}")
            moved += 1
            continue
        merge_move(src, target)

        cover = None
        if row["cover"]:
            cover = f"{name}/{Path(row['cover']).name}"
        conn.execute(
            "UPDATE posts SET folder = ?, cover = ? WHERE id = ?",
            (name, cover, row["id"]),
        )
        for media_row in conn.execute(
            "SELECT path FROM post_media WHERE media_id = ?", (row["media_id"],)
        ).fetchall():
            new_path = f"{name}/{Path(media_row['path']).name}"
            conn.execute(
                "UPDATE post_media SET path = ? WHERE media_id = ? AND path = ?",
                (new_path, row["media_id"], media_row["path"]),
            )
        moved += 1
        if moved % 500 == 0:
            conn.commit()
            print(f"  {moved} folders moved", flush=True)

    conn.commit()
    top = conn.execute(
        "SELECT folder FROM posts ORDER BY sort_order LIMIT 3"
    ).fetchall()
    conn.close()

    if not args.dry_run and args.move_db:
        new_db = Path(args.new_db)
        if new_db.exists():
            print(f"Refusing to overwrite existing {new_db}", file=sys.stderr)
            sys.exit(1)
        new_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(db_path), str(new_db))
        print(f"index moved to {new_db}")

    for folder in sorted(media.glob("*")):
        if folder.is_dir() and not any(folder.iterdir()):
            try:
                folder.rmdir()
            except OSError:
                pass

    print(f"moved {moved} folders, skipped {skipped}; examples: {[r[0] for r in top]}")


if __name__ == "__main__":
    main()
