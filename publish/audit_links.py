#!/usr/bin/env python3
"""Audit every link used by the published galleries.

For each source it loads the gallery page
``https://altcar.github.io/instagram-saved-post/gallery/?source=<source>`` and
the manifest that page renders, then checks every media, thumbnail, cover,
avatar, comment and permalink URL.  Results go to ``publish/link_audit.json``
and ``publish/LINK_AUDIT.md``.

    python publish/audit_links.py                 # all sources, full check
    python publish/audit_links.py --sample 5      # quick smoke test
    python publish/audit_links.py --base http://127.0.0.1:8780/ig/
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://altcar.github.io/instagram-saved-post/"
SOURCES = [
    ("shalevfrfr", "instagram"),
    ("threads", "threads"),
    ("x", "x"),
    ("rednote", "rednote"),
    ("reddit", "reddit"),
]
UA = {"User-Agent": "ig-archive-link-audit/1.0 (+static gallery check)",
      "Accept": "*/*"}
TIMEOUT = 45


def fetch(url: str, *, head_first: bool = True,
          retries: int = 6) -> tuple[int, str]:
    """Return (status, note). Retries the Hugging Face CDN's 429s politely."""
    methods = ("HEAD", "GET") if head_first else ("GET",)
    last = (0, "unreachable")
    attempt = 0
    while attempt <= retries:
        attempt += 1
        for method in methods:
            request = urllib.request.Request(url, method=method, headers=UA)
            if method == "GET":
                request.add_header("Range", "bytes=0-0")
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                    size = response.headers.get("Content-Length") or ""
                    return response.status, f"{size} bytes"
            except urllib.error.HTTPError as exc:
                last = (exc.code, exc.reason or "")
                if exc.code == 429:
                    break  # back off below, outside the method loop
                if exc.code == 403:
                    return last
            except Exception as exc:
                last = (0, type(exc).__name__)
            time.sleep(0.05)
        if last[0] != 429 or attempt > retries:
            break
        delay = min(60, 6 * (2 ** attempt)) + (hash(url) % 3)
        time.sleep(delay)
    return last


def get_json(url: str):
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def classify(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.endswith("huggingface.co") or ".hf.co" in host:
        return "hf"
    if "altcar.github.io" in host:
        return "site"
    return "external"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--sample", type=int, default=0,
                        help="only check N posts per source (0 = all)")
    parser.add_argument("--sources", default="",
                        help="comma separated source ids (default: all five)")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    base = args.base if args.base.endswith("/") else args.base + "/"
    sources = ([s.strip() for s in args.sources.split(",") if s.strip()]
               if args.sources else [s for s, _ in SOURCES])
    platform_of = dict(SOURCES)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base": base,
        "pages": {},
        "sources": {},
        "failures": [],
    }

    manifest_url = f"{base}gallery/manifest.json?nc={int(time.time())}"
    try:
        status, manifest = get_json(manifest_url)
    except Exception as exc:
        print(f"FATAL: cannot read {manifest_url}: {exc}")
        return 2
    report["manifest_status"] = status
    report["manifest_posts"] = len(manifest.get("posts", []))
    posts = manifest.get("posts", [])

    for source in sources:
        page_url = f"{base}gallery/?source={source}"
        gallery_base = f"{base}gallery/"
        page_status, _ = fetch(page_url, head_first=False)
        report["pages"][source] = {"url": page_url, "status": page_status}
        selected = [p for p in posts
                    if p.get("source") == source or p.get("platform") == platform_of.get(source)]
        if args.sample:
            selected = selected[: args.sample]
        checks: list[dict] = []
        for post in selected:
            label = post.get("shortcode") or str(post.get("id"))
            targets = []
            if post.get("thumb"):
                targets.append(("thumb", post["thumb"]))
            if post.get("cover") and post["cover"] != post.get("thumb"):
                targets.append(("cover", post["cover"]))
            if post.get("avatar"):
                targets.append(("avatar", post["avatar"]))
            if post.get("comments_url"):
                targets.append(("comments", post["comments_url"]))
            for item in post.get("media") or []:
                if item.get("url"):
                    targets.append((f"media[{item.get('i', 0)}]", item["url"]))
                if item.get("thumb") and item["thumb"] != post.get("thumb"):
                    targets.append((f"thumb[{item.get('i', 0)}]", item["thumb"]))
            for name, url in targets:
                checks.append({"post": label, "id": post.get("id"), "kind": name,
                               "url": urljoin(gallery_base, url), "type": classify(urljoin(gallery_base, url))})
            if post.get("permalink"):
                checks.append({"post": label, "id": post.get("id"),
                               "kind": "permalink", "url": post["permalink"],
                               "type": "external"})

        # de-duplicate urls (a thumb may repeat across media items)
        seen: dict[tuple[str, str], dict] = {}
        for check in checks:
            seen.setdefault((check["post"], check["url"]), check)
        checks = list(seen.values())

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            statuses = list(pool.map(lambda c: fetch(c["url"]), checks))
        for check, (status, note) in zip(checks, statuses):
            check["status"] = status
            check["note"] = note
            # Hugging Face / site assets must be 200. External platform pages
            # (Instagram, X, Reddit) commonly answer 403/429 to anonymous
            # clients, which is reported but not counted as a broken link.
            if check["type"] in ("hf", "site"):
                check["ok"] = status in (200, 206)
            else:
                check["ok"] = status in (200, 206, 301, 302, 403, 429)
            if not check["ok"]:
                report["failures"].append({"source": source, **check})

        kinds = Counter(c["kind"].split("[")[0] for c in checks)
        failures = [c for c in checks if not c["ok"]]
        report["sources"][source] = {
            "platform": platform_of.get(source, source),
            "page_url": page_url,
            "page_status": page_status,
            "posts": len(selected),
            "posts_in_manifest": len([p for p in posts
                                      if p.get("source") == source
                                      or p.get("platform") == platform_of.get(source)]),
            "checks": len(checks),
            "ok": len(checks) - len(failures),
            "failed": len(failures),
            "by_kind": dict(kinds),
            "status_histogram": dict(Counter(c["status"] for c in checks)),
            "failed_urls": [{"kind": c["kind"], "url": c["url"],
                             "status": c["status"]} for c in failures[:25]],
            "checked_posts": [c["post"] for c in checks][:0],
        }
        print(f"{source:12s} posts={len(selected):4d} checks={len(checks):5d} "
              f"failed={len(failures)}")

    out_json = ROOT / "publish/link_audit.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    lines = ["# Link audit", "",
             f"- generated: {report['generated_at']}",
             f"- base: {base}",
             f"- manifest: {report['manifest_status']} "
             f"({report['manifest_posts']} posts)", ""]
    lines += ["| source | posts | checks | ok | failed | page |",
              "|---|---:|---:|---:|---:|---:|"]
    for source, info in report["sources"].items():
        lines.append(f"| {source} ({info['platform']}) | {info['posts']} | "
                     f"{info['checks']} | {info['ok']} | {info['failed']} | "
                     f"{info['page_status']} |")
    total_checks = sum(i["checks"] for i in report["sources"].values())
    total_failed = sum(i["failed"] for i in report["sources"].values())
    lines += ["", f"**{total_checks - total_failed}/{total_checks} links OK**"
                  f" · {total_failed} failing", ""]

    lines += ["## Per-source detail", ""]
    for source, info in report["sources"].items():
        lines.append(f"### {source} ({info['platform']})")
        lines.append("")
        lines.append(f"- gallery page: {info['page_url']} → {info['page_status']}")
        lines.append(f"- posts: {info['posts']} of {info['posts_in_manifest']} in manifest")
        lines.append(f"- checked: {info['checks']} ({info['ok']} ok, {info['failed']} failed)")
        lines.append(f"- by kind: " + ", ".join(f"{k}={v}"
                                                for k, v in sorted(info['by_kind'].items())))
        lines.append(f"- statuses: " + ", ".join(f"{k}×{v}"
                                                 for k, v in sorted(info['status_histogram'].items())))
        if info["failed_urls"]:
            lines.append("- failures:")
            for item in info["failed_urls"]:
                lines.append(f"  - {item['kind']} {item['status']} {item['url']}")
        lines.append("")

    if report["failures"]:
        lines += ["## Failures by type", ""]
        by_type = defaultdict(int)
        for failure in report["failures"]:
            by_type[(failure["source"], failure["kind"].split("[")[0],
                     failure["status"])] += 1
        lines += ["| source | kind | status | count |", "|---|---|---:|---:|"]
        for (source, kind, status), count in sorted(by_type.items()):
            lines.append(f"| {source} | {kind} | {status} | {count} |")
        lines.append("")

    lines += ["## Notes", "",
              "- `hf` links must answer 200/206 on the Hugging Face dataset "
              "`shalevlau/ig-archive-test`.",
              "- `permalink` links point at the original platform post. Instagram, "
              "X and Reddit routinely answer 403/429 to anonymous clients; those "
              "statuses are reported but not counted as broken.",
              "- Telegram is intentionally excluded from media hosting and from "
              "this audit.", ""]
    out_md = ROOT / "publish/LINK_AUDIT.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out_md} and {out_json}")
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
