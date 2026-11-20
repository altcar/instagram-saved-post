# Saved Instagram Archive

This is the tooling around `data/<DDMM>/` runs: a SQLite index, a local browse
web app, cross-run dedup, cover downloading, a vision/transcription
classification pipeline, and a browser fallback for posts the normal API cannot
download.

## Why posts stay "Saved" after downloading

Downloading is read-only. Instagram only removes an item from Saved when you
explicitly unsave it (the heart/bookmark toggle). The downloader never touched
that state, which is why the saved page still showed everything.

To unsave after a successful download, add `--unsave`:

```bash
python download_saved.py --username shalevfrfr --sessionid "$IG_SESSIONID" \
  --out data/2011/saved_media --json data/2011/saved_posts.json \
  --db data/dedup.db --unsave
```

This is irreversible for the account, so it is off by default and should be
tested with `--limit` first.

## Folder layout (portable, one post per folder)

```
data/1911/
  saved_media/0001_Df7r0X2ivOB/
    Df7r0X2ivOB_20261119_180654.mp4
    cover.jpg
    caption.txt
    comments.json
    comments.txt
    meta.json
  archive.db          # SQLite index (fast listing/search)
  .cache/thumbs/      # 360 px thumbnails generated on demand
```

`caption.txt` and the comments files are plain files so a post can be moved or
copied without the database. The database is only an index: the web app reads
it instead of scanning 8,000 folders, which keeps an HDD quiet and the UI fast.

## Browse web app

```bash
python webapp.py --db data/1911/archive.db --media data/1911/saved_media \
  --cache data/1911/.cache --port 8770
```

Open <http://127.0.0.1:8770/>.

- Grid / list / post views.
- Infinite scroll, 60 cards per request.
- 360 px thumbnails, generated once and cached; full media only when opened.
- Search across captions, owners, notes, tags, vision captions and transcripts.
- Categories, notes, personal tags (autosaved), and drag-and-drop manual order
  (switch sort to "Manual order").

## Downloading / resuming

The downloader checks `data/dedup.db` by Instagram media id **before** any
download, and merges new records into an existing `saved_posts.json`:

```bash
python download_saved.py --username shalevfrfr --sessionid "$IG_SESSIONID" \
  --out data/1911/saved_media --json data/1911/saved_posts.json \
  --db data/dedup.db --skip-existing
```

`data/dedup.db` is the cross-run "already downloaded" list. Backfill it after
importing an old run:

```bash
python dedup.py backfill --run-db data/1911/archive.db --run 1911
```

## Covers

The saved API gives the real video cover. `fetch_covers.py` downloads them in
parallel and falls back to a video frame only when the cover is missing:

```bash
python fetch_raw.py --out data/1911/raw --media data/1911/saved_media \
  --db data/1911/archive.db --no-covers
python fetch_covers.py --raw data/1911/raw/items.jsonl \
  --media data/1911/saved_media --db data/1911/archive.db --workers 8
```

## Ads and API-hard posts

`browser_fetch.py` uses Chrome's cookies through Playwright, opens the post
page, captures the media GraphQL response, and saves video/images **and the
caption**:

```bash
python browser_fetch.py --shortcode DaSP3wRzyD3
python browser_fetch.py --missing --limit 20
```

This is the autonomous version of "read the HTML / use an online downloader".
Posts that Instagram has actually deleted (410/empty media response) cannot be
recovered by any client.

## Classification pipeline

```bash
python classify.py fast       # caption/hashtag/owner heuristics (seconds)
python classify.py clip       # CLIP zero-shot on the cover (vision, not caption)
python classify.py caption    # BLIP vision caption -> vision_caption
python classify.py ocr        # tesseract on the cover
python classify.py transcribe # Whisper on videos
```

`clip` is the important stage: it looks at the image, so clickbait or
misleading captions (Japanese footstep memes, car specs, etc.) are not trusted
alone. It fills `category`, `category_confidence` and `category_source`.
`--filter other` or `--filter lowconf` limits BLIP/OCR/Whisper to the posts
where vision actually changes the answer.

Categories currently include: `art`, `artist`, `sexy`, `erotic`, `ai-robotics`,
`ai-skills`, `meme`, `music`, `ad`, `food`, `travel`, `fitness`, `tech`,
`fashion`, `design`, `news`, `other`.

## Index maintenance

```bash
# rebuild/refresh the index from the run JSON + folders
python archive_db.py import --db data/1911/archive.db \
  --json data/1911/saved_posts.json --media data/1911/saved_media

# (re)write caption/comments/meta files
python archive_db.py write-files --db data/1911/archive.db \
  --media data/1911/saved_media

# regenerate saved_posts.json from the index (includes .webp media)
python archive_db.py export-json --db data/1911/archive.db \
  --json data/1911/saved_posts.json --media data/1911/saved_media

python archive_db.py stats --db data/1911/archive.db
```
