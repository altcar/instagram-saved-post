#!/usr/bin/env python3
"""Merge per-folder meta.json records into saved_posts.json.

Useful after a resumed download is interrupted before it rewrites the run JSON:
every downloaded post already has meta.json, so nothing is lost.

    python merge_meta.py --json data/saved_posts.json \
        --media data/posts
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge folder metadata into run JSON")
    parser.add_argument("--json", default="data/saved_posts.json")
    parser.add_argument("--media", default="data/posts")
    args = parser.parse_args()

    json_path = Path(args.json)
    media_root = Path(args.media)
    records = {}
    if json_path.exists():
        for rec in json.loads(json_path.read_text(encoding="utf-8")):
            media_id = rec.get("media_id")
            if media_id is not None:
                records[str(media_id)] = rec

    added = 0
    for meta_path in sorted(media_root.glob("*/meta.json")):
        try:
            rec = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        media_id = rec.get("media_id")
        if media_id is None:
            continue
        key = str(media_id)
        if key not in records:
            rec["folder"] = meta_path.parent.name
            records[key] = rec
            added += 1

    json_path.write_text(
        json.dumps(list(records.values()), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"merged {added} new records; {len(records)} total -> {json_path}")


if __name__ == "__main__":
    main()
