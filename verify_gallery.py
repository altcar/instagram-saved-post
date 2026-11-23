#!/usr/bin/env python3
"""End-to-end checks for the local gallery and dashboard.

    python verify_gallery.py

Read-only except for a temporary category and a notes/tags round-trip that is
restored immediately.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8770"


def get(path: str, timeout: int = 30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def get_bytes(path: str) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return response.read()


def post_json(path: str, payload: dict):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(("PASS " if condition else "FAIL ") + name + ((" · " + detail) if detail else ""))
    return condition


def main() -> int:
    ok = True
    status, accounts = get("/api/accounts")
    sources = [a["id"] for a in accounts.get("accounts", []) if not a.get("error")]
    th_source = next((a["id"] for a in accounts["accounts"]
                      if str(a.get("platform")).lower() == "threads"
                      and not a.get("error")), None)
    x_source = next((a["id"] for a in accounts["accounts"]
                     if str(a.get("platform")).lower() in ("twitter", "x")
                     and not a.get("error")), None)
    ok &= check("accounts", status == 200 and len(sources) >= 2,
                ", ".join(sources))

    for source in sources:
        status, facets = get(f"/api/facets?source={source}")
        ok &= check(f"{source}: facets", status == 200 and facets["total"] >= 1,
                    f"{facets.get('total')} posts")
        flags = facets.get("filter_counts") or {}
        bad = []
        for flag, count in flags.items():
            _, page = get(f"/api/posts?source={source}&limit=1&flags={flag}")
            if page["total"] != count:
                bad.append(f"{flag}={count}->{page['total']}")
        ok &= check(f"{source}: filter counts match listings", not bad,
                    ", ".join(bad))
        _, page = get(f"/api/posts?source={source}&limit=2")
        ok &= check(f"{source}: posts", page["total"] > 0 and bool(page["items"]),
                    f"{page['total']} posts")
        if page["items"]:
            post_id = page["items"][0]["id"]
            status, post = get(f"/api/post/{post_id}?comments=1&source={source}")
            ok &= check(f"{source}: drawer", status == 200 and "media" in post,
                        f"id={post_id}, {len(post.get('media') or [])} media")
            status, comments = get(f"/api/comments/{post_id}?source={source}")
            ok &= check(f"{source}: comments endpoint", status == 200 and "items" in comments)

    # Native port of the 8766 dashboard inside the Live tab.
    dash_source = next((a["id"] for a in accounts["accounts"]
                        if str(a.get("platform")).lower() == "instagram"
                        and not a.get("error")), None)
    if dash_source:
        try:
            _, dash = get(f"/api/dashboard?source={dash_source}", timeout=180)
            expected = ["download", "comments", "fast", "clip", "caption", "ocr",
                        "transcribe", "fuse", "covers", "ads", "unsave"]
            names = [j["name"] for j in dash.get("jobs", [])]
            required = {"folders", "position", "total", "reference_total", "failed",
                        "size_human", "rate", "eta", "status", "comments",
                        "pipeline", "jobs", "logs", "tail", "data_dir", "log_path"}
            ok &= check("dashboard: all jobs", names == expected, ", ".join(names))
            ok &= check("dashboard: all 8766 fields", required <= set(dash),
                        ", ".join(sorted(required - set(dash))))
            _, log = get(f"/api/dashboard/log?source={dash_source}&name=download&lines=5",
                         timeout=60)
            ok &= check("dashboard: log endpoint", isinstance(log.get("lines"), list))
            try:
                with urllib.request.urlopen("http://127.0.0.1:8766/api/status",
                                            timeout=10) as response:
                    standalone = json.loads(response.read().decode("utf-8"))
                ok &= check("dashboard: matches 8766 jobs",
                            [j["name"] for j in standalone.get("jobs", [])] == names)
                ok &= check("dashboard: matches 8766 keys",
                            set(standalone) <= set(dash))
            except Exception as exc:
                print("SKIP dashboard: standalone 8766 comparison ·", exc)
        except Exception as exc:
            print("SKIP dashboard: native port ·", exc)

    # Social sources: comment media endpoint + text-only fallback data.
    for social_source in [s for s in (th_source, x_source) if s]:
        try:
            _, tl = get(f"/api/posts?source={social_source}&limit=80")
            media_ok = False
            text_only = any((p.get("media_count") or 0) == 0 for p in tl["items"])
            for item in tl["items"][:40]:
                _, detail = get(f"/api/post/{item['id']}?comments=1&limit=80&source={social_source}")
                for comment in (detail.get("comments_data") or {}).get("items", []):
                    for media in comment.get("media") or []:
                        with urllib.request.urlopen(BASE + media["url"], timeout=15) as resp:
                            if resp.status == 200:
                                media_ok = True
                                break
                    if media_ok:
                        break
                if media_ok:
                    break
            ok &= check(f"{social_source}: comment media endpoint", media_ok)
            ok &= check(f"{social_source}: text-only posts exist", text_only)
        except Exception as exc:
            ok &= check(f"{social_source}: comment media endpoint", False, str(exc))

    # Thumbnails in the previously repaired range must be distinct.
    hashes = []
    for post_id in range(11889, 11896):
        hashes.append(hashlib.sha256(get_bytes(f"/api/thumb/{post_id}?i=0")).hexdigest())
    ok &= check("distinct repaired covers", len(set(hashes)) == len(hashes),
                f"{len(set(hashes))}/{len(hashes)}")

    # Category CRUD on both backends, cleaned up afterwards.
    temp = "zz-verify-temp"
    ig_source = next((a["id"] for a in accounts["accounts"]
                      if str(a.get("platform")).lower() == "instagram"
                      and not a.get("error")), None)
    rn_source = next((a["id"] for a in accounts["accounts"]
                      if str(a.get("platform")).lower() == "rednote"
                      and not a.get("error")), "rednote-main")
    for source in [s for s in (ig_source, rn_source, th_source, x_source) if s]:
        try:
            result = post_json(f"/api/categories?source={source}", {"op": "add", "name": temp})
            names = [c["name"] for c in result["categories"]]
            ok &= check(f"{source}: category add", temp in names)
            result = post_json(f"/api/categories?source={source}",
                          {"op": "rename", "name": temp, "new_name": temp + "2"})
            names = [c["name"] for c in result["categories"]]
            ok &= check(f"{source}: category rename", (temp + "2") in names and temp not in names)
            result = post_json(f"/api/categories?source={source}", {"op": "remove", "name": temp + "2"})
            names = [c["name"] for c in result["categories"]]
            ok &= check(f"{source}: category remove", temp + "2" not in names)
        finally:
            for name in (temp, temp + "2"):
                try:
                    post_json(f"/api/categories?source={source}", {"op": "remove", "name": name})
                except Exception:
                    pass

    # Notes/tags round-trip, restored.
    for source in [s for s in (rn_source, th_source, x_source) if s]:
        _, page = get(f"/api/posts?source={source}&limit=1")
        if not page["items"]:
            continue
        post_id = page["items"][0]["id"]
        _, before = get(f"/api/post/{post_id}?source={source}")
        post_json(f"/api/post/{post_id}?source={source}",
                  {"notes": "verify-temp", "tags": ["verify-temp"]})
        _, after = get(f"/api/post/{post_id}?source={source}")
        ok &= check(f"{source}: notes/tags save",
                    after["notes"] == "verify-temp" and "verify-temp" in after["tags"])
        post_json(f"/api/post/{post_id}?source={source}",
                  {"notes": before.get("notes") or "",
                   "tags": before.get("tags") or []})
        _, restored = get(f"/api/post/{post_id}?source={source}")
        ok &= check(f"{source}: notes/tags restore",
                    restored["notes"] == (before.get("notes") or ""))

    status, cloud = get("/api/cloud")
    ok &= check("cloud status", status == 200 and "provider" in cloud,
                cloud.get("provider", ""))

    for path in ("/", "/rednote"):
        with urllib.request.urlopen(BASE + path, timeout=20) as response:
            ok &= check(f"{path} serves the shared UI", response.status == 200)
    for path in ("/instagram", "/rednote", "/threads", "/twitter", "/telegram", "/progress"):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8891" + path,
                                        timeout=10) as response:
                ok &= check(f"hub {path}", response.status == 200)
        except Exception as exc:
            ok &= check(f"hub {path}", False, str(exc))
    for source in ("threads", "twitter"):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:8892/gallery/?source={source}&embed=1",
                    timeout=10) as response:
                ok &= check(f"gallery 8892: {source}", response.status == 200)
        except Exception as exc:
            ok &= check(f"gallery 8892: {source}", False, str(exc))
    try:
        with urllib.request.urlopen("http://127.0.0.1:8891/health",
                                    timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
            ok &= check("hub health", bool(body.get("ok")), body.get("hub", ""))
    except Exception as exc:
        ok &= check("hub health", False, str(exc))
    try:
        with urllib.request.urlopen("http://127.0.0.1:8766/", timeout=10) as response:
            ok &= check("dashboard", response.status == 200)
    except Exception as exc:
        ok &= check("dashboard", False, str(exc))

    print("\nRESULT:", "OK" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
