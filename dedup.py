#!/usr/bin/env python3
"""Cross-run dedup index: never download the same Instagram post twice.

    python dedup.py backfill --run-db data/1911/archive.db --dedup data/dedup.db
    python dedup.py stats --dedup data/dedup.db
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    media_id TEXT PRIMARY KEY,
    shortcode TEXT UNIQUE,
    folder TEXT,
    run TEXT,
    downloaded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_shortcode ON seen(shortcode);
"""


class Dedup:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.executescript(SCHEMA)

    def folder_for(self, media_id: str, shortcode: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT folder FROM seen WHERE media_id = ? OR shortcode = ?",
            (str(media_id), shortcode),
        ).fetchone()
        return row[0] if row else None

    def mark(self, media_id: str, shortcode: str, folder: str, run: str,
             commit: bool = True) -> None:
        self.conn.execute(
            """INSERT INTO seen (media_id, shortcode, folder, run, downloaded_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(media_id) DO UPDATE SET
                 shortcode=excluded.shortcode, folder=excluded.folder,
                 run=excluded.run, downloaded_at=excluded.downloaded_at""",
            (
                str(media_id), shortcode, folder, run,
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            ),
        )
        if commit:
            self.conn.commit()

    def stats(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]


def backfill(run_db: Path, dedup: Dedup, run: str) -> int:
    src = sqlite3.connect(run_db)
    src.row_factory = sqlite3.Row
    count = 0
    for row in src.execute(
        "SELECT media_id, shortcode, folder FROM posts WHERE folder IS NOT NULL"
    ):
        if not row["shortcode"]:
            continue
        dedup.mark(
            row["media_id"] or f"folder:{row['shortcode']}",
            row["shortcode"],
            row["folder"],
            run,
            commit=False,
        )
        count += 1
    dedup.conn.commit()
    src.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-run dedup index")
    parser.add_argument("--dedup", default="data/dedup.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backfill")
    p.add_argument("--dedup", default=argparse.SUPPRESS)
    p.add_argument("--run-db", required=True)
    p.add_argument("--run", required=True)

    p = sub.add_parser("stats")
    p.add_argument("--dedup", default=argparse.SUPPRESS)

    args = parser.parse_args()
    dedup = Dedup(Path(args.dedup))
    if args.cmd == "backfill":
        n = backfill(Path(args.run_db), dedup, args.run)
        print(f"backfilled {n} posts; total {dedup.stats()}")
    else:
        print(f"{dedup.stats()} posts recorded")


if __name__ == "__main__":
    main()
