#!/usr/bin/env python3
"""Lay out six GitHub repositories from the archive, using hard links locally.

The repos contain 9-ish GB of media each. Repo 1 also contains a small ``docs``
folder with the static GitHub Pages site.

    python publish/partition_archive.py
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from build_static import make_buckets

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "data/posts"
STAGE = ROOT / "publish/repos"
SITE = ROOT / "publish/site"


def link_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main() -> None:
    buckets = make_buckets()
    repos = buckets["repos"]
    folder_repo = {
        folder: repos[index] for folder, index in buckets["folders"].items()
    }
    if STAGE.exists():
        shutil.rmtree(STAGE)
    for repo in repos:
        (STAGE / repo).mkdir(parents=True)
        (STAGE / repo / "README.md").write_text(
            f"# {repo}\n\nInstagram saved-post archive media bucket. "
            "The browsable gallery is served by `ig-archive-1/docs`.\n",
            encoding="utf-8",
        )

    counts = {repo: 0 for repo in repos}
    total = 0
    for folder in sorted(POSTS.iterdir()):
        if not folder.is_dir():
            continue
        repo = folder_repo.get(folder.name, repos[0])
        target = STAGE / repo / "posts" / folder.name
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(folder)
            link_file(path, target / rel)
            counts[repo] += 1
            total += 1

    # Repo 1 publishes only /docs, so the media in /posts is not part of Pages.
    docs = STAGE / repos[0] / "docs"
    if SITE.is_dir():
        shutil.copytree(SITE, docs)
    (STAGE / repos[0] / "docs" / ".nojekyll").write_text("", encoding="utf-8")

    print(json.dumps({"files": total, "per_repo": counts, "buckets": buckets}, indent=1))


if __name__ == "__main__":
    main()
