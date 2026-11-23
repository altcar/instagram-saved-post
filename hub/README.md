# Saved Archive hub (8891)

A tiny static shell that keeps one URL family for every archive service:

```
http://127.0.0.1:8891/instagram
http://127.0.0.1:8891/rednote
http://127.0.0.1:8891/threads
http://127.0.0.1:8891/telegram
http://127.0.0.1:8891/progress
```

Every route renders `public/index.html` with a shared social rail. `public/app.js`
loads the matching backend from `public/config.js` in an iframe. This is
deliberate: the hub itself is static and can live on **workers.dev**, while the
downloaders, SQLite archives, media files and progress dashboard stay on
separate machines/services.

## Run locally

```bash
python3 hub/server.py
# http://127.0.0.1:8891/
```

`serve_local.sh` also starts it on boot.

## Deploy to workers.dev

1. Edit `public/config.js` and point the five backend URLs at reachable
   deployments (Cloudflare Tunnel, Fly, Render, a VPS, etc.). The gallery
   backends need `?embed=1` so their own social rail is hidden inside the hub.
2. Deploy the static shell:

   ```bash
   cd hub
   npx wrangler deploy
   ```

   `wrangler.jsonc` uses Workers Assets with SPA fallback, so `/threads`,
   `/telegram`, etc. serve `index.html`.
3. Keep `/progress` pointed at the standalone dashboard (8766) or any public
   read-only progress endpoint. The hub never embeds backend code in the worker.

## Backends

| Route | Local backend | Notes |
| --- | --- | --- |
| `/instagram` | `http://127.0.0.1:8770/?embed=1` | shared gallery |
| `/rednote` | `http://127.0.0.1:8770/rednote?embed=1` | same gallery |
| `/threads` | `http://127.0.0.1:8770/?source=threads&embed=1` | Threads adapter |
| `/telegram` | `http://127.0.0.1:8799/` | existing Telegram UI, unchanged |
| `/progress` | `http://127.0.0.1:8766/` | separate downloader dashboard |

The Threads downloader at 8771 now redirects its old page to the hub; its
downloader keeps running and its data is read by the shared Threads adapter.
