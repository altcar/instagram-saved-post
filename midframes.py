#!/usr/bin/env python3
"""Precompute midpoint frames for every video (parallel).

The vision stage then reads a cached JPEG instead of running ffmpeg inside the
CLIP loop. Midpoint is duration/2 (e.g. 30 s for a one-minute video).

    python midframes.py --db data/archive.db --media data/posts \
        --cache data/.cache --workers 8
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple

import imageio_ffmpeg

from classify import video_duration


def main() -> None:
    parser = argparse.ArgumentParser(description="Precompute video midpoint frames")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--media", default="data/posts")
    parser.add_argument("--cache", default="data/.cache")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    media_root = Path(args.media)
    out_dir = Path(args.cache) / "mid"
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    conn = sqlite3.connect(args.db, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT p.id, m.path FROM posts p
           JOIN post_media m ON m.media_id = p.media_id
           WHERE p.is_video = 1 AND m.kind = 'video'
           ORDER BY p.sort_order"""
    ).fetchall()
    conn.close()

    tasks: List[Tuple[int, Path, Path]] = []
    seen = set()
    for row in rows:
        post_id = row["id"]
        if post_id in seen:
            continue
        seen.add(post_id)
        target = out_dir / f"{post_id}.jpg"
        if target.is_file() and target.stat().st_size > 0 and not args.overwrite:
            continue
        video = media_root / row["path"]
        if video.is_file():
            tasks.append((post_id, video, target))
    if args.limit:
        tasks = tasks[: args.limit]

    print(f"{len(tasks)} midpoint frames to extract ({len(seen)} videos total)")
    lock = threading.Lock()
    done = 0
    failed = 0

    def work(task: Tuple[int, Path, Path]) -> bool:
        post_id, video, target = task
        duration = video_duration(video)
        midpoint = duration / 2 if duration else 1.0
        try:
            subprocess.run(
                [
                    ffmpeg, "-y", "-loglevel", "error",
                    "-ss", f"{midpoint:.3f}", "-i", str(video),
                    "-frames:v", "1", "-vf", f"scale={args.width}:-1", str(target),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
        except Exception:
            return False
        return target.is_file() and target.stat().st_size > 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, task) for task in tasks]
        for future in as_completed(futures):
            with lock:
                done += 1
                if not future.result():
                    failed += 1
                if done % 250 == 0:
                    print(f"  {done}/{len(tasks)} (failed {failed})", flush=True)
    print(f"done: {done - failed} frames, {failed} failed")


if __name__ == "__main__":
    main()
