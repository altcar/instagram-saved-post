#!/usr/bin/env python3
"""Restore manual order to the current Instagram Saved order (newest first).

The raw feed items are returned in exactly the order the Saved page shows.
This sets posts.sort_order from that order, then appends archived posts that
are no longer saved.

    python resort.py --db data/archive.db --raw data/raw/items.jsonl
"""

from __future__ import annotations

import argparse
import json
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild saved order")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--raw", default="data/raw/items.jsonl")
    args = parser.parse_args()

    order = []
    seen = set()
    with open(args.raw, encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            media = item.get("media") or item
            code = media.get("code")
            if code and code not in seen:
                seen.add(code)
                order.append(code)

    conn = sqlite3.connect(args.db, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    rows = conn.execute(
        "SELECT id, shortcode, COALESCE(taken_at,'') FROM posts"
    ).fetchall()
    by_code = {code: post_id for post_id, code, _ in rows}

    updates = []
    rank = 0
    for code in order:
        post_id = by_code.get(code)
        if post_id is not None:
            updates.append((rank, post_id))
            rank += 1
    for post_id, code, taken in sorted(
        (row for row in rows if row[1] not in seen),
        key=lambda row: row[2],
        reverse=True,
    ):
        updates.append((rank, post_id))
        rank += 1

    conn.executemany(
        "UPDATE posts SET sort_order = ? WHERE id = ?", updates
    )
    conn.commit()

    top = conn.execute(
        "SELECT shortcode FROM posts ORDER BY sort_order LIMIT 10"
    ).fetchall()
    conn.close()
    print(f"ordered {rank} posts; live saved items in raw feed: {len(order)}")
    for index, row in enumerate(top):
        print(f"  {index:5d}  {row[0]}")


if __name__ == "__main__":
    main()
