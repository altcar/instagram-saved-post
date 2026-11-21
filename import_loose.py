#!/usr/bin/env python3
"""Import old loose saved_media folders and ad files into data/posts.

Patterns handled:
  * <run>/saved_media/NNNN_<shortcode>/          (old numbered runs)
  * data/download/<shortcode> <caption>.jpg      (loose ads)
  * data/download/<hash>.mp4                     (unnamed ad media)
  * data/MyInsta/<owner>/*.mp4                   (owner folders)

Duplicate shortcodes already present in data/archive.db are skipped.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import archive_db as adb

CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,}$")
PREFIX_RE = re.compile(r"^\d{4}_(.+)$")


def parse_myinsta(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    decoder = json.JSONDecoder()
    items = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n,[]":
            index += 1
        if index >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, index)
        except ValueError:
            break
        if isinstance(obj, dict):
            items.append(obj)
        index = end
    return items


def load_media_map(data_root: Path) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for path in data_root.rglob("saved_posts.json*"):
        try:
            if path.name == "saved_posts.json" and path.parent.name == "MyInsta":
                records = parse_myinsta(path)
            else:
                records = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(records, list):
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            code = rec.get("shortcode")
            media_id = rec.get("media_id")
            if code and media_id is not None and str(media_id).isdigit():
                mapping[code] = str(media_id)
    return mapping


def merge_move(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if target.exists():
            continue
        try:
            item.rename(target)
        except OSError:
            shutil.move(str(item), str(target))
    try:
        src.rmdir()
    except OSError:
        pass


def media_files(folder: Path) -> List[Path]:
    return [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in adb.MEDIA_EXTS
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import loose archive sources")
    parser.add_argument("--data", default="data")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_root = Path(args.data)
    posts_root = Path(args.posts)
    posts_root.mkdir(parents=True, exist_ok=True)
    media_map = load_media_map(data_root)
    conn = adb.connect(Path(args.db))
    existing = {
        row[0] for row in conn.execute("SELECT shortcode FROM posts")
    }

    imported = 0
    duplicates = 0
    skipped = 0

    # 1. old numbered runs
    for run_dir in sorted(data_root.glob("*/saved_media")):
        if run_dir.parent.name in ("1911",):
            continue
        for folder in sorted(run_dir.iterdir()):
            if not folder.is_dir():
                continue
            match = PREFIX_RE.match(folder.name)
            code = match.group(1) if match else folder.name
            if code in existing:
                duplicates += 1
                continue
            files = media_files(folder)
            if not files:
                skipped += 1
                continue
            dest_name = media_map.get(code, code)
            dest = posts_root / dest_name
            print(f"{run_dir.parent.name}/{folder.name} -> posts/{dest_name}")
            if args.dry_run:
                imported += 1
                continue
            merge_move(folder, dest)
            caption = ""
            for txt in sorted(dest.glob("*.txt")):
                caption = txt.read_text(encoding="utf-8", errors="replace").strip()
                break
            is_video = any(p.suffix.lower() in adb.VIDEO_EXTS for p in media_files(dest))
            adb.upsert_post(
                conn,
                media_id=media_map.get(code),
                shortcode=code,
                media_root=posts_root,
                caption=caption,
                is_video=is_video,
                folder=dest_name,
                cover=adb.find_cover(dest),
                source="run",
            )
            existing.add(code)
            imported += 1
            if imported % 100 == 0:
                conn.commit()
                print(f"  {imported} imported", flush=True)

    # 2. data/download loose ads
    download = data_root / "download"
    if download.is_dir():
        for index, path in enumerate(sorted(download.iterdir())):
            if not path.is_file():
                continue
            stem = path.stem
            token = stem.split(" ", 1)[0]
            if CODE_RE.match(token):
                code = token
                caption = stem[len(token):].lstrip(" •-")
            elif "_" in stem and CODE_RE.match(stem.split("_", 1)[0]):
                code = stem.split("_", 1)[0]
                caption = stem
            else:
                code = f"misc_{stem[:32]}"
                caption = stem
            dest = posts_root / code
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / path.name
            if target.exists():
                duplicates += 1
                continue
            print(f"download/{path.name} -> posts/{code}/")
            if args.dry_run:
                imported += 1
                continue
            shutil.move(str(path), str(target))
            if code not in existing:
                adb.upsert_post(
                    conn,
                    media_id=None,
                    shortcode=code,
                    media_root=posts_root,
                    caption=caption,
                    is_video=path.suffix.lower() in adb.VIDEO_EXTS,
                    folder=code,
                    cover=adb.find_cover(dest),
                    source="loose",
                )
                existing.add(code)
            imported += 1

    # 3. MyInsta owner folders
    myinsta = data_root / "MyInsta"
    if myinsta.is_dir():
        for owner_dir in sorted(myinsta.iterdir()):
            if not owner_dir.is_dir():
                continue
            for path in sorted(owner_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in adb.MEDIA_EXTS:
                    continue
                code = f"{owner_dir.name}_{path.stem[:40]}"
                dest = posts_root / code
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / path.name
                if target.exists():
                    duplicates += 1
                    continue
                print(f"MyInsta/{owner_dir.name}/{path.name} -> posts/{code}/")
                if args.dry_run:
                    imported += 1
                    continue
                shutil.move(str(path), str(target))
                adb.upsert_post(
                    conn,
                    media_id=None,
                    shortcode=code,
                    media_root=posts_root,
                    caption=f"@{owner_dir.name}",
                    owner_username=owner_dir.name,
                    is_video=path.suffix.lower() in adb.VIDEO_EXTS,
                    folder=code,
                    cover=adb.find_cover(dest),
                    source="myinsta",
                )
                imported += 1

    conn.commit()
    conn.close()
    print(
        f"imported {imported}, duplicates {duplicates}, empty/non-media {skipped}"
    )


if __name__ == "__main__":
    main()
