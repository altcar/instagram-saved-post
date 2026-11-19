import argparse
import getpass
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import instaloader
import requests
from instaloader import Post
from instaloader.exceptions import (
    BadCredentialsException,
    ConnectionException,
    InstaloaderException,
    TwoFactorAuthRequiredException,
)


SAVED_POSTS_URL = "https://www.instagram.com/api/v1/feed/saved/posts/"
SAVED_PAGE_SIZE_HINT = 21


def saved_api_headers(username: str) -> Dict[str, str]:
    """Headers the Instagram web app sends for its internal REST endpoints."""
    return {
        "X-IG-App-ID": "936619743392459",
        "X-ASBD-ID": "359341",
        "X-IG-WWW-Claim": "0",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Origin": "https://www.instagram.com",
        "Referer": f"https://www.instagram.com/{username}/saved/all-posts/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def build_loader(output_dir: Path) -> instaloader.Instaloader:
    """Configure Instaloader with sane defaults for saved-post scraping."""
    return instaloader.Instaloader(
        dirname_pattern=str(output_dir / "{target}"),
        filename_pattern="{shortcode}_{date_utc:%Y%m%d_%H%M%S}",
        download_comments=False,
        save_metadata=False,
        download_video_thumbnails=False,
        download_geotags=False,
        compress_json=False,
        max_connection_attempts=3,
        request_timeout=30.0,
        sleep=True,
        quiet=False,
    )


def ensure_login(
    loader: instaloader.Instaloader,
    username: str,
    password: str | None,
    sessionid: str | None,
) -> str:
    """Login using session cookie if provided, otherwise username/password (+2FA)."""
    ctx = loader.context

    if sessionid:
        # Instaloader seeds its session with empty ds_user_id/mid cookies; those
        # make Instagram treat the request as logged out, so start clean.
        ctx._session.cookies.clear()
        ctx._session.cookies.set("sessionid", sessionid, domain=".instagram.com")
        user_id = sessionid.split("%3A", 1)[0]
        if user_id.isdigit():
            ctx._session.cookies.set(
                "ds_user_id", user_id, domain=".instagram.com"
            )
        ctx.username = username
        # The old context.test_login() uses the retired graphql/query endpoint,
        # so the current saved-posts REST endpoint is used to validate instead.
        response = ctx._session.get(
            SAVED_POSTS_URL,
            headers=saved_api_headers(username),
            timeout=30,
        )
        if response.status_code == 429:
            raise SystemExit("Instagram rate-limited this session (HTTP 429).")
        if response.status_code != 200 or not response.headers.get(
            "content-type", ""
        ).startswith("application/json"):
            raise SystemExit(
                "Session cookie was rejected; Instagram did not return saved posts."
            )
        return username

    if password is None:
        password = getpass.getpass("Instagram password: ")

    try:
        loader.login(username, password)
    except TwoFactorAuthRequiredException:
        code = input("Enter 2FA code: ").strip()
        loader.two_factor_login(code)
    except BadCredentialsException as exc:
        raise SystemExit(f"Invalid credentials: {exc}") from exc
    except ConnectionException as exc:
        raise SystemExit(f"Network/Instagram error: {exc}") from exc

    logged_in = ctx.test_login()
    if not logged_in:
        raise SystemExit("Login failed; Instagram did not return a session.")
    return logged_in


def iter_saved_posts(
    loader: instaloader.Instaloader, username: str, limit: int | None
) -> Iterable[Tuple[Post, Dict[str, Any]]]:
    """Iterate saved posts via the web app's current REST endpoint.

    Instagram retired the ``graphql/query`` GET endpoint that Instaloader's
    ``Profile.get_saved_posts()`` still uses, so paginate the same endpoint the
    saved page itself calls.
    """
    headers = saved_api_headers(username)
    max_id: str | None = None
    seen = set()
    count = 0

    while True:
        params = {"max_id": max_id} if max_id else {}
        response = loader.context._session.get(
            SAVED_POSTS_URL,
            headers=headers,
            params=params,
            timeout=30,
        )
        if response.status_code == 429:
            raise ConnectionException("Instagram rate-limited saved posts (HTTP 429).")
        response.raise_for_status()
        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise ConnectionException(
                "Instagram returned HTML instead of saved posts; the session may "
                "be invalid or rate-limited."
            ) from exc

        items = data.get("items") or []
        for item in items:
            media = item.get("media") or item
            post = Post.from_iphone_struct(loader.context, media)
            if post.mediaid in seen:
                continue
            seen.add(post.mediaid)
            yield post, media
            count += 1
            if limit and count >= limit:
                return

        next_max_id = data.get("next_max_id")
        if not data.get("more_available") or not next_max_id:
            return
        max_id = next_max_id
        time.sleep(1.5)


def collect_comments(post: Post) -> List[Dict[str, Any]]:
    comments: List[Dict[str, Any]] = []
    for comment in post.get_comments():
        comments.append(
            {
                "id": comment.id,
                "text": comment.text or "",
                "created_at": comment.created_at_utc.isoformat(),
                "owner": comment.owner.username if comment.owner else None,
                "likes": getattr(comment, "likes_count", None),
            }
        )
    return comments


def collect_media_files(target_dir: Path, base_dir: Path) -> List[Dict[str, str]]:
    media: List[Dict[str, str]] = []
    for path in sorted(target_dir.glob("*")):
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".mp4"}:
            continue
        media.append(
            {
                "type": "video" if path.suffix.lower() == ".mp4" else "image",
                "path": str(path.relative_to(base_dir)),
            }
        )
    return media


def serialize_post(
    post: Post,
    media: Dict[str, Any],
    target_dir: Path,
    base_dir: Path,
    with_comments: bool = False,
) -> Dict[str, Any]:
    location = media.get("location") or {}
    location_name = location.get("name")

    comments: List[Dict[str, Any]] = []
    if with_comments:
        try:
            comments = collect_comments(post)
        except Exception as exc:
            # Comment listing uses a separate (currently unstable) endpoint; keep
            # the media metadata rather than failing the whole post.
            print(
                f"Could not fetch comments for {post.shortcode}: {exc}",
                file=sys.stderr,
            )

    return {
        "media_id": post.mediaid,
        "shortcode": post.shortcode,
        "permalink": f"https://www.instagram.com/p/{post.shortcode}/",
        "taken_at": post.date_utc.isoformat(),
        "caption": post.caption or "",
        "owner_username": post.owner_username,
        "owner_id": post.owner_id,
        "likes": post.likes,
        "is_video": post.is_video,
        "typename": post.typename,
        "media": collect_media_files(target_dir, base_dir),
        "comments": comments,
        "location": location_name,
        "hashtags": post.caption_hashtags,
    }


def download_saved(
    username: str,
    password: str | None,
    sessionid: str | None,
    output_dir: Path,
    json_path: Path,
    limit: int | None,
    skip_existing: bool,
    with_comments: bool,
) -> None:
    loader = build_loader(output_dir)
    logged_in_as = ensure_login(loader, username, password, sessionid)
    print(f"Logged in as {logged_in_as}")

    records: List[Dict[str, Any]] = []

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path_tmp = json_path.with_suffix(json_path.suffix + ".tmp")

    existing_dirs: Dict[str, Path] = {}
    if skip_existing:
        for child in output_dir.iterdir():
            if child.is_dir() and "_" in child.name:
                existing_dirs.setdefault(child.name.split("_", 1)[1], child)

    first_record = True
    json_file = json_path_tmp.open("w", encoding="utf-8")
    json_file.write("[\n")

    try:
        for idx, (post, media) in enumerate(
            iter_saved_posts(loader, logged_in_as, limit), start=1
        ):
            target = f"{idx:04d}_{post.shortcode}"
            target_dir = existing_dirs.get(post.shortcode)

            if target_dir is not None:
                print(f"Skip existing {target_dir.name}")
            else:
                target_dir = output_dir / target
                print(f"Downloading {target} ...", flush=True)
                try:
                    loader.download_post(post, target=target)
                except InstaloaderException as exc:
                    print(f"Failed to download {target}: {exc}", file=sys.stderr)
                    continue

            record = serialize_post(
                post, media, target_dir, output_dir, with_comments
            )

            if not first_record:
                json_file.write(",\n")
            json.dump(record, json_file, ensure_ascii=False)
            json_file.flush()
            first_record = False
            records.append(record)

        json_file.write("\n]\n")
        json_file.flush()
    finally:
        json_file.close()

    json_path_tmp.replace(json_path)
    print(f"Saved {len(records)} entries to {json_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download your Instagram saved posts (media + metadata)",
    )
    parser.add_argument("--username", required=True, help="Instagram username")
    parser.add_argument(
        "--password",
        help="Password (omit to be prompted). Ignored if --sessionid is given.",
    )
    parser.add_argument(
        "--sessionid",
        help="Instagram sessionid cookie value to skip interactive login/2FA.",
    )
    parser.add_argument("--out", default="saved_media", help="Folder for media files")
    parser.add_argument(
        "--json",
        default="saved_posts.json",
        help="Path to write JSON metadata",
    )
    parser.add_argument(
        "--limit", type=int, help="Max number of saved posts to fetch (for testing)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="If a target folder exists, reuse it and only refresh JSON entry.",
    )
    parser.add_argument(
        "--comments",
        action="store_true",
        help="Also fetch comments (slow/unreliable while Instagram migrates APIs).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    download_saved(
        username=args.username,
        password=args.password,
        sessionid=args.sessionid,
        output_dir=Path(args.out),
        json_path=Path(args.json),
        limit=args.limit,
        skip_existing=args.skip_existing,
        with_comments=args.comments,
    )


if __name__ == "__main__":
    main()
