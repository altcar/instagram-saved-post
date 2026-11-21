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
import subprocess
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple

INDEX_RE = re.compile(r"(?:Downloading|Skip existing) (\d{4})_([A-Za-z0-9_-]+)")
DONE_RE = re.compile(r"Saved (\d+) entries")
FAIL_RE = re.compile(r"Failed to (?:download|serialise)")

_sampler: Deque[Tuple[float, int]] = deque(maxlen=240)
_cache: Dict[str, Tuple[float, int, int]] = {}


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def dir_stats(path: Path) -> Tuple[int, int]:
    """Return (number of post folders, total bytes), cached briefly."""
    key = str(path)
    now = time.time()
    cached = _cache.get(key)
    if cached and now - cached[0] < 4:
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


def running_process() -> Optional[Tuple[int, int]]:
    """Return (pid, elapsed_seconds) of the downloader, if it is running."""
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
        if "download_saved.py" not in args:
            continue
        command = args.split(None, 1)[0]
        if "python" in os.path.basename(command):
            return int(pid), int(elapsed)
    return None


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


def build_status(data_dir: Path, log_path: Path, reference_total: int) -> Dict[str, Any]:
    folders, size = dir_stats(data_dir)
    log = parse_log(log_path)
    process = running_process()
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

  <section class="grid">
    <div class="card"><div class="k">Downloaded</div><div class="v ok" id="cDownloaded">0</div><div class="n">unique folders</div></div>
    <div class="card"><div class="k">On disk</div><div class="v" id="cSize">0 B</div><div class="n" id="cFiles"></div></div>
    <div class="card"><div class="k">Failures</div><div class="v" id="cFailed">0</div><div class="n">logged errors</div></div>
    <div class="card"><div class="k">Current</div><div class="v" id="cCurrent" style="font-size:15px;word-break:break-all">—</div><div class="n" id="cIndex"></div></div>
  </section>

  <section class="log">
    <div class="loghead"><span>downloader log</span><span id="updated">—</span></div>
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

    const dot = $('dot');
    dot.className = 'dot ' + (d.status === 'running' ? 'run' : (d.status === 'complete' ? 'done' : 'stop'));
    $('state').textContent = d.status === 'running'
      ? `running · pid ${d.pid}`
      : (d.status === 'complete' ? 'complete' : 'stopped');

    if (d.tail.join('\\n') !== lastTail.join('\\n')){
      lastTail = d.tail;
      $('log').textContent = d.tail.join('\\n');
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

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter server
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/status"):
            status = build_status(
                self.data_dir, self.log_path, self.reference_total
            )
            body = json.dumps(status).encode("utf-8")
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
    args = parser.parse_args()

    data_dir = Path(args.data).expanduser().resolve()
    if (data_dir / "saved_media").is_dir():
        data_dir = data_dir / "saved_media"
    Handler.data_dir = data_dir
    Handler.log_path = Path(args.log).expanduser()
    Handler.reference_total = args.total

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
