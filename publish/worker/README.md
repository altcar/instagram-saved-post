# D1 personal-state Worker

`ig-archive-state` stores the gallery's personal overrides (categories, notes,
tags, order) in the `ig_archive_docs` table of the existing `notes-db` D1
database. One row per document; writes are merge-by-timestamp, so two tabs or
two devices cannot overwrite each other.

- Endpoint: `https://ig-archive-state.hheylau.workers.dev`
- Auth: `X-Archive-Key: <key>` (the key is in `.archive-key`, gitignored)
- Table: `ig_archive_docs(id TEXT PRIMARY KEY, body TEXT, rev INTEGER, updated_at INTEGER)`

The static site writes here at most once per 30 seconds. The gist is a
5-minute snapshot/backup.

Deploy:

```bash
cd publish/worker
npx wrangler deploy
npx wrangler secret put ARCHIVE_KEY < .archive-key
```
