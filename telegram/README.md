# Telegram UI (static shell, local content)

This folder is the Telegram archiver UI that is published at
<https://altcar.github.io/instagram-saved-post/telegram/>.

It contains **no Telegram messages or media**. The UI calls the tg-archiver
FastAPI server on the user's own computer:

| UI | Data |
| --- | --- |
| GitHub Pages (`/telegram/`) | `http://127.0.0.1:8799/api/...` |
| `tg-archiver/data/exports/*` | served by that local server |

Start the local server:

```bash
cd tg-archiver
./run.sh 8799
```

Then open <https://altcar.github.io/instagram-saved-post/telegram/>.

## Browser permission

Chrome treats a request from `https://altcar.github.io` to
`http://127.0.0.1:8799` as **local network access**. The first time, allow the
prompt. If it was missed, the page shows a **Connect** button and an
**Open local UI** link (`http://127.0.0.1:8799/?local=1`), which needs no
permission at all.

Use `?local_api=http://127.0.0.1:8799` to point at a different port, or
`?local_api=https://<tunnel>` when the local server is exposed through an
HTTPS tunnel.

## Required tg-archiver server change

The local server must allow the Pages origin and the private-network preflight
(`server.py`):

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://altcar.github.io", "https://altcar.github.io/"],
    allow_origin_regex=r"^https://altcar\.github\.io$|^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    allow_private_network=True,
    max_age=86400,
)
```

This is already applied to the tg-archiver copy on this machine.

## Build

`publish/build_unified.py` copies this folder to
`publish/site-unified/telegram/`. `config.js` reads the optional
`local_api` query parameter and stores it in `localStorage` as `tg_api_base`.
