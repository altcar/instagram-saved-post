# HANDOFF 2 — unified accounts, RedNote parity, persistence

## What changed

### Unified hub on 8891 (Workers-ready)

`hub/` is a static shell with a shared social rail:

```
/instagram  /rednote  /threads  /telegram  /progress
```

Locally it runs on `http://127.0.0.1:8891/` via `hub/server.py`; on
workers.dev deploy `hub/public/` with `hub/wrangler.jsonc`. Backends are iframed
and configured in `hub/public/config.js` (galleries use `?embed=1` to hide their
inner rail). The progress dashboard is therefore a separate backend iframe, not
frontend code. `serve_local.sh` starts the hub on boot.

### X / Twitter source + 8892 gallery

- The same `ThreadsArchive` adapter also reads `x-archiver/data/archive.db`
  (`platform="twitter"`, domain `x.com`). It handles the flat X comments
  schema, `unsave_status` saved-state filters, comment media, text-only posts
  and the X scraper `/api/state`.
- Source id `twitter` (`source=twitter`), label "X / Twitter".
- A second gallery instance runs on **8892**; `/gallery/?source=threads&embed=1`
  and `/gallery/?source=twitter&embed=1` serve the shared UI for those archives.
- 8771 (Threads) and 8772 (X) now redirect to the 8892 gallery. Their downloaders
  keep running; old dashboards are backed up under each project’s `static/`.
- The 8891 hub also has a `/twitter` route and account selector entry.

### Threads source

- `threads_source.py` reads `threads-archiver/data/archive.db` + `data/media`
  read-only and exposes the normalised gallery API (posts, media, comments,
  facets, categories, notes, tags, order, live scraper progress).
- It is registered as the builtin `threads` source; `/threads` on the hub loads
  `8770/?source=threads&embed=1` with the standard UI.
- The old 8771 page now redirects to `http://127.0.0.1:8892/gallery/?source=threads&embed=1` while the
  Threads downloader keeps running; its original UI is backed up at
  `threads-archiver/static/legacy-threads.html`.
- Sidecar metadata: `data/threads_meta.json` (categories, assignments, notes,
  tags, manual order), included in the gist/D1 cloud mirror.
- Text-only Threads posts render their caption inside the card thumbnail area
  (`NO MEDIA · TEXT POST`) instead of a broken/placeholder image.
- Every card shows its comment count (`💬 N`).
- Threads drawer is a wide split layout: post/media on the left, a large
  independently scrollable comments panel on the right. Comment images/videos
  are shown inline; URLs are clickable and Threads/Instagram post/share links
  render as “linked post ↗”.


### Telegram

- The 8799 app and its UI are untouched.
- It is available at `8891/telegram` inside the hub shell (hub rail provides the
  shared nav). The main gallery also has a Telegram service button pointing at
  the hub route.


### One gallery UI for every source

`/` and `/rednote` now serve the same `web/index.html` + `web/app.js` shell.
Every source (Instagram account, RedNote archive, future platform) has the same
grid/list/post/live views, search, creator filter, tags, category registry,
"only categories" filters, header filter counts, saved-status filters, sorts,
drawer, comments, multi-select, drag-to-category, manual reorder and live
progress.

Source ids come from:

- `accounts.py` / `config/accounts.json` (all accounts whose archive DB exists,
  even when disabled for downloading; `enabled: false` is shown as "sync off");
- UI-only additions in `data/accounts.json` (used for RedNote roots and extra
  galleries);
- a built-in RedNote source.

`GET /api/accounts` returns the switcher data. `?source=<id>` selects a source;
`/rednote` defaults to the RedNote source. `nav.socialBar` is rendered from that
API and contains no x.com/instagram.com links.

### RedNote parity

`rednote_source.py` adapts the folder-per-note RedNote archive to the same
normalised API as Instagram:

```
GET  /api/posts?source=...      GET  /api/facets?source=...
GET  /api/post/{id}?source=...  GET  /api/comments/{id}?source=...
POST /api/post/{id}             POST /api/bulk_category
POST /api/reorder               GET/POST /api/categories
GET  /api/live?source=...
```

It supports carousel media (images + live clips + video), comments with
replies, categories/assignments, personal notes/tags, saved-vs-unsaved filters,
stable ids (`ui_ids.json`), manual order (`order.json`) and per-note metadata
(`meta.json`).

### Category registry

- Instagram: new `categories (name, position, updated_at)` table, seeded from
  the real `post_categories` list and kept in sync when classifiers add names.
- RedNote: `categories.json` with `order` and `assignments`.
- UI: hover a category for ▲ ▼ rename and delete; `+ add`; the "Only
  categories" filter and drawer both use this ordered registry.
- "Uncategorised first" was removed from Sort and is now the
  `uncategorised only` filter. The legacy uncategorised sort remains in the
  backend for compatibility but is not exposed.

### Online persistence (active)

`cloud_store.py` mirrors personal state (category order, assignments, notes,
tags, RedNote metadata/order) to a remote store. This install is configured to
use a **secret GitHub Gist** (id stored in `data/cloud.json`) and gets its
token from `gh auth token` (fallback `GITHUB_TOKEN`). Providers: `gist`,
`github`, `d1`, `file`, `none`. See `CLOUD_SYNC.md`.

Sidebar shows the provider and last push. Click to push, shift-click to pull.
`POST /api/cloud {"op":"push"|"pull"}`. Edits push automatically after ~1.5 s.

### Data repairs

- `normalize_sort_order.py`: dense unique `sort_order` without changing the
  current visual order (fixed NULLs, negative/epoch values and ties).
- `repair_ui_data.py --apply`: relinked covers, fixed `cover_ok`, cleared two
  numeric owner values, excluded `cover.jpg` from carousel media. Remaining
  posts without media: 3 metadata-only folders.
- `repair_duplicate_covers.py --apply`: replaced 49 byte-identical video covers
  with a frame from each post's own video and cleared the thumb cache. One
  duplicate group remains: posts 3022/3234 genuinely contain the same image.
- Thumbnail generation no longer substitutes another carousel slide when a
  slide is flat; only cover/video-poster cases fall back. Per-index thumbnails
  are distinct.
- Owner validation accepts real handles that start with digits, rejects numeric
  ids/shortcodes/placeholders.
- Comments are normalised to `{owner,text,created_at,likes,replies}` and
  paginated through `/api/comments/{id}` for both sources.

### Live progress

`/api/live?source=...` returns stat cards + progress + log + actions.
The Live tab now contains a **native port of the 8766 dashboard** for every
Instagram source (`/api/dashboard`, `/api/dashboard/log`,
`/api/dashboard/job`, `/api/dashboard/unsave/preview|run`) — no iframe needed.
It includes every 8766 element:

- status dot + running/complete/stopped, pid, elapsed, rate, ETA;
- posts-saved hero, list position/shortcode, determinate/indeterminate bar;
- Downloaded / On disk / Failures / Current cards;
- all 11 pipeline tasks (download, comments, fast, clip, caption, ocr,
  transcribe, fuse, covers, ads, unsave) with counts, totals, percent, rate,
  ETA, status, breakdown detail and Start/Pause/Resume;
- eight unsave criteria, Validate selected, Unsave matching, Pause and the
  preview result (persisted in the same localStorage key as 8766);
- log selector (all 14 catalog entries + "all" for the legacy archive, or
  runner/pipeline for pipeline accounts), live tail, updated time;
- paths, offline state, and a 2 s refresh with an overlap guard.

The legacy archive has full job controls. Accounts managed by
`accounts.py`/`run_accounts.py` get the same dashboard **read-only** (Start/
Pause/Unsave disabled) and read `runner.log` / `pipeline.log`. RedNote keeps its
native job controls. `verify_gallery.py` compares the native dashboard job list
and keys against `http://127.0.0.1:8766/api/status`.

### Instant uncategorised removal

When `uncategorised only` is active and a post is assigned to a category (drag,
drop, or drawer selector), the card is removed from `state.items` and the DOM
immediately, the count/offset are decremented, and the next page is fetched to
fill the gap. No reload or server round-trip wait.

### Scheduled jobs

Cron already runs `daily_update.sh` daily at 04:30. The script now also runs the
sort/cover repairs and posts a cloud push. Systemd-timer alternative is in
`systemd/` (`install-user-units.sh`); enable either cron or the timer, not both.

### Hugging Face / Pages

- `publish/hf_upload.py` builds a manifest and uploads it (and optionally
  media) to a Hugging Face dataset. Dry-run by default.
- The static build (`publish/build_static.py`) now emits per-slide thumbs,
  `filter_counts`, `category_list` and static `/api/accounts`, `/api/categories`,
  `/api/live`, comments pagination. Static editing keeps local overrides and now
  has Export/Import buttons for personal notes/categories.

## Verification

`python verify_gallery.py` passes:

- all sources (Instagram accounts, RedNote, Threads) and filter counts match `/api/posts`;
- drawer + comments endpoints for each source;
- Threads media/comments/category/notes sidecar round-trip;
- 7 repaired covers are distinct (SHA-256);
- category add/rename/remove on Instagram + RedNote + Threads;
- notes/tags save and restore (RedNote + Threads);
- cloud provider status (`gist`, online);
- `/`, `/rednote`, hub `/instagram`, `/rednote`, `/threads`, `/telegram`,
  `/progress`, hub `/health`, and the 8766 dashboard return 200.

Manual browser checks: account switcher (Instagram main, prettysushey,
RedNote, Threads), RedNote 19-slide drawer, Threads standard gallery/drawer,
drag-to-category, category rename/reorder, Live tab on all sources, hub account
selector, and Telegram UI unchanged inside the hub frame.


## Caveats

- `prettysushey` is being downloaded by another process; its post count grows
  continuously. That is expected.
- Accounts are read from `accounts.py`; disabled accounts still appear if their
  DB exists so old archives remain browsable. Removal of pipeline accounts must
  be done in `accounts.py` (the UI refuses it).
- The gist is secret/unlisted, not encrypted; use D1 if notes must stay private.
- 3 posts are metadata-only with no media on disk; they show the placeholder.
- 2 posts (3022/3234) share one genuine image; the covers stay identical.
