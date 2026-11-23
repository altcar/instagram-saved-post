# Unified static gallery (github.io)

Live site: **https://altcar.github.io/instagram-saved-post/**

Only repo used: `altcar/instagram-saved-post`, branch `gh-pages`.
**No media is stored in GitHub.** Every image, video, thumbnail, avatar and
comment file lives in the Hugging Face dataset **`shalevlau/ig-archive-test`**
and is served from
`https://huggingface.co/datasets/shalevlau/ig-archive-test/resolve/main/<path>`.

## Layout

```
/                        hub shell (Instagram / RedNote / Threads / X / Reddit / Telegram)
/?platform=threads       hub route -> iframe /gallery/?source=threads
/gallery/                single gallery app + combined manifest
/gallery/manifest.json   every post from every platform (HF URLs inside)
/gallery/thumbs/         only the 5 legacy Instagram thumbnails
/gallery/comments/       only the 5 legacy Instagram comment files
/telegram/               Telegram UI shell; messages/media come from the local PC
/404.html                copy of the hub shell
```

Routing is query based (`?platform=…`, gallery `?source=…`) because GitHub
Pages serves the project from `/instagram-saved-post/`, so path routes such as
`/threads` would never reach the app.

## Dataset prefixes

| Prefix | Platform | Contents |
|---|---|---|
| `posts/…` | Instagram | legacy paths, unchanged for compatibility |
| `threads/…` | Threads | `<pk>/<media-key>.<ext>`, `thumbs/`, `comments/` |
| `x/…` | X / Twitter | `<pk>/<media-key>.<ext>`, `thumbs/`, `comments/`, `avatars/` |
| `rednote/…` | RedNote | `<noteId>/video.mp4`, `<noteId>/image_NN.jpg`, `thumbs/`, `comments/` |
| `reddit/…` | Reddit | `<pk>/<media-key>.<ext>`, `thumbs/`, `comments/` |
| `manifests/<platform>.json` | — | per-platform manifest snapshot used by the build |

Each post is uploaded in **one commit** (media + 360 px thumbnail + comments
JSON, typically 2–4 files). Files already verified in
`publish/manifests/<platform>.json` are skipped, so a re-run only sends what is
missing.

Telegram is included as a **UI-only** source: the shell is uploaded, but its
messages and media are read from the tg-archiver server on the local computer
(`http://127.0.0.1:8799`). No Telegram media is uploaded. See
`publish/telegram-ui/README.md`.

## Upload

Media is committed in **large batches, one commit per batch**:

```bash
set -a; . /tmp/opencode/hf.env; set +a          # HF_TOKEN

python publish/hf_bulk_upload.py --platform threads --limit 60 --dry-run
python publish/hf_bulk_upload.py --platform threads --limit 60 --apply
python publish/hf_bulk_upload.py --platform rednote --resume --limit 100 --skip-videos --apply
python publish/hf_bulk_upload.py --platform rednote --resume --limit 60 --apply
python publish/hf_bulk_upload.py --platform x --resume --limit 60 --apply
```

`hf_bulk_upload.py` stages the batch (media + 360 px thumb + comments JSON) in
a temporary symlink tree and sends it with one
`upload_folder(multi_commits=False)` call, then verifies a sample of the new
`resolve/main/` URLs. `--resume` re-computes what is still missing from
`publish/manifests/<platform>.json`, so runs are idempotent.

`publish/hf_platform_upload.py` remains for single-post debugging
(`--posts-per-commit N` groups posts into one commit). `huggingface_hub`,
`pillow`, `imageio-ffmpeg` and `hf_xet` are required; logs append to
`/tmp/opencode/hf_platform_upload.log`.

## Rebuild / publish

```bash
python publish/build_unified.py                 # reads publish/manifests/*.json
cd publish/site-unified
git init -b gh-pages && git add -A \
  && git commit -m "Update unified site" \
  && git push --force origin gh-pages
```

The build is ~0.5 MB (manifest + app shell + 5 legacy thumbs), so it always
stays far below the Pages limit.

## Link audit

```bash
python publish/audit_links.py                   # full crawl of all five sources
python publish/audit_links.py --sample 5        # quick smoke test
python publish/audit_links.py --sources threads,x,reddit
```

The audit loads `gallery/?source=<source>` plus `gallery/manifest.json`, then
checks every `thumb`, `cover`, `avatar`, `comments`, `media[i]` and `permalink`
URL. Results: `publish/LINK_AUDIT.md` and `publish/link_audit.json`.

## Persistence (manual category / notes / tags)

`gallery/static-api.js` writes edits to `localStorage`
(`ig_static_overrides_v1`) and can push them to the gist
`5dc24371e7371776648695269b30cfd2` (`static_overrides.json`) or a D1 fallback.
Post ids are stable: Instagram keeps its original numeric ids, the other
platforms derive one from the platform primary key, so edits survive rebuilds.
