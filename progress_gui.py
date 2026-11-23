#!/usr/bin/env python3
"""Live progress dashboard for the Instagram saved-post downloader.

Serves a small local web UI (no external assets) that reports how many post
folders exist, the current list position, download rate/ETA, failures, disk
usage and the tail of the downloader log.

Example:
    python progress_gui.py --data data --log /tmp/ig.log --total 971
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple

INDEX_RE = re.compile(r"(?:Downloading|Skip existing) (\d{4})_([A-Za-z0-9_-]+)")
DONE_RE = re.compile(r"Saved (\d+) entries")
FAIL_RE = re.compile(r"Failed to (?:download|serialise)")
COMMENT_RE = re.compile(
    r"(\d+)/(\d+) saved=(\d+) comments=(\d+)"
)

REPO = Path(__file__).resolve().parent
IGPY = Path("/tmp/opencode/igvenv/bin/python")
PY = str(IGPY if IGPY.is_file() else "python3")

_sampler: Deque[Tuple[float, int]] = deque(maxlen=240)
_comment_sampler: Deque[Tuple[float, int]] = deque(maxlen=240)
_job_samplers: Dict[str, Deque[Tuple[float, int]]] = {}
_comment_breakdown_cache: Tuple[float, Dict[str, int]] = (0.0, {})
_cache: Dict[str, Tuple[float, int, int]] = {}


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def dir_stats(path: Path, ttl: float = 4.0) -> Tuple[int, int]:
    """Return (number of post folders, total bytes), cached briefly."""
    key = str(path)
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < ttl:
        return cached[1], cached[2]
    folders = 0
    size = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                folders += 1
                try:
                    with os.scandir(entry.path) as files:
                        for item in files:
                            try:
                                if item.is_file():
                                    size += item.stat().st_size
                            except OSError:
                                pass
                except OSError:
                    pass
    except OSError:
        pass
    _cache[key] = (now, folders, size)
    return folders, size


def running_process(script: str = "download_saved.py") -> Optional[Tuple[int, int]]:
    """Return (pid, elapsed_seconds) of a Python job, if it is running."""
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,etimes=,args="], text=True, timeout=3
        )
    except Exception:
        return None
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, elapsed, args = parts
        if script not in args or "grep" in args:
            continue
        command = args.split(None, 1)[0]
        if "python" in os.path.basename(command):
            return int(pid), int(elapsed)
    return None


def process_matches(script: str, marker: str = "") -> Optional[Tuple[int, int, bool]]:
    """Return pid, elapsed, paused for a Python job matching script+marker."""
    try:
        output = subprocess.check_output(
            ["ps", "-eo", "pid=,stat=,etimes=,args="], text=True, timeout=3
        )
    except Exception:
        return None
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, stat, elapsed, args = parts
        if script not in args or marker not in args or "grep" in args:
            continue
        if "python" not in os.path.basename(args.split(None, 1)[0]):
            continue
        return int(pid), int(elapsed), stat.startswith("T")
    return None


def sessionid() -> str:
    try:
        return subprocess.check_output(
            [
                PY, "-c",
                "from chrome_cookies import load_cookies;"
                "print(load_cookies('instagram.com').get('sessionid',''))",
            ],
            cwd=REPO,
            text=True,
            timeout=15,
        ).strip()
    except Exception:
        return ""


def job_specs(
    pipeline: Dict[str, Any],
    folders: int,
    comments: Optional[Dict[str, int]] = None,
) -> Dict[str, Dict[str, Any]]:
    total = pipeline.get("total") or folders
    sources = pipeline.get("sources") or {}
    comments = comments or {}
    specs = {
        "download": {
            "label": "Download + enrich + unsave",
            "script": "post_download_pipeline.py",
            "marker": "--username",
            "count": folders,
            "total": total,
            "breakdown": {
                "success": folders,
                "missing_media": pipeline.get("missing_media", 0),
                "errors": 0,
            },
            "log": "/tmp/opencode/download_dashboard.log",
            "cmd": lambda: [
                PY, "post_download_pipeline.py", "--username", "shalevfrfr",
                "--sessionid", sessionid(), "--out", "data/posts",
                "--json", "data/saved_posts.json", "--db", "data/dedup.db",
                "--archive-db", "data/archive.db",
            ],
        },
        "comments": {
            "label": "Download comments",
            "script": "comments_api_fetch.py",
            "marker": "",
            "count": pipeline.get("comments", 0),
            "total": total,
            "breakdown": {
                "success": comments.get("scanned", pipeline.get("comments", 0)),
                "with_comments": comments.get("with_comments", 0),
                "no_comments": comments.get("no_comments", 0),
                "errors": comments.get("errors", 0),
            },
            "log": "/tmp/opencode/comments_all.log",
            "cmd": lambda: [
                PY, "comments_api_fetch.py", "--workers", "2",
                "--delay", "0.25", "--max-pages", "5",
            ],
        },
        "fast": {
            "label": "Classify · fast",
            "script": "classify.py", "marker": " fast",
            "count": sources.get("fast", 0), "total": total,
            "log": "/tmp/opencode/classify_fast.log",
            "cmd": lambda: [PY, "classify.py", "fast",
                            "--db", "data/archive.db", "--media", "data/posts",
                            "--raw", "data/raw/items.jsonl"],
        },
        "clip": {
            "label": "Classify · CLIP",
            "script": "classify.py", "marker": " clip",
            "count": sources.get("clip", 0), "total": total,
            "log": "/tmp/opencode/clip_video.log",
            "cmd": lambda: [PY, "classify.py", "clip", "--limit", "100",
                            "--db", "data/archive.db", "--media", "data/posts",
                            "--cache", "data/.cache"],
        },
        "caption": {
            "label": "Vision · BLIP",
            "script": "classify.py", "marker": " caption",
            "count": pipeline.get("vision", 0), "total": total,
            "log": "/tmp/opencode/vision.log",
            "cmd": lambda: [PY, "classify.py", "caption", "--limit", "20",
                            "--db", "data/archive.db", "--media", "data/posts",
                            "--cache", "data/.cache"],
        },
        "ocr": {
            "label": "OCR · tesseract",
            "script": "classify.py", "marker": " ocr",
            "count": pipeline.get("ocr", 0), "total": total,
            "log": "/tmp/opencode/ocr.log",
            "cmd": lambda: [PY, "classify.py", "ocr",
                            "--db", "data/archive.db", "--media", "data/posts",
                            "--cache", "data/.cache"],
        },
        "transcribe": {
            "label": "Transcript · Whisper",
            "script": "classify.py", "marker": " transcribe",
            "count": pipeline.get("transcript", 0), "total": total,
            "log": "/tmp/opencode/transcribe.log",
            "cmd": lambda: [PY, "classify.py", "transcribe", "--limit", "20",
                            "--db", "data/archive.db", "--media", "data/posts",
                            "--cache", "data/.cache"],
        },
        "fuse": {
            "label": "Classify · fuse",
            "script": "classify.py", "marker": " fuse",
            "count": sources.get("fusion", 0), "total": total,
            "log": "/tmp/opencode/classify_fuse.log",
            "cmd": lambda: [PY, "classify.py", "fuse",
                            "--db", "data/archive.db", "--media", "data/posts",
                            "--cache", "data/.cache"],
        },
        "covers": {
            "label": "Covers",
            "script": "fetch_covers.py", "marker": "",
            "count": pipeline.get("covers", 0), "total": total,
            "log": "/tmp/opencode/covers_local.log",
            "cmd": lambda: [PY, "fetch_covers.py",
                            "--raw", "data/raw/items.jsonl",
                            "--media", "data/posts", "--db", "data/archive.db",
                            "--workers", "8"],
        },
        "ads": {
            "label": "Ads sync",
            "script": "ads_sync.py", "marker": "",
            "count": pipeline.get("ads", 0), "total": total,
            "log": "/tmp/opencode/ads_carousel.log",
            "cmd": lambda: [PY, "ads_carousel.py",
                            "--db", "data/archive.db", "--posts", "data/posts",
                            "--ads-json", "data/ads.json"],
        },
        "unsave": {
            "label": "Unsave validated",
            "script": "unsave_validated.py", "marker": "--run",
            "count": pipeline.get("unsaved", 0),
            "total": pipeline.get("unsaved", 0) + pipeline.get("unsave_pending", 0),
            "breakdown": {
                "success": pipeline.get("unsaved", 0),
                "errors": 0,
                "remaining": pipeline.get("unsave_pending", 0),
                "ineligible": max(
                    0,
                    pipeline.get("still_saved", 0)
                    - pipeline.get("unsave_pending", 0),
                ),
            },
            "log": "/tmp/opencode/unsave.log",
            "cmd": None,
        },
    }
    specs["ads"]["breakdown"] = {
        "success": pipeline.get("ads", 0),
        "non_ads": max(0, total - pipeline.get("ads", 0)),
        "errors": 0,
    }
    specs["covers"]["breakdown"] = {
        "success": pipeline.get("covers", 0),
        "missing": max(0, total - pipeline.get("covers", 0)),
        "errors": 0,
    }
    for name, spec in specs.items():
        if "breakdown" not in spec:
            count = int(spec.get("count") or 0)
            spec["breakdown"] = {
                "success": count,
                "not_applicable": max(0, total - count),
                "errors": 0,
            }
    return specs


def job_status(
    specs: Dict[str, Dict[str, Any]], now: float, detect_process: bool = True
) -> list[Dict[str, Any]]:
    jobs = []
    for name, spec in specs.items():
        found = process_matches(spec["script"], spec.get("marker", "")) if detect_process else None
        pid = found[0] if found else None
        elapsed = found[1] if found else None
        paused = found[2] if found else False
        count = int(spec.get("count") or 0)
        total = int(spec.get("total") or 0)
        breakdown = dict(spec.get("breakdown") or {})
        status = "paused" if paused else ("running" if found else "stopped")
        if not found:
            accounted = (
                breakdown.get("success", count)
                + breakdown.get("missing", 0)
                + breakdown.get("missing_media", 0)
                + breakdown.get("errors", 0)
                + breakdown.get("non_ads", 0)
                + breakdown.get("not_applicable", 0)
            )
            if name in {"fast", "clip", "caption", "ocr", "transcribe", "fuse"}:
                status = "partial"
            elif name == "unsave":
                status = "idle" if count == 0 else ("verified" if total and count >= total else "partial")
            elif total and accounted >= total:
                status = "verified"
        sampler = _job_samplers.setdefault(name, deque(maxlen=240))
        sampler.append((now, count))
        while sampler and now - sampler[0][0] > 120:
            sampler.popleft()
        rate = 0.0
        if found and len(sampler) >= 2:
            dt = sampler[-1][0] - sampler[0][0]
            if dt > 3:
                rate = (sampler[-1][1] - sampler[0][1]) / dt
        eta = None
        if found and rate > 0.001:
            remaining = max(0, total - count)
            eta = remaining / rate if remaining else 0
        jobs.append({
            "name": name, "label": spec["label"], "count": count, "total": total,
            "rate": rate, "eta": eta, "status": status, "pid": pid,
            "elapsed": elapsed, "available": spec.get("cmd") is not None,
            "log": spec["log"], "breakdown": breakdown,
        })
    return jobs


def start_job(specs: Dict[str, Dict[str, Any]], name: str) -> Tuple[bool, str]:
    spec = specs.get(name)
    if not spec or not spec.get("cmd"):
        return False, "job is not startable"
    found = process_matches(spec["script"], spec.get("marker", ""))
    if found:
        if found[2]:
            os.kill(found[0], 18)  # SIGCONT
            return True, "resumed"
        return False, "already running"
    cmd = spec["cmd"]()
    if "--sessionid" in cmd and not cmd[cmd.index("--sessionid") + 1]:
        return False, "no Instagram sessionid in Chrome cookies"
    log = Path(spec["log"])
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "ab", buffering=0)
    subprocess.Popen(
        cmd, cwd=REPO, stdout=handle, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    handle.close()
    return True, "started"


def pause_job(specs: Dict[str, Dict[str, Any]], name: str) -> Tuple[bool, str]:
    spec = specs.get(name)
    if not spec:
        return False, "unknown job"
    found = process_matches(spec["script"], spec.get("marker", ""))
    if not found:
        return False, "not running"
    if found[2]:
        os.kill(found[0], 18)  # SIGCONT
        return True, "resumed"
    os.kill(found[0], 19)  # SIGSTOP
    return True, "paused"


def parse_log(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "exists": path.exists(),
        "position": 0,
        "shortcode": None,
        "failed": 0,
        "done": None,
        "tail": [],
        "last_line": "",
        "mtime": None,
    }
    if not path.exists():
        return info
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
            info["mtime"] = path.stat().st_mtime
    except OSError:
        return info

    matches = INDEX_RE.findall(text)
    if matches:
        index, shortcode = matches[-1]
        info["position"] = int(index)
        info["shortcode"] = shortcode
    info["failed"] = len(FAIL_RE.findall(text))
    done = DONE_RE.findall(text)
    if done:
        info["done"] = int(done[-1])
    lines = [line for line in text.splitlines() if line.strip()]
    info["tail"] = lines[-14:]
    if lines:
        info["last_line"] = lines[-1][:220]
    return info


def parse_comments_log(path: Path) -> Dict[str, Any]:
    info = {"done": 0, "total": 0, "saved": 0, "comments": 0,
            "last_line": "", "tail": [], "exists": path.exists()}
    if not path.exists():
        return info
    try:
        lines = [line for line in path.read_text(
            encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return info
    matches = COMMENT_RE.findall("\n".join(lines))
    if matches:
        done, total, saved, comments = matches[-1]
        info.update(
            done=int(done), total=int(total), saved=int(saved),
            comments=int(comments),
        )
    info["tail"] = lines[-10:]
    if lines:
        info["last_line"] = lines[-1][:220]
    return info


def comment_db_stats(db_path: Path) -> Tuple[int, int]:
    if not db_path.is_file():
        return 0, 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        fetched = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE comments_fetched_at IS NOT NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        conn.close()
        return int(fetched), int(total)
    except Exception:
        return 0, 0


def db_progress(db_path: Path) -> Dict[str, Any]:
    """Counts for every pipeline stage that writes into the archive DB."""
    if not db_path.is_file():
        return {}
    out: Dict[str, Any] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        out["total"] = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        out["categorised"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE category IS NOT NULL AND trim(category)<>''"
        ).fetchone()[0]
        out["covers"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE cover IS NOT NULL AND trim(cover)<>''"
        ).fetchone()[0]
        out["ads"] = conn.execute("SELECT COUNT(*) FROM posts WHERE is_ad=1").fetchone()[0]
        out["comments"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE comments_fetched_at IS NOT NULL"
        ).fetchone()[0]
        out["missing_media"] = conn.execute(
            "SELECT COUNT(*) FROM posts p WHERE NOT EXISTS "
            "(SELECT 1 FROM post_media m WHERE m.media_id=p.media_id)"
        ).fetchone()[0]
        out["unsaved"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE unsaved_at IS NOT NULL AND trim(unsaved_at)<>''"
        ).fetchone()[0]
        out["still_saved"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE unsaved_at IS NULL "
            "OR trim(unsaved_at)=''"
        ).fetchone()[0]
        out["unsave_pending"] = conn.execute(
            "SELECT COUNT(*) FROM posts p WHERE p.unsaved_at IS NULL "
            "AND p.comments_fetched_at IS NOT NULL "
            "AND p.owner_username IS NOT NULL AND trim(p.owner_username)<>'' "
            "AND lower(p.owner_username)<>'unknown' "
            "AND lower(p.owner_username) NOT LIKE 'post %' "
            "AND p.cover IS NOT NULL AND trim(p.cover)<>'' "
            "AND COALESCE(p.cover_ok,0)=1"
        ).fetchone()[0]
        out["vision"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE vision_caption IS NOT NULL AND trim(vision_caption)<>''"
        ).fetchone()[0]
        out["ocr"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE ocr_text IS NOT NULL AND trim(ocr_text)<>''"
        ).fetchone()[0]
        out["transcript"] = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE transcript IS NOT NULL AND trim(transcript)<>''"
        ).fetchone()[0]
        out["sources"] = {
            row[0] or "(none)": row[1]
            for row in conn.execute(
                "SELECT category_source, COUNT(*) FROM posts GROUP BY category_source"
            )
        }
        conn.close()
    except Exception:
        return {}
    return out


def comment_breakdown(db_path: Path, posts_dir: Path) -> Dict[str, int]:
    global _comment_breakdown_cache
    now = time.time()
    cached_at, cached = _comment_breakdown_cache
    if cached and now - cached_at < 30:
        return cached
    if not db_path.is_file():
        return {}
    out = {"scanned": 0, "with_comments": 0, "no_comments": 0, "errors": 0, "total": 0}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT folder, comments_fetched_at FROM posts WHERE folder IS NOT NULL"
        ).fetchall()
        out["total"] = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        conn.close()
    except Exception:
        return out
    for row in rows:
        if not row["comments_fetched_at"]:
            continue
        out["scanned"] += 1
        path = posts_dir / row["folder"] / "comments.json"
        count = 0
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                count = len(value) if isinstance(value, list) else 0
            except (OSError, ValueError):
                count = 0
        if count:
            out["with_comments"] += 1
        else:
            out["no_comments"] += 1
    out["errors"] = max(0, out["total"] - out["scanned"])
    _comment_breakdown_cache = (now, out)
    return out


def log_catalog(download_log: Path, comments_log: Optional[Path]) -> list[Dict[str, Any]]:
    candidates = {
        "download": Path("/tmp/opencode/download_dashboard.log"),
        "download-old": download_log,
        "comments": comments_log or Path("/tmp/opencode/comments_all.log"),
        "classify:clip": Path("/tmp/opencode/clip_video.log"),
        "classify:clip-old": Path("/tmp/opencode/clip3.log"),
        "classify:ocr": Path("/tmp/opencode/ocr.log"),
        "classify:vision": Path("/tmp/opencode/vision.log"),
        "classify:transcribe": Path("/tmp/opencode/transcribe.log"),
        "covers": Path("/tmp/opencode/covers_local.log"),
        "ads": Path("/tmp/opencode/ads_carousel.log"),
        "repairs": Path("/tmp/opencode/repair_archive_full.log"),
        "raw": Path("/tmp/opencode/raw_fresh.log"),
        "unsave": Path("data/logs/unsave.log"),
        "cron": Path(f"data/logs/update-{time.strftime('%Y-%m-%d')}.log"),
    }
    catalog = []
    for name, path in candidates.items():
        path = path.expanduser()
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        catalog.append(
            {"name": name, "path": str(path), "exists": path.is_file(), "size": size}
        )
    return catalog


def build_status(
    data_dir: Path,
    log_path: Path,
    reference_total: int,
    db_path: Optional[Path] = None,
    comments_log: Optional[Path] = None,
    detect_process: bool = True,
    dir_ttl: float = 4.0,
) -> Dict[str, Any]:
    folders, size = dir_stats(data_dir, ttl=dir_ttl)
    log = parse_log(log_path)
    process = ((running_process("post_download_pipeline.py") or running_process("download_saved.py"))
               if detect_process else None)
    total = log["done"] if log["done"] is not None else reference_total
    position = log["position"]

    now = time.time()
    _sampler.append((now, folders))
    while _sampler and now - _sampler[0][0] > 120:
        _sampler.popleft()
    rate = 0.0
    if len(_sampler) >= 2:
        dt = _sampler[-1][0] - _sampler[0][0]
        if dt > 3:
            rate = (_sampler[-1][1] - _sampler[0][1]) / dt

    eta = None
    if process and rate > 0.01 and total:
        remaining = total - folders
        if remaining > 0:
            eta = remaining / rate

    status = "running" if process else ("complete" if log["done"] is not None else "stopped")

    comments = parse_comments_log(comments_log) if comments_log else {
        "done": 0, "total": 0, "saved": 0, "comments": 0, "last_line": "", "tail": []
    }
    comments_process = running_process("comments_api_fetch.py") if detect_process else None
    comments_fetched, posts_total = comment_db_stats(db_path) if db_path else (0, 0)
    _comment_sampler.append((now, comments_fetched))
    while _comment_sampler and now - _comment_sampler[0][0] > 120:
        _comment_sampler.popleft()
    comment_rate = 0.0
    if len(_comment_sampler) >= 2:
        dt = _comment_sampler[-1][0] - _comment_sampler[0][0]
        if dt > 3:
            comment_rate = (_comment_sampler[-1][1] - _comment_sampler[0][1]) / dt
    comment_eta = None
    if comments_process and comment_rate > 0.001 and posts_total:
        remaining = max(0, posts_total - comments_fetched)
        if remaining:
            comment_eta = remaining / comment_rate
    comments.update(
        {
            "posts_fetched": comments_fetched,
            "posts_total": posts_total,
            "status": "running" if comments_process else "stopped",
            "pid": comments_process[0] if comments_process else None,
            "elapsed": comments_process[1] if comments_process else None,
            "rate": comment_rate,
            "eta": comment_eta,
        }
    )
    pipeline = db_progress(db_path) if db_path else {}
    comment_stats = comment_breakdown(db_path, data_dir) if db_path else {}
    specs = job_specs(pipeline, folders, comment_stats)
    jobs = job_status(specs, now, detect_process=detect_process)

    return {
        "status": status,
        "pid": process[0] if process else None,
        "elapsed": process[1] if process else None,
        "folders": folders,
        "position": position,
        "shortcode": log["shortcode"],
        "total": total,
        "reference_total": reference_total,
        "completed_total": log["done"],
        "failed": log["failed"],
        "size": size,
        "size_human": human_bytes(size),
        "rate": rate,
        "eta": eta,
        "last_line": log["last_line"],
        "tail": log["tail"],
        "log_mtime": log["mtime"],
        "now": now,
        "data_dir": str(data_dir),
        "log_path": str(log_path),
        "comments": comments,
        "pipeline": pipeline,
        "jobs": jobs,
        "logs": log_catalog(log_path, comments_log),
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Instagram Saved — Download Progress</title>
<style>
  :root{
    --bg:#0b0d12; --panel:#121621; --panel2:#0f131c; --line:#222a3a;
    --text:#e8ecf5; --muted:#8b96ad; --accent:#c13584; --accent2:#f77737;
    --good:#35d07f; --warn:#f7b731; --bad:#ff5c7a;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 700px at 15% -10%,#24122a 0%,transparent 60%),
       radial-gradient(1000px 700px at 100% 0%,#132333 0%,transparent 55%),var(--bg);
       color:var(--text);font:15px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial}
  .wrap{max-width:1080px;margin:0 auto;padding:32px 22px 60px}
  header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:26px}
  h1{font-size:22px;margin:0;letter-spacing:.2px}
  h1 small{display:block;font-size:12px;color:var(--muted);font-weight:500;margin-top:4px}
  .pill{display:inline-flex;align-items:center;gap:8px;padding:7px 12px;border:1px solid var(--line);
        border-radius:999px;background:var(--panel);font-size:12px;color:var(--muted)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
  .dot.run{background:var(--good);box-shadow:0 0 0 0 rgba(53,208,127,.45);animation:pulse 1.8s infinite}
  .dot.stop{background:var(--bad)}
  .dot.done{background:var(--warn)}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(53,208,127,.45)}70%{box-shadow:0 0 0 10px rgba(53,208,127,0)}100%{box-shadow:0 0 0 0 rgba(53,208,127,0)}}
  .hero{background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.01));
        border:1px solid var(--line);border-radius:20px;padding:24px 26px 22px;margin-bottom:18px;
        box-shadow:0 20px 60px rgba(0,0,0,.35)}
  .heroTop{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;flex-wrap:wrap}
  .big{font-size:52px;line-height:1;font-weight:750;letter-spacing:-1.5px;
       background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
  .big span{font-size:16px;color:var(--muted);font-weight:600;letter-spacing:0;-webkit-text-fill-color:var(--muted)}
  .sub{color:var(--muted);font-size:13px;margin-top:6px}
  .bar{height:12px;border-radius:999px;background:#1b2130;overflow:hidden;margin-top:22px;position:relative}
  .bar i{display:block;height:100%;width:0;border-radius:999px;
         background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .5s ease}
  .bar.indet i{width:38%;animation:slide 1.5s ease-in-out infinite}
  @keyframes slide{0%{margin-left:-38%}100%{margin-left:100%}}
  .barmeta{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin-top:8px}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:15px 16px}
  .card .k{font-size:11px;letter-spacing:.7px;text-transform:uppercase;color:var(--muted)}
  .card .v{font-size:21px;font-weight:700;margin-top:6px}
  .card .n{font-size:11px;color:var(--muted);margin-top:2px}
  .taskGrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
  .task{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
  .task .t{display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}
  .task .t b{color:var(--text)}
  .task .tb{height:7px;border-radius:999px;background:#1b2130;overflow:hidden;margin-top:8px}
  .task .tb i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2))}
  .task .n{font-size:10.5px;color:var(--dim);margin-top:5px}
  .task .jobBtns{display:flex;gap:5px;margin-top:7px}
  .task .jobBtns button{background:#1a2231;border:1px solid var(--line);border-radius:7px;
    color:var(--text);font-size:10.5px;padding:3px 7px;cursor:pointer}
  .task .jobBtns button:disabled{opacity:.35;cursor:default}
  .task .status{font-size:10px;color:var(--muted);margin-left:auto;text-transform:uppercase;letter-spacing:.4px}
  .task .status.verified{color:var(--good)}
  .task .status.running{color:var(--accent2)}
  .task .status.paused{color:var(--warn)}
  .criteria{display:flex;flex-wrap:wrap;gap:6px 13px;margin:8px 0 2px;font-size:11px;color:var(--muted)}
  .criteria label{display:flex;align-items:center;gap:4px;cursor:pointer}
  .criteria input{accent-color:var(--accent)}
  .logselect{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    color:var(--text);padding:5px 8px;font-size:11px;max-width:320px}
  @media(max-width:780px){.taskGrid{grid-template-columns:1fr 1fr}}
  .log{background:var(--panel2);border:1px solid var(--line);border-radius:16px;overflow:hidden}
  .loghead{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);color:var(--muted);font-size:12px}
  pre{margin:0;padding:14px 16px;max-height:300px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#b9c4d8;white-space:pre-wrap;word-break:break-word}
  .path{font:11px ui-monospace,monospace;color:#6f7c94}
  .ok{color:var(--good)} .warn{color:var(--warn)} .bad{color:var(--bad)}
  @media(max-width:780px){.grid{grid-template-columns:repeat(2,1fr)}.big{font-size:40px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Instagram Saved Posts <small id="paths" class="path"></small></h1>
    <div class="pill"><span id="dot" class="dot"></span><span id="state">connecting…</span></div>
  </header>

  <section class="hero">
    <div class="heroTop">
      <div>
        <div class="big"><span id="folders">0</span> <span>posts saved</span></div>
        <div class="sub" id="position">list position —</div>
      </div>
      <div style="text-align:right">
        <div class="sub">elapsed <b id="elapsed" style="color:var(--text)">—</b></div>
        <div class="sub">rate <b id="rate" style="color:var(--text)">—</b></div>
        <div class="sub">eta <b id="eta" style="color:var(--text)">—</b></div>
      </div>
    </div>
    <div class="bar" id="barwrap"><i id="bar"></i></div>
    <div class="barmeta"><span id="progressLabel">0%</span><span id="totalLabel"></span></div>
  </section>

  <section class="hero">
    <div class="heroTop">
      <div>
        <div class="big" style="font-size:26px;letter-spacing:-.5px">Pipeline tasks</div>
        <div class="sub">classify · comments · covers · unsave · ads</div>
      </div>
    </div>
    <div class="taskGrid" id="taskGrid" style="margin-top:16px"></div>
  </section>

  <section class="grid">
    <div class="card"><div class="k">Downloaded</div><div class="v ok" id="cDownloaded">0</div><div class="n">unique folders</div></div>
    <div class="card"><div class="k">On disk</div><div class="v" id="cSize">0 B</div><div class="n" id="cFiles"></div></div>
    <div class="card"><div class="k">Failures</div><div class="v" id="cFailed">0</div><div class="n">logged errors</div></div>
    <div class="card"><div class="k">Current</div><div class="v" id="cCurrent" style="font-size:15px;word-break:break-all">—</div><div class="n" id="cIndex"></div></div>
  </section>

  <section class="log">
    <div class="loghead">
      <span>progress logs</span>
      <span><select id="logSelect" class="logselect"></select> <span id="updated">—</span></span>
    </div>
    <pre id="log"></pre>
  </section>
</div>

<script>
const $ = id => document.getElementById(id);
const fmtTime = s => {
  if (s == null) return '—';
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return (h? h+'h ':'') + (h||m? m+'m ':'') + x+'s';
};
const fmtRate = r => r > 0 ? r.toFixed(2) + ' posts/s' : '—';
let lastTail = [];
let logName = 'download';
let logCatalogLoaded = false;
const UNSAVE_KEY = 'ig_unsave_criteria_v1';
let unsaveCriteria = {};
let unsavePreview = '';
try { unsaveCriteria = JSON.parse(localStorage.getItem(UNSAVE_KEY) || '{}'); } catch (_) {}
const criteriaList = [
  ['comments', 'comments'],
  ['caption', 'caption'],
  ['cover', 'cover'],
  ['owner', 'ownerLink'],
  ['shortcode', 'post shortcode'],
  ['postid', 'Instagram post id'],
  ['adcarousel', 'non-video ad carousel'],
  ['ads', 'all ads'],
];

function taskRow(job){
  const count = job.count || 0, total = job.total || 0;
  const pct = total ? Math.min(100, (count / total) * 100) : 0;
  const rate = job.rate > 0 ? job.rate.toFixed(2) + '/s' : '—';
  const eta = job.eta == null ? '—' : fmtTime(job.eta);
  const active = job.status === 'running' || job.status === 'paused';
  const buttonLabel = job.status === 'running' ? 'Pause'
    : (job.status === 'paused' ? 'Resume' : 'Start');
  const action = job.status === 'running' ? 'pause' : 'start';
  const b = job.breakdown || {};
  let detail = `success ${(b.success ?? count).toLocaleString()}`;
  if (job.name === 'comments') {
    detail += ` · with comments ${(b.with_comments || 0).toLocaleString()} · no comments ${(b.no_comments || 0).toLocaleString()} · errors/429 ${(b.errors || 0).toLocaleString()}`;
  } else if (job.name === 'ads') {
    detail += ` · non-ads ${(b.non_ads || 0).toLocaleString()} · errors ${(b.errors || 0).toLocaleString()}`;
  } else if (job.name === 'download') {
    detail += ` · folders ${count.toLocaleString()} · missing media ${(b.missing_media || 0).toLocaleString()}`;
  } else if (job.name === 'covers') {
    detail += ` · missing ${(b.missing || 0).toLocaleString()}`;
  } else if (job.name === 'unsave') {
    detail += ` · errors/rate-limited ${(b.errors || 0).toLocaleString()} · remaining ${(b.remaining || 0).toLocaleString()}`;
    if (b.ineligible) detail += ` · still saved (not eligible) ${b.ineligible.toLocaleString()}`;
  } else {
    detail += ` · n/a ${(b.not_applicable || 0).toLocaleString()}`;
  }
  const statusClass = job.status === 'verified' ? 'verified'
    : (job.status === 'running' ? 'running' : (job.status === 'paused' ? 'paused' : ''));
  if (job.name === 'unsave') {
    const boxes = criteriaList.map(([key, label]) =>
      `<label><input type="checkbox" data-crit="${key}" ${unsaveCriteria[key] ? 'checked' : ''}> ${label}</label>`
    ).join('');
    return `<div class="task" style="grid-column:1/-1">
      <div class="t"><span>${job.label}</span><b>${count.toLocaleString()} / ${total.toLocaleString()}</b></div>
      <div class="tb"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="n">${pct.toFixed(1)}% · rate ${rate} · eta ${eta} <span class="status ${statusClass}">${job.status}</span></div>
      <div class="n">${detail}</div>
      <div class="criteria">${boxes}</div>
      <div class="jobBtns">
        <button data-job="unsave" data-action="preview">Validate selected</button>
        <button data-job="unsave" data-action="unsave">Unsave matching</button>
        ${job.status === 'running' ? '<button data-job="unsave" data-action="pause">Pause</button>' : ''}
      </div>
      <div class="n" id="unsavePreview">${unsavePreview}</div>
    </div>`;
  }
  return `<div class="task">
    <div class="t"><span>${job.label}</span><b>${count.toLocaleString()} / ${total.toLocaleString()}</b></div>
    <div class="tb"><i style="width:${pct.toFixed(1)}%"></i></div>
    <div class="n">${pct.toFixed(1)}% · rate ${rate} · eta ${eta} <span class="status ${statusClass}">${job.status}</span></div>
    <div class="n">${detail}</div>
    <div class="jobBtns">
      <button data-job="${job.name}" data-action="${action}" ${job.available ? '' : 'disabled'}>${buttonLabel}</button>
    </div>
  </div>`;
}

async function refreshLog(d){
  try{
    if (logName === 'download' && d.tail){
      $('log').textContent = d.tail.join('\\n');
    } else {
      const r = await fetch('/api/log?name=' + encodeURIComponent(logName) + '&lines=160', {cache:'no-store'});
      const j = await r.json();
      $('log').textContent = (j.lines || []).join('\\n');
    }
  }catch(e){}
}

async function tick(){
  try{
    const r = await fetch('/api/status', {cache:'no-store'});
    const d = await r.json();
    $('paths').textContent = d.data_dir + '  ·  ' + d.log_path;
    $('folders').textContent = d.folders.toLocaleString();
    const pos = d.position || 0;
    $('position').textContent = pos ? `list position ${pos}${d.shortcode ? ' · ' + d.shortcode : ''}` : 'waiting for first post…';
    $('elapsed').textContent = fmtTime(d.elapsed);
    $('rate').textContent = fmtRate(d.rate);
    $('eta').textContent = d.eta == null ? '—' : fmtTime(d.eta);
    $('cDownloaded').textContent = d.folders.toLocaleString();
    $('cSize').textContent = d.size_human;
    $('cFiles').textContent = d.completed_total ? (d.completed_total + ' in metadata') : 'media folders';
    $('cFailed').textContent = d.failed;
    $('cFailed').className = 'v ' + (d.failed ? 'bad' : 'ok');
    $('cCurrent').textContent = d.shortcode || '—';
    $('cIndex').textContent = pos ? '#' + pos : '';

    const total = d.completed_total ?? d.reference_total;
    const pct = total ? Math.min(100, (d.folders / total) * 100) : 0;
    if (d.status === 'running' && !total){
      $('barwrap').classList.add('indet');
    } else {
      $('barwrap').classList.remove('indet');
      $('bar').style.width = pct.toFixed(1) + '%';
    }
    $('progressLabel').textContent = total ? pct.toFixed(1) + '%' : 'in progress';
    $('totalLabel').textContent = total ? `of ${total.toLocaleString()} ${d.completed_total ? 'total' : '(previous run)'}` : 'total unknown';

    const c = d.comments || {};
    $('taskGrid').innerHTML = (d.jobs || []).map(taskRow).join('');

    if (!$('taskGrid').onclick) {
      $('taskGrid').addEventListener('change', (event) => {
        const box = event.target.closest('input[data-crit]');
        if (!box) return;
        unsaveCriteria[box.dataset.crit] = box.checked;
        localStorage.setItem(UNSAVE_KEY, JSON.stringify(unsaveCriteria));
      });
      $('taskGrid').onclick = async (event) => {
        const btn = event.target.closest('button[data-job]');
        if (!btn) return;
        const name = btn.dataset.job;
        const action = btn.dataset.action;
        const selected = criteriaList
          .map(([key]) => key)
          .filter((key) => unsaveCriteria[key]);
        if (name === 'unsave' && action === 'preview') {
          if (!selected.length) { unsavePreview = 'Select at least one checkbox.'; tick(); return; }
          btn.disabled = true;
          unsavePreview = 'Validating…';
          tick();
          try {
            const r = await fetch('/api/unsave/preview?criteria=' + encodeURIComponent(selected.join(',')), {cache:'no-store'});
            const j = await r.json();
            if (j.error) unsavePreview = 'Error: ' + j.error;
            else {
              const f = Object.entries(j.failures || {}).map(([k,v]) => k + ' ' + v).join(' · ');
              unsavePreview = `matched ${j.matched.toLocaleString()} / ${j.scanned.toLocaleString()} · ${f || 'all pass'}`;
            }
          } catch (e) { unsavePreview = 'Preview failed.'; }
          tick();
          return;
        }
        if (name === 'unsave' && action === 'unsave') {
          if (!selected.length) { unsavePreview = 'Select at least one checkbox.'; tick(); return; }
          if (!confirm('Unsave matching posts using: ' + selected.join(', ') + '?')) return;
          btn.disabled = true;
          try {
            const r = await fetch('/api/unsave/run', {
              method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({criteria:selected.join(',')}),
            });
            const j = await r.json();
            unsavePreview = j.ok ? 'Unsave started.' : ('Error: ' + j.error);
          } catch (e) { unsavePreview = 'Unsave request failed.'; }
          tick();
          return;
        }
        if (action === 'pause' || action === 'start') {
          btn.disabled = true;
          try {
            const r = await fetch('/api/job', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({name, action}),
            });
            const j = await r.json();
            if (!j.ok) console.warn(j.message);
          } catch (e) {}
          setTimeout(tick, 500);
        }
      };
    }

    if (!logCatalogLoaded && d.logs){
      const sel = $('logSelect');
      sel.innerHTML = '<option value="all">all logs</option>' +
        d.logs.map(x => `<option value="${x.name}">${x.name}${x.exists ? '' : ' (missing)'}</option>`).join('');
      sel.value = logName;
      sel.onchange = () => { logName = sel.value; refreshLog(d); };
      logCatalogLoaded = true;
    }

    const dot = $('dot');
    dot.className = 'dot ' + (d.status === 'running' ? 'run' : (d.status === 'complete' ? 'done' : 'stop'));
    $('state').textContent = d.status === 'running'
      ? `running · pid ${d.pid}`
      : (d.status === 'complete' ? 'complete' : 'stopped');

    if (logName === 'download' && d.tail.join('\\n') !== lastTail.join('\\n')){
      lastTail = d.tail;
      $('log').textContent = d.tail.join('\\n');
      $('log').scrollTop = $('log').scrollHeight;
    } else if (logName !== 'download') {
      await refreshLog(d);
      $('log').scrollTop = $('log').scrollHeight;
    }
    $('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
  }catch(e){
    $('state').textContent = 'dashboard offline';
    $('dot').className = 'dot stop';
  }
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    data_dir: Path
    log_path: Path
    reference_total: int
    db_path: Optional[Path]
    comments_log: Optional[Path]

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter server
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, b'{"ok":false,"error":"invalid json"}', "application/json")
            return
        if self.path.startswith("/api/unsave/run"):
            criteria = payload.get("criteria", "")
            if not criteria:
                body = b'{"ok":false,"error":"select at least one criterion"}'
                self._send(400, body, "application/json")
                return
            log = Path("/tmp/opencode/unsave.log")
            log.parent.mkdir(parents=True, exist_ok=True)
            handle = open(log, "ab", buffering=0)
            subprocess.Popen(
                [PY, "unsave_validated.py", "--run", "--criteria", criteria,
                 "--confirm"],
                cwd=REPO, stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            handle.close()
            body = json.dumps({"ok": True, "message": "unsave started",
                               "criteria": criteria}).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if not self.path.startswith("/api/job"):
            self._send(404, b"not found", "text/plain")
            return
        name = payload.get("name", "")
        action = payload.get("action", "start")
        pipeline = db_progress(self.db_path) if self.db_path else {}
        folders, _ = dir_stats(self.data_dir)
        specs = job_specs(pipeline, folders)
        if action == "pause":
            ok, message = pause_job(specs, name)
        else:
            ok, message = start_job(specs, name)
        body = json.dumps({"ok": ok, "message": message, "name": name,
                           "action": action}).encode("utf-8")
        self._send(200 if ok else 409, body, "application/json")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/status"):
            status = build_status(
                self.data_dir,
                self.log_path,
                self.reference_total,
                self.db_path,
                self.comments_log,
            )
            body = json.dumps(status).encode("utf-8")
            self._send(200, body, "application/json")
        elif self.path.startswith("/api/unsave/preview"):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            criteria = (query.get("criteria", [""])[0] or "").strip()
            if not criteria:
                self._send(400, b'{"error":"select a criterion"}',
                           "application/json")
                return
            try:
                output = subprocess.check_output(
                    [PY, "unsave_validated.py", "--preview",
                     "--criteria", criteria],
                    cwd=REPO, text=True, timeout=240,
                )
                body = output.encode("utf-8")
                self._send(200, body, "application/json")
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self._send(500, body, "application/json")
        elif self.path.startswith("/api/log"):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            name = (query.get("name", ["download"])[0] or "download")
            lines = max(10, min(400, int(query.get("lines", ["160"])[0])))
            catalog = {
                item["name"]: Path(item["path"])
                for item in log_catalog(self.log_path, self.comments_log)
            }
            chunks = []
            if name == "all":
                for item in log_catalog(self.log_path, self.comments_log):
                    path = Path(item["path"])
                    if not path.is_file():
                        continue
                    try:
                        tail = path.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()[-40:]
                    except OSError:
                        continue
                    chunks.append(f"===== {item['name']} =====")
                    chunks.extend(tail)
            else:
                path = catalog.get(name)
                if path and path.is_file():
                    try:
                        chunks = path.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()[-lines:]
                    except OSError:
                        chunks = [f"cannot read {path}"]
                else:
                    chunks = [f"log not found: {name}"]
            body = json.dumps({"name": name, "lines": chunks}).encode("utf-8")
            self._send(200, body, "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live download progress GUI")
    parser.add_argument("--data", default="data", help="Run folder (contains saved_media)")
    parser.add_argument("--log", default="/tmp/opencode/ig_1911.log", help="Downloader log file")
    parser.add_argument("--port", type=int, default=8766, help="HTTP port")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument(
        "--total", type=int, default=971, help="Reference total from the previous run"
    )
    parser.add_argument(
        "--db", default="data/archive.db", help="Archive SQLite database"
    )
    parser.add_argument(
        "--comments-log", default="/tmp/opencode/comments_all.log",
        help="Comment job log",
    )
    args = parser.parse_args()

    data_dir = Path(args.data).expanduser().resolve()
    if (data_dir / "saved_media").is_dir():
        data_dir = data_dir / "saved_media"
    Handler.data_dir = data_dir
    Handler.log_path = Path(args.log).expanduser()
    Handler.reference_total = args.total
    Handler.db_path = Path(args.db).expanduser().resolve()
    Handler.comments_log = Path(args.comments_log).expanduser()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Progress dashboard: http://{args.host}:{args.port}/")
    print(f"  data: {data_dir}")
    print(f"  log:  {Handler.log_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
