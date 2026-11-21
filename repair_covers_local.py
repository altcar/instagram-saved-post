#!/usr/bin/env python3
"""Create missing cover.jpg files from local media, without Instagram calls.

For image posts the first image is copied. For videos the frame at the midpoint
is extracted, which is usually much more useful than a black first frame.

    python repair_covers_local.py --dry-run
    python repair_covers_local.py --workers 4
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover
    imageio_ffmpeg = None

import archive_db as adb

DB_LOCK = threading.Lock()


def duration(video: Path) -> Optional[float]:
    if imageio_ffmpeg is None:
        return None
    try:
        proc = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(video)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def make_cover(folder: Path, media: list[Tuple[str, str]]) -> Optional[Path]:
    cover = folder / "cover.jpg"
    if cover.is_file() and cover.stat().st_size > 1024:
        return cover
    for rel, kind in media:
        path = folder / Path(rel).name
        if not path.is_file() or path.stat().st_size <= 1024:
            continue
        if kind == "image":
            try:
                shutil.copyfile(path, cover)
                return cover if cover.stat().st_size > 1024 else None
            except OSError:
                continue
        if kind == "video" and imageio_ffmpeg is not None:
            total = duration(path)
            at = (total / 2) if total else 0.5
            try:
                subprocess.run(
                    [
                        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
                        "-ss", f"{at:.3f}", "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=720:-1", str(cover),
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                )
            except Exception:
                continue
            if cover.is_file() and cover.stat().st_size > 1024:
                return cover
            # Some very short clips have no frame at the exact midpoint.
            try:
                subprocess.run(
                    [
                        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
                        "-ss", "0.5", "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=720:-1", str(cover),
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                )
            except Exception:
                pass
            if cover.is_file() and cover.stat().st_size > 1024:
                return cover
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build covers from local media")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--posts", default="data/posts")
    parser.add_argument("--cache", default="data/.cache")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    media_root = Path(args.posts)
    db = adb.connect(Path(args.db))
    db.row_factory = sqlite3.Row
    media_by_id: Dict[str, list] = {}
    for row in db.execute("SELECT media_id, path, kind FROM post_media ORDER BY path"):
        media_by_id.setdefault(row["media_id"], []).append(
            (row["path"], row["kind"])
        )
    tasks = []
    for row in db.execute("SELECT * FROM posts ORDER BY sort_order, id"):
        cover = media_root / row["cover"] if row["cover"] else None
        if cover is not None and cover.is_file() and cover.stat().st_size > 1024:
            continue
        if not row["folder"]:
            continue
        folder = media_root / row["folder"]
        media = media_by_id.get(row["media_id"], [])
        if not media:
            continue
        tasks.append((row, folder, media))
    if args.limit:
        tasks = tasks[: args.limit]
    print(f"{len(tasks)} covers to build")
    if args.dry_run or not tasks:
        db.close()
        return

    def work(task: Tuple[sqlite3.Row, Path, list]) -> Tuple[sqlite3.Row, Optional[Path]]:
        row, folder, media = task
        return row, make_cover(folder, media)

    done = 0
    ok = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(work, task) for task in tasks]
        for future in as_completed(futures):
            row, cover = future.result()
            done += 1
            if cover is not None:
                ok += 1
                with DB_LOCK:
                    db.execute(
                        "UPDATE posts SET cover=? WHERE id=?",
                        (f"{cover.parent.name}/cover.jpg", row["id"]),
                    )
                    db.commit()
                for thumb in (Path(args.cache) / "thumbs").glob(f"{row['id']}_*.jpg"):
                    try:
                        thumb.unlink()
                    except OSError:
                        pass
            if done % 100 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} built={ok}", flush=True)
    db.close()
    print(f"done: {ok}/{len(tasks)} covers built")


if __name__ == "__main__":
    main()
