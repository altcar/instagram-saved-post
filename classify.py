#!/usr/bin/env python3
"""Categorise and enrich saved posts.

Stages (each is incremental and safe to re-run):

  fast        caption/hashtag/owner heuristics -> category
  clip        CLIP zero-shot on the cover -> category + confidence
  caption     BLIP vision caption -> vision_caption
  ocr         tesseract on the cover -> ocr_text
  transcribe  Whisper on videos -> transcript

Heuristics are deliberately conservative; the vision stage is what rescues
clickbait or misleading captions ("Japanese footstep" style memes, car specs,
etc.) because it looks at the image, not the author's words.

Examples:
    python classify.py fast --db data/archive.db --media data/posts
    python classify.py clip --db data/archive.db --media data/posts
    python classify.py caption --limit 200
    python classify.py ocr --limit 500
    python classify.py transcribe --limit 50
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from webapp import Archive

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover
    imageio_ffmpeg = None

# ---------------------------------------------------------------------------
# heuristic rules
# ---------------------------------------------------------------------------

RULES: List[Tuple[str, List[str]]] = [
    ("ad", [
        "sponsored", "#ad", "advertisement", "paid partnership", "shop now",
        "link in bio", "discount", "promo code", "buy now", "order now",
        "limited time", "sale", "offer", "available now", "dm me", "website",
        "affiliate", "collab", "brand ambassador", "black friday", "cyber monday",
    ]),
    ("ai-skills", [
        "claude", "chatgpt", "openai", "gpt", "prompt", "prompting", "agent",
        "mcp", "cursor", "copilot", "llm", "claude code", "skill", "automation",
        "workflow", "n8n", "langchain", "rag", "vibe coding", "codex",
    ]),
    ("ai-robotics", [
        "robot", "robotics", "humanoid", "boston dynamics", "unitree", "sim2real",
        "reinforcement learning", "simulation", "ros", "actuator", "servo",
        "robot arm", "quadruped", "locomotion", "manipulation", "digital twin",
    ]),
    ("erotic", [
        "nude", "nudity", "nsfw", "erotic", "onlyfans", "explicit", "18+", "xxx",
        "adult content", "lingerie", "boudoir", "sensual",
    ]),
    ("sexy", [
        "sexy", "bikini", "swimwear", "model", "glamour", "hot girl", "body",
        "fitness model", "attractive", "beautiful woman", "dress", "outfit",
    ]),
    ("art", [
        "art", "artist", "artwork", "painting", "illustration", "drawing",
        "sketch", "sculpture", "concept art", "digital art", "3d render",
        "blender", "character design", "animation", "gallery", "mural",
    ]),
    ("music", [
        "music", "song", "track", "album", "spotify", "soundcloud", "beat",
        "lyrics", "official video", "musician", "dj", "remix", "concert",
    ]),
    ("meme", [
        "meme", "funny", "lol", "lmao", "when you", "pov", "relatable",
        "tag someone", "me when", "cursed", "shitpost", "😂", "🤣",
    ]),
    ("food", [
        "recipe", "food", "cook", "cooking", "restaurant", "eat", "dish",
        "coffee", "dessert", "baking", "chef", "meal",
    ]),
    ("travel", [
        "travel", "trip", "hotel", "flight", "destination", "visit", "city",
        "beach", "mountains", "itinerary", "vacation", "holiday",
    ]),
    ("fitness", [
        "workout", "gym", "fitness", "training", "exercise", "muscle",
        "calisthenics", "running", "lifting", "diet", "protein",
    ]),
    ("tech", [
        "tech", "gadget", "review", "iphone", "android", "laptop", "gpu",
        "cpu", "software", "hardware", "coding", "programming", "developer",
    ]),
    ("fashion", [
        "fashion", "style", "outfit", "streetwear", "designer", "clothing",
        "collection", "lookbook", "runway",
    ]),
    ("design", [
        "design", "ux", "ui", "typography", "branding", "graphic design",
        "product design", "architecture", "interior",
    ]),
    ("news", [
        "breaking", "news", "report", "politics", "government", "election",
        "war", "economy", "market", "alert",
    ]),
]

STRONG_AD = {
    "sponsored", "paid partnership", "#ad", "shop now", "buy now",
    "discount", "promo code", "limited time offer", "black friday",
}

_hashtag_re = re.compile(r"#([\w]+)", re.UNICODE)


def heuristic_category(caption: str, hashtags: List[str], owner: Optional[str],
                       likes: int = 0) -> Tuple[str, float, List[str]]:
    text = (caption or "").lower()
    text += " " + " ".join(hashtags or []).lower()
    text += " " + (owner or "").lower()
    scores: Dict[str, int] = {}
    for name, words in RULES:
        hits = [w for w in words if w in text]
        if hits:
            scores[name] = len(hits)
    if not scores:
        return "other", 0.2, []
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, score = ranked[0]
    confidence = min(0.9, 0.45 + 0.12 * score)
    tags = [f"{best}"]

    # strong ad / partnership overrides softer art/music signals
    if any(word in text for word in STRONG_AD):
        best, confidence = "ad", max(confidence, 0.8)
    # popular artist: art with unusually high engagement
    if best == "art" and likes >= 10000:
        best = "artist"
        tags.append("popular-artist")
        confidence = max(confidence, 0.7)
    return best, confidence, tags


# ---------------------------------------------------------------------------
# data helpers
# ---------------------------------------------------------------------------

def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    return conn


def rows_for(conn: sqlite3.Connection, stage: str, limit: Optional[int],
             filter_mode: str = "all", redo: str = "none") -> List[sqlite3.Row]:
    where = {
        "fast": "1=1",
        "clip": "(category_source IS NULL OR category_source NOT LIKE '%clip%')",
        "caption": "vision_caption IS NULL",
        "ocr": "ocr_text IS NULL",
        "transcribe": "transcript IS NULL AND is_video = 1",
    }[stage]
    if stage == "clip" and redo == "video":
        where = "is_video = 1"
    if filter_mode == "other":
        where += " AND (category = 'other' OR category IS NULL)"
    elif filter_mode == "lowconf":
        where += " AND (category IN ('other','meme') OR category_confidence < 0.45 OR category IS NULL)"
    sql = f"SELECT * FROM posts WHERE {where} ORDER BY sort_order"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def raw_flags(raw_path: Path) -> Dict[str, Dict[str, Any]]:
    flags: Dict[str, Dict[str, Any]] = {}
    if not raw_path.exists():
        return flags
    with raw_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            media = item.get("media") or item
            code = media.get("code")
            if not code:
                continue
            flags[code] = {
                "music": bool(media.get("music_metadata")),
                "paid": bool(media.get("is_paid_partnership")),
                "product": media.get("product_type"),
            }
    return flags


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------

def stage_fast(conn: sqlite3.Connection, limit: Optional[int], raw_path: Path) -> None:
    flags = raw_flags(raw_path)
    rows = rows_for(conn, "fast", limit)
    print(f"fast: {len(rows)} posts")
    for row in rows:
        caption = row["caption"] or ""
        hashtags = json.loads(row["hashtags"] or "[]")
        category, confidence, tags = heuristic_category(
            caption, hashtags, row["owner_username"], row["likes"] or 0
        )
        extra = flags.get(row["shortcode"], {})
        if extra.get("paid"):
            category, confidence, tags = "ad", 0.85, ["ad"]
        if extra.get("music") and category == "other":
            category, confidence, tags = "music", 0.5, ["music"]
        conn.execute(
            "UPDATE posts SET category=?, category_confidence=?, category_source=? WHERE id=?",
            (category, confidence, "fast", row["id"]),
        )
    conn.commit()


def _archive(args: argparse.Namespace) -> Archive:
    return Archive(Path(args.db), Path(args.media), Path(args.cache))


def video_duration(video: Path) -> Optional[float]:
    if imageio_ffmpeg is None:
        return None
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(video)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except Exception:
        return None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def midpoint_frame(
    archive: Archive, row: sqlite3.Row, width: int = 480
) -> Optional[Path]:
    """Vision frame: midpoint of the video, or the cover/thumbnail for images."""
    if not row["is_video"]:
        return archive.thumbnail(row["id"], width)
    cache_dir = Path(archive.cache_dir) / "mid"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{row['id']}.jpg"
    if target.is_file() and target.stat().st_size > 0:
        return target
    media = archive._media_paths(row["id"])
    video = next(
        (archive.media_root / path for path, kind in media if kind == "video"),
        None,
    )
    if video is None or not video.is_file() or imageio_ffmpeg is None:
        return archive.thumbnail(row["id"], width)
    duration = video_duration(video)
    midpoint = duration / 2 if duration else 1.0
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        subprocess.run(
            [
                ffmpeg, "-y", "-loglevel", "error",
                "-ss", f"{midpoint:.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", f"scale={width}:-1", str(target),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except Exception:
        return archive.thumbnail(row["id"], width)
    return target if target.is_file() else archive.thumbnail(row["id"], width)


def stage_clip(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    import torch
    from transformers import CLIPModel, CLIPProcessor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"clip: device={device}")
    model_name = "openai/clip-vit-base-patch32"
    model = CLIPModel.from_pretrained(model_name).to(device).eval()
    processor = CLIPProcessor.from_pretrained(model_name)

    prompts = [
        ("art", "a piece of artwork, painting, illustration, digital art, character design"),
        ("artist", "artwork by a popular artist, gallery portfolio piece"),
        ("sexy", "a glamorous photo of an attractive woman in a bikini or stylish outfit"),
        ("erotic", "explicit erotic nudity or adult content"),
        ("ai-robotics", "a robot, robotic arm, humanoid robot, or AI robotics simulation"),
        ("ai-skills", "a screenshot of an AI coding assistant tutorial, Claude, ChatGPT or agent workflow"),
        ("music", "a music video, album cover, or musician performing"),
        ("meme", "an internet meme, funny image with large text captions"),
        ("ad", "an advertisement, product promotion, sponsored brand post, shop now"),
        ("food", "food, a meal, cooking, or a restaurant dish"),
        ("travel", "a travel destination, city, beach, hotel or landscape"),
        ("fitness", "gym workout, fitness training, or exercise"),
        ("fashion", "fashion lookbook, streetwear outfit, or runway"),
        ("design", "graphic design, typography, branding or UI design"),
        ("tech", "a gadget, computer, software interface or technology review"),
        ("news", "a news report, politics, or current events"),
    ]
    text_inputs = processor(
        text=[f"a photo of {p}" for _, p in prompts], return_tensors="pt", padding=True
    ).to(device)
    with torch.no_grad():
        text_features = model.get_text_features(**text_inputs)
        if not isinstance(text_features, torch.Tensor):
            text_features = text_features.pooler_output
        text_features = text_features / text_features.norm(dim=-1, keepdim=True)

    archive = _archive(args)
    rows = rows_for(conn, "clip", args.limit, redo=getattr(args, "redo", "none"))
    id_to_row = {row["id"]: row for row in rows}
    print(f"clip: {len(rows)} posts")
    batch_size = args.batch
    from PIL import Image

    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        images = []
        ids = []
        for row in batch:
            thumb = midpoint_frame(archive, row, 360)
            if not thumb:
                continue
            try:
                images.append(Image.open(thumb).convert("RGB"))
                ids.append(row["id"])
            except Exception:
                continue
        if not images:
            continue
        inputs = processor(images=images, return_tensors="pt").to(device)
        with torch.no_grad():
            image_features = model.get_image_features(**inputs)
            if not isinstance(image_features, torch.Tensor):
                image_features = image_features.pooler_output
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            logits = 100.0 * image_features @ text_features.t()
            probs = logits.softmax(dim=-1).cpu()
        for i, post_id in enumerate(ids):
            index = int(probs[i].argmax())
            category = prompts[index][0]
            confidence = float(probs[i][index])
            row = id_to_row[post_id]
            prev = row["category"]
            prev_conf = row["category_confidence"] or 0
            # Strong ad signals and confident text labels win over a weak CLIP guess.
            if prev == "ad" and prev_conf >= 0.8:
                continue
            if confidence < 0.30 and prev and prev != "other":
                continue
            if category == "art" and (row["likes"] or 0) >= 10000:
                category = "artist"
            conn.execute(
                "UPDATE posts SET category=?, category_confidence=?, category_source=? WHERE id=?",
                (category, confidence, "clip", post_id),
            )
        conn.commit()
        print(f"  {min(start + batch_size, len(rows))}/{len(rows)}", flush=True)


def stage_caption(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    import torch
    from transformers import BlipForConditionalGeneration, BlipProcessor
    from PIL import Image

    device = "cuda" if torch.cuda.is_available() else "cpu"
    name = "Salesforce/blip-image-captioning-base"
    print(f"caption: device={device}")
    processor = BlipProcessor.from_pretrained(name)
    model = BlipForConditionalGeneration.from_pretrained(name).to(device).eval()
    archive = _archive(args)
    rows = rows_for(conn, "caption", args.limit, args.filter)
    print(f"caption: {len(rows)} posts")
    for i, row in enumerate(rows, 1):
        thumb = midpoint_frame(archive, row, 480)
        if not thumb:
            continue
        try:
            image = Image.open(thumb).convert("RGB")
            inputs = processor(images=image, return_tensors="pt").to(device)
            with torch.no_grad():
                out = model.generate(**inputs, max_new_tokens=48)
            caption = processor.decode(out[0], skip_special_tokens=True).strip()
            conn.execute(
                "UPDATE posts SET vision_caption=? WHERE id=?", (caption, row["id"])
            )
        except Exception as exc:
            print(f"  caption failed {row['shortcode']}: {exc}", file=sys.stderr)
        if i % 50 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}", flush=True)
    conn.commit()


def stage_ocr(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    archive = _archive(args)
    rows = rows_for(conn, "ocr", args.limit, args.filter)
    print(f"ocr: {len(rows)} posts")
    for i, row in enumerate(rows, 1):
        thumb = midpoint_frame(archive, row, 720)
        if not thumb:
            continue
        try:
            proc = subprocess.run(
                ["tesseract", str(thumb), "stdout", "--psm", "6"],
                capture_output=True, text=True, timeout=30,
            )
            text = " ".join(proc.stdout.split())[:4000]
            conn.execute("UPDATE posts SET ocr_text=? WHERE id=?", (text, row["id"]))
        except Exception as exc:
            print(f"  ocr failed {row['shortcode']}: {exc}", file=sys.stderr)
        if i % 100 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}", flush=True)
    conn.commit()


def stage_transcribe(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    import torch
    from transformers import pipeline
    import imageio_ffmpeg

    device = 0 if torch.cuda.is_available() else -1
    print(f"transcribe: device={device}")
    pipe = pipeline(
        "automatic-speech-recognition",
        model="openai/whisper-small",
        device=device,
    )
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    media_root = Path(args.media)
    rows = rows_for(conn, "transcribe", args.limit, args.filter)
    print(f"transcribe: {len(rows)} posts")
    tmp = Path(args.cache) / "audio"
    tmp.mkdir(parents=True, exist_ok=True)
    for i, row in enumerate(rows, 1):
        media = conn.execute(
            "SELECT path FROM post_media WHERE media_id=? AND kind='video' ORDER BY path LIMIT 1",
            (row["media_id"],),
        ).fetchone()
        if not media:
            continue
        video = media_root / media["path"]
        audio = tmp / f"{row['id']}.wav"
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(video),
                 "-ar", "16000", "-ac", "1", str(audio)],
                check=True, timeout=300,
            )
            result = pipe(str(audio), chunk_length_s=30)
            text = (result.get("text") or "").strip()[:8000]
            conn.execute("UPDATE posts SET transcript=? WHERE id=?", (text, row["id"]))
        except Exception as exc:
            print(f"  transcribe failed {row['shortcode']}: {exc}", file=sys.stderr)
        finally:
            try:
                audio.unlink()
            except OSError:
                pass
        if i % 20 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}", flush=True)
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify saved posts")
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--media", default="data/posts")
    parser.add_argument("--cache", default="data/.cache")
    parser.add_argument("--raw", default="data/raw/items.jsonl")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--filter", default="all", choices=["all", "other", "lowconf"])
    parser.add_argument("--redo", default="none", choices=["none", "video"])
    sub = parser.add_subparsers(dest="stage", required=True)
    for name in ("fast", "clip", "caption", "ocr", "transcribe"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=argparse.SUPPRESS)
        p.add_argument("--media", default=argparse.SUPPRESS)
        p.add_argument("--cache", default=argparse.SUPPRESS)
        p.add_argument("--raw", default=argparse.SUPPRESS)
        p.add_argument("--limit", type=int, default=argparse.SUPPRESS)
        p.add_argument("--batch", type=int, default=argparse.SUPPRESS)
        p.add_argument("--filter", default=argparse.SUPPRESS, choices=["all", "other", "lowconf"])
        p.add_argument("--redo", default=argparse.SUPPRESS, choices=["none", "video"])
    args = parser.parse_args()
    conn = connect(Path(args.db))
    if args.stage == "fast":
        stage_fast(conn, args.limit, Path(args.raw))
    elif args.stage == "clip":
        stage_clip(conn, args)
    elif args.stage == "caption":
        stage_caption(conn, args)
    elif args.stage == "ocr":
        stage_ocr(conn, args)
    elif args.stage == "transcribe":
        stage_transcribe(conn, args)
    conn.close()


if __name__ == "__main__":
    main()
