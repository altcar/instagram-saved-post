# Prompt: port this Saved Archive gallery to Telegram or Threads

Use the **shared prompt** below, then paste the matching platform addendum.
Replace `<PLATFORM>` and `<ROOT>`.

---

## SHARED PROMPT (copy this first)

You are a senior full-stack/archive engineer. Port the complete functionality of
the local Saved Archive at `<ROOT>` to **<PLATFORM>**. This is a behaviour and
UX port, not a line-by-line copy: keep the same data model, feature set and
acceptance criteria, but adapt storage, authentication and interaction to the
platform.

### Read first

- `web/index.html`, `web/app.js`, `web/style.css` — the shared gallery UI.
- `webapp.py` — normalised API, source dispatch, live dashboard.
- `rednote_source.py` — the reference adapter for a non-Instagram platform.
- `accounts.py` / `config/accounts.json` — per-account registry and paths.
- `cloud_store.py` / `CLOUD_SYNC.md` — online persistence of categories/notes.
- `progress_gui.py` — the standalone progress dashboard that the Live tab ports.
- `HANDOFF2.md`, `ARCHIVE.md` — what has already been built and verified.
- `verify_gallery.py` — the acceptance harness. Extend it; do not weaken it.

Do not rewrite unrelated downloaders, RedNote code, or concurrent users'
changes. Work additively. Keep one shared frontend for all platforms.

### Non-negotiable feature parity

1. **Accounts/platforms registry + switcher**
   - Multiple accounts of the same platform and multiple platforms.
   - The switcher lives in `body > nav.socialBar`; no external social links.
   - Every source gets the same UI and URL state (`?source=<id>`).

2. **Views**: grid, list, post, live.

3. **Search** across captions/text, creator, personal notes, tags and any
   transcript/vision/OCR text. Debounced, server-side.

4. **Filters**
   - creator filter (click an owner);
   - tags;
   - category list + "only categories" checkboxes;
   - header checkboxes with live counts for: no video, no image, no ads,
     bad owner/ownerLink, bad cover, caption empty, caption error/429,
     bad shortcode, comments empty, comments error/429, still saved,
     already unsaved, **uncategorised only**.
   - Saved-status counts must match `/api/posts?flags=…` totals exactly.

5. **Sorts**: saved/manual order, newest post, oldest post, most liked, shuffle.
   "Uncategorised first" must remain a **filter**, not a sort. Manual reorder is
   persisted.

6. **Categories**
   - The registry is the user's real category list, ordered.
   - Add, rename, delete, reorder.
   - The sidebar and "only categories" use that registry, not raw classifier
     output.
   - Assign by multi-select + drag-to-category. When `uncategorised only` is
     active, assigning a post must remove its card from the current view
     immediately (decrement count/offset, refill the page) without a reload.

7. **Post drawer**
   - Every carousel item/album item (images and videos, correct order).
   - Caption/text, cover, creator link, date, likes, counts.
   - Comments (paginated, with replies where the platform has them).
   - Edit personal notes, tags and categories; edits persist and update the
     cloud mirror.

8. **Multi-select**: checkbox and drag-rectangle selection; drag selected posts
   to a category; clear selection; acts on all selected.

9. **Live dashboard** (full parity with `progress_gui.py`)
   - status dot/state, pid, elapsed, rate, ETA;
   - posts-saved hero, list position, current item, determinate/indeterminate
     progress bar;
   - Downloaded / On disk / Failures / Current cards;
   - all pipeline jobs with count/total/percent/rate/ETA/status/detail and
     Start/Pause/Resume (download, comments, classification, covers, ads,
     unsave; adapt the job list to the new platform);
   - unsave/remove criteria checkboxes, Validate, Run, Pause and preview result;
   - log selector with every log plus "all", live tail, updated time, paths;
   - auto-refresh about every 2 s; graceful offline state.
   - The Live tab must not depend on an iframe.
   - Extra accounts may expose the same dashboard read-only when a separate
     runner owns their jobs; never let a Start button silently target another
     account's data tree.

10. **Data quality**
    - distinct thumbnails per carousel index (never substitute another slide);
    - no duplicate/wrong covers; cover versioning in URLs;
    - stable ordering, no NULL/duplicate sort keys;
    - valid owner links only (accept real digit-leading handles, reject numeric
      ids/shortcodes/placeholders);
    - consistent comment owner/time/text and pagination;
    - report metadata-only/missing-media posts rather than showing broken cards.

11. **Online persistence**
    - Local store is authoritative.
    - Mirror category registry/order, assignments, notes and tags to a free
      online store (GitHub secret gist is the current implementation), with
      Cloudflare D1 as fallback, plus a local file mirror.
    - Auto-push after edits; explicit pull; status in the sidebar.
    - Tokens come from env/`gh auth token`, never committed.

12. **Operations**
    - Per-account isolation and a registry like `accounts.py`.
    - Scheduled sync via cron or a systemd timer; job progress survives restarts.
    - Optional Hugging Face export and GitHub Pages notes export/import.

### Required architecture

- One `Source` adapter interface used by the API:
  `list_posts`, `facets`, `get_post`, `comments`, `update_post`,
  `bulk_category`, `reorder`, `category_list`, `category_op`.
- Normalised item fields: `id, shortcode, owner, owner_url, permalink, caption,
  caption_full, likes, taken_at, is_video, typename, location, categories,
  tags, notes, media[{i,kind,url,thumb}], thumb, cover, saved_state,
  comments_count, has_comments`.
- Normalised detail adds `comments_data {items[{id,owner,text,created_at,
  likes,replies}], total, hasMore, offset}`.
- Endpoints stay source-scoped:
  `/api/posts`, `/api/facets`, `/api/post/{id}`, `/api/comments/{id}`,
  `/api/categories` (GET/POST), `/api/bulk_category`, `/api/reorder`,
  `/api/live`, `/api/dashboard*`.
- Stable platform ids; store a mapping if the platform has no shortcode.
- Schema migrations are additive and idempotent.
- Never let one bad source kill the server; return JSON errors and retry
  transient DB locks.
- Share the frontend; platform differences belong in the adapter and small
  UI affordances, not a fork of `app.js`.

### Deliverables

1. Adapter module(s), schema/migrations, registry entries.
2. Wired API + UI, including Live tab and instant uncategorised removal.
3. Migration/import script for existing data.
4. Extended `verify_gallery.py` and any platform-specific tests.
5. Updated docs (`ARCHIVE.md`, `HANDOFF2.md`, `CLOUD_SYNC.md`) and exact run
   commands.
6. A parity checklist mapping every numbered item above to code/tests.
7. No regressions: the Instagram and RedNote sources must still pass the
   existing verification.

### Acceptance gates

- Counts from `/api/facets` equal `/api/posts?flags=<flag>` for every flag.
- Drawer shows all media and paginated comments for a sampled post.
- Category add/rename/reorder/delete round-trips and survives restart.
- Assigning while `uncategorised only` is active removes the card instantly.
- Every Live-tab element matches the standalone dashboard's data.
- Thumbnails for repaired multi-slide posts have distinct hashes.
- Owner links resolve; invalid owners render as unavailable.
- Online push happens automatically; a pull restores categories/notes.
- `/`, the platform route, and the progress dashboard return 200.
- Existing Instagram/RedNote verification still passes.

Ask before destructive migrations. Show the plan first if the platform has no
official saved-items API.

---

## PLATFORM ADDENDUM — TELEGRAM (message-based)

Tailor everything to Telegram's message/chat model:

- **Source data**: Saved Messages, a folder, a channel, a group, or a bot chat.
  Use MTProto (Telethon/Pyrogram) with a user session for Saved Messages and
  private chats; use a bot token only where the bot can read the chat. Do not
  assume an official "saved posts" API.
- **Identity**: stable key = `chat_id:message_id`. Also store
  `file_unique_id` for dedupe and `media_group_id` for albums. The "shortcode"
  field can be a compact stable token; keep `permalink` as
  `https://t.me/c/<chat>/<id>` or `https://t.me/<username>/<id>`.
- **Media**: albums/media groups become the carousel (preserve order). Download
  with `client.download_media` into per-item folders; generate thumbnails
  locally; videos use a poster/frame. Do not hotlink Telegram CDN URLs
  (they expire).
- **Owner/creator**: sender username or display name; link `t.me/<username>`
  when available, otherwise no link.
- **Saved state**: membership in Saved Messages/folder is the saved state.
  "Unsave" means deleting/removing the message from the saved location, with
  explicit confirmation and a dry-run/preview. Never delete from the source
  channel/group.
- **Comments**: replies in the linked discussion group (`reply_to` chains), or
  comments in a forum topic. Flatten roots + replies into the normalised
  comment shape; paginate by message id.
- **Dates/likes**: message date; views/reactions/forwards as available. There is
  no universal "likes" count, so map the best available metric and label it.
- **UI**: build it as a Telegram **Mini App** (WebApp) that reuses the shared
  HTML/JS. Desktop/mobile get grid/list/drawer/multi-select/drag; also provide
  message-based fallbacks for chats where a WebApp cannot open:
  `/saved`, `/search <q>`, `/tag <tag>`, `/category <name>`, inline keyboard
  pagination, and callback buttons for "assign category", "next page",
  "open post". Multi-select in chat uses checkmark callback buttons; drag is
  replaced by "Select → Assign category".
- **Live dashboard**: show the same cards/jobs in the Mini App. If jobs run
  server-side, stream/log via the bot by editing a single status message
  (respect Telegram edit/flood limits) or expose the web dashboard locally.
- **Auth/limits**: session files are per-account, chmod 600; handle FloodWait,
  `FLOOD_PREMIUM_WAIT`, media size limits, and resumable downloads. Never run
  multiple clients on the same session concurrently.
- **Search**: Telegram server search for messages, plus local SQLite FTS for
  captions/notes/tags/OCR/transcripts once downloaded.
- **Categories/tags/notes**: Telegram has no native categories; they are local
  metadata exactly like the reference site. Persist them online with the same
  gist/D1 mirror.
- **Scheduled sync**: systemd timer/cron runs the incremental fetch, then
  repairs covers/order and pushes cloud state.

Acceptance additions for Telegram: album order matches `media_group_id`; a
deleted source message is reported as unavailable, not silently dropped; the
Mini App and the chat fallback expose the same search/filter/category state.

---

## PLATFORM ADDENDUM — THREADS (Twitter-like)

Tailor everything to Threads' post/thread/reply model:

- **Source data**: the user's saved/bookmarked Threads, their own posts, a
  profile timeline, or a list. Threads does not expose a reliable public saved
  API; use an authenticated browser session/cookies and the internal endpoints
  the web client uses, with HTML fallback. Treat a partial/soft-blocked
  response as invalid; never mark a scan complete on a partial page.
- **Identity**: stable key = post code/pk. `permalink` is
  `https://www.threads.net/@<user>/post/<code>`. Keep a local integer id for the
  gallery, unrelated to the platform id.
- **Media**: image/video/carousel (up to 20). Download media; preserve carousel
  order; generate distinct per-slide thumbnails; videos get a real frame/poster.
- **Owner/creator**: `@username`, display name, avatar; link
  `https://www.threads.net/@<username>`.
- **Saved state**: bookmark/saved state where the account exposes it; otherwise
  track "in local archive" and make the saved-state filter honest. Unsaving
  toggles the bookmark with confirmation; verify the post is actually gone from
  the saved list and do not trust a stale local flag.
- **Comments**: replies, nested replies, quotes/reposts. Normalise to
  roots + replies with owner/text/created_at/likes; paginate by cursor.
  Preserve deleted/unavailable reply placeholders only if useful.
- **Metrics**: likes, replies, reposts, quotes, shares/views when present. Label
  the metric; do not pretend a missing metric is zero.
- **Threads specifics**: a "post" may be part of a thread. Group thread parts by
  conversation/root id; store `thread_id`, `reply_to_id`, `root_id`. The drawer
  should show the whole thread/carousel, not just one fragment.
- **UI**: reuse the same web app and routes. There is no saved-folder concept;
  categories/tags/notes are local metadata. Drag-to-category and multi-select
  work as on the reference site. Mobile uses the same responsive grid.
- **Auth/limits**: cookie/session auth, CSRF headers, realistic user agent,
  rate limiting/backoff, 429/soft-block detection, resumable scan cursors.
  Never solve a captcha or bypass a security check; pause and surface a
  verification link.
- **Live dashboard**: same cards/jobs. Jobs: scan saved, download media,
  fetch replies, covers/thumbs, classify, repair, unsave validated. Show rate,
  ETA, current code, failures, log selector and auto-refresh.
- **Search/filters**: local SQLite FTS over text, owner, notes, tags, OCR,
  transcripts; creator filter; category registry; saved/bookmarked filters;
  uncategorised-only; newest/oldest/likes/shuffle/manual.
- **Scheduled sync**: systemd timer/cron; incremental cursor; cloud push after
  repairs.
- **Pages/HF**: optional export of notes/categories to GitHub Pages and media
  manifest to Hugging Face, as in the reference repo.

Acceptance additions for Threads: a partial scan cannot report a final total; a
thread is reconstructed as one item; carousel slides have distinct thumbnails;
bookmark state is re-verified after unsave; owner links resolve; the Live tab
matches the standalone dashboard job list.
