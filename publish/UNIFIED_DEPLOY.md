# Unified static gallery (GitHub Pages + Hugging Face)

One site: <https://altcar.github.io/instagram-saved-post/>

## Architecture

- **Frontend:** static HTML/JS/CSS in `publish/site-unified/`, served by GitHub
  Pages from `gh-pages`.
- **Media / thumbnails / comments:** Hugging Face dataset
  `shalevlau/ig-archive-test`.
  `publish/manifests/<platform>.json` is the upload state; only posts with
  `hf_ready` are included.
- **Personal state:** category order, per-post categories, notes and tags are
  overrides keyed by gallery post id.
  - default file: gist `5dc24371e7371776648695269b30cfd2`,
    `static_overrides.json`
  - read anonymously from the public Gist API
  - writes require a gist-scope token (the site's “Cloud sync” button stores it
    in this browser only)
  - optional D1/Worker endpoint (`publish/worker/`) as the fast auto-save target
  - `static_overrides.json` is also shipped with the site as an offline base.

## Auto-save without rate-limit explosions

Edits are written to `localStorage` immediately, then coalesced into a
write-behind queue:

| Target | Minimum gap | Practical maximum |
| --- | --- | --- |
| D1 Worker | 30 s (one row upsert) | ~2,880 writes/day |
| Gist `static_overrides.json` | 5 min (one gist commit) | ~288 commits/day |

Details:

- One flush carries the whole document, so 30 keystrokes become one write.
- A flush happens 5 s after the last edit, at most once per interval.
- A continuous editor still gets a write at least every 2 minutes.
- Hiding the tab / leaving the page flushes once urgently; the gist is only
  written if at least 15 s have passed since its previous commit.
- `403`/`429`/`5xx` responses honour `Retry-After`/`X-RateLimit-Reset` and back
  off exponentially (30 s → 30 min). D1 and gist back off independently.
- The D1 Worker merges per-post `t` timestamps, so two tabs or two devices do not
  overwrite each other.
- The “Cloud sync” button shows `Saved`, `Pending…`, `Retry 2m`, or
  `Local only`, and opens a panel with **Sync now** / **Configure**.

Configure once per browser: gist token (gist scope), D1 Worker URL
`https://ig-archive-state.hheylau.workers.dev`, and the D1 archive key stored in
`publish/worker/.archive-key` (gitignored).

## Telegram view (local files, uploaded UI)

`/telegram/` is the Telegram archiver UI, hosted on GitHub Pages. It does **not**
upload Telegram content: `/api/local/...` calls go to the tg-archiver server on
the user's own computer (default `http://127.0.0.1:8799`), which serves
`tg-archiver/data/exports/*` from disk.

```bash
cd tg-archiver && ./run.sh 8799      # keep the local server running
```

Chrome treats `https://altcar.github.io` → `http://127.0.0.1:8799` as a local
network request. The first time, Chrome asks **“Allow local network access?”** —
choose **Allow**. If the prompt was missed, the page shows a **Connect** button
and an **Open local UI** link (`http://127.0.0.1:8799/?local=1`), which needs no
permission at all.

Override the local server URL with `?local_api=http://127.0.0.1:8799` (or
`https://<tunnel>` if you expose the local server through a tunnel).

The UI shell is checked in at `publish/telegram-ui/`; `build_unified.py` copies it
to `site-unified/telegram/`. The tg-archiver server allows CORS/PNA from
`https://altcar.github.io` and loopback origins.

## Routes

| Route | What |
| --- | --- |
| `/` | hub with platform rail + account selector |
| `/instagram/`, `/threads/`, `/twitter/`, `/rednote/`, `/reddit/` | direct platform routes (200) |
| `/telegram/` | Telegram UI; messages/media from the local tg-archiver |
| `/gallery/?source=threads&embed=1` | Threads gallery |
| `/gallery/?source=twitter&embed=1` | X gallery (alias for `source=x`) |
| `/gallery/?source=shalevfrfr` | Instagram test batch |
| `/unknown` | custom 404 hub page |

## Rebuild / publish

```bash
# refresh the local cloud_state.json from the browser export if needed
python3 publish/export_static_overrides.py
python3 publish/build_unified.py
bash publish/deploy_unified.sh
```

`deploy_unified.sh` builds, syncs `publish/site-unified` to a `gh-pages`
checkout at `publish/.deploy-pages`, commits and pushes.

## Validate

```bash
python3 publish/audit_links.py --sample 5
python3 publish/audit_links.py
```

The audit checks every HF media/thumb/comment URL plus gallery pages.
