#!/usr/bin/env python3
"""Convert cloud_state.json into the static gallery override format.

The static GitHub Pages gallery keys personal edits by the numeric gallery id,
while cloud_state.json keys them by media_id/pk. This maps them through the
unified manifest so the same categories, notes and tags appear on Pages.

    python publish/export_static_overrides.py
    python publish/export_static_overrides.py --state /tmp/cloud_state.json \
        --out publish/static_overrides.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "publish/site-unified/gallery/manifest.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(MANIFEST))
    parser.add_argument("--state", default=str(ROOT / "data/cloud_state.json"))
    parser.add_argument("--out", default=str(ROOT / "publish/static_overrides.json"))
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    by_key: dict[str, dict] = {}
    for post in manifest.get("posts", []):
        for key in (post.get("folder"), post.get("pk"), str(post.get("id"))):
            if key:
                by_key[str(key)] = post

    state_path = Path(args.state)
    if not state_path.is_file():
        print(f"missing state: {state_path}")
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))

    overrides: dict[str, dict] = {}
    categories: list[str] = []
    for src in (state.get("sources") or {}).values():
        for cat in src.get("categories") or []:
            if cat not in categories:
                categories.append(cat)
        assignments = src.get("assignments") or {}
        notes = src.get("notes") or {}
        tags = src.get("tags") or {}
        meta = src.get("meta") or {}
        keys = set(assignments) | set(notes) | set(tags) | set(meta)
        for key in keys:
            post = by_key.get(str(key))
            if not post:
                continue
            patch: dict = {}
            if isinstance(assignments.get(key), list):
                patch["categories"] = assignments[key]
            if isinstance(notes.get(key), str) and notes[key]:
                patch["notes"] = notes[key]
            if isinstance(tags.get(key), list) and tags[key]:
                patch["tags"] = tags[key]
            item = meta.get(key) if isinstance(meta, dict) else None
            if isinstance(item, dict):
                if isinstance(item.get("notes"), str) and item["notes"]:
                    patch["notes"] = item["notes"]
                if isinstance(item.get("tags"), list) and item["tags"]:
                    patch["tags"] = item["tags"]
            if patch:
                overrides[str(post["id"])] = {**overrides.get(str(post["id"]), {}), **patch}

    doc = {
        "overrides": overrides,
        "categories": categories,
        "__category_order": categories,
        "updated_at": state.get("updated_at") or "",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"wrote {out} · {len(overrides)} posts · {len(categories)} categories")


if __name__ == "__main__":
    main()
