# Automa-only Instagram Saved downloader

This is a standalone [Automa](https://www.automa.site/) workflow. It does **not**
change or replace the Python archive pipeline (`download_saved.py`,
`daily_update.sh`, etc.). It is a separate browser-only second opinion.

## What it does

1. Opens your Instagram Saved page in a tab (already logged in via Chrome).
2. Calls the current Saved endpoint
   (`/api/v1/feed/saved/posts/`) and follows `max_id` pagination.
3. Expands real ad carousels with `/api/v1/media/<pk>/info/` so multi-image ads
   are not reduced to their first creative.
4. Builds one row per media file and downloads each URL through Chrome's
   downloads API into your normal Downloads folder.
5. Exports `instagram-saved-posts.json` with all post metadata.

Downloaded filenames look like `ig_<media_id>_<slide>.jpg` / `.mp4`, so they are
stable and do not collide. The manifest keeps the original permalink, owner,
caption, timestamp and media count.

## Import and run

1. Open the Automa extension (toolbar icon → **Workflows**).
2. Choose **Import** and select
   `instagram-saved-download.automa.json`.
3. Open Instagram and make sure you are logged in.
4. Run the workflow manually.

If Chrome asks for download permission, allow it once. For thousands of files,
turn off “Ask where to save each file” in Chrome's download settings first.

## Test first

The JavaScript block starts with:

```js
const USERNAME = 'shalevfrfr';
const ENRICH_ADS = true;
const MAX_POSTS = 0; // 0 = all saved posts
```

Set `MAX_POSTS = 25` for a test run, or `ENRICH_ADS = false` to skip the extra
media-info requests for ads. The saved page URL is also in the **New tab**
block; change the username there if needed.

## Scope / limitations

- The workflow downloads media through the browser, not into the Python
  `data/posts/<media_id>/` layout. It is intentionally independent.
- A full run can create many thousands of files and use a lot of bandwidth.
- Instagram can rate-limit repeated media-info calls. Keep the built-in sleeps;
  lower `MAX_POSTS` and run in batches if needed.
- The exported JSON manifest can be used to reconcile the browser downloads with
  the Python archive later.
