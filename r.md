Install: pip install -r requirements.txt (inside your venv).
Session cookie (no prompts): python download_saved.py --username shalevfrfr --sessionid <sessionid_cookie> --out saved_media --json saved_posts.json (grab sessionid from your browser’s Instagram cookies).
Password/2FA: python download_saved.py --username shalevfrfr --out saved_media --json saved_posts.json (will prompt for password and, if required, a 2FA code).
Options: --limit N to test on a subset; --skip-existing to reuse already-downloaded folders.
Outputs: media stored under saved_media/NNNN_shortcode; metadata (caption, comments, media paths, owner info, hashtags) written to saved_posts.json.



python download_saved.py --username shalevfrfr --sessionid 3030229108%3A5v6lt7M9a54dU0%3A18%3AAYjz9zZixWKDH8WaKAUJfwGzlbygzgIw5XS7O3wgooQ --out data/1412/saved_media --json data/1412/saved_posts.json

python download_saved.py --username shalevfrfr --sessionid 3030229108%3A5v6lt7M9a54dU0%3A18%3AAYjz9zZixWKDH8WaKAUJfwGzlbygzgIw5XS7O3wgooQ --out data/1412/saved_media --json data/1412/saved_posts.json --skip-existing --prune-orphans

python [download_saved.py](http://_vscodecontentref_/0) --username ... --sessionid ... --out data/1412/saved_media --json data/1412/saved_posts.json --skip-existing --prune-orphans