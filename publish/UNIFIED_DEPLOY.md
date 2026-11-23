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
  - optional D1/Worker endpoint as fallback
  - `static_overrides.json` is also shipped with the site as an offline base.

## Routes

| Route | What |
| --- | --- |
| `/` | hub with platform rail + account selector |
| `/instagram/`, `/threads/`, `/twitter/`, `/rednote/`, `/reddit/` | direct platform routes (200) |
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
