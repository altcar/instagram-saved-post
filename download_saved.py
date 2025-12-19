import argparse
import getpass
import itertools
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import instaloader
from instaloader import Post, Profile
from instaloader.exceptions import (
    BadCredentialsException,
    ConnectionException,
    InstaloaderException,
    TwoFactorAuthRequiredException,
)


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
        ctx._session.cookies.set("sessionid", sessionid)
        ctx.username = username
        logged_in = ctx.test_login()
        if not logged_in:
            raise SystemExit("Session cookie was rejected; login failed.")
        return logged_in

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


def iter_saved_posts(profile: Profile, limit: int | None) -> Iterable[Post]:
    posts = profile.get_saved_posts()
    if limit:
        posts = itertools.islice(posts, limit)
    return posts


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


def serialize_post(post: Post, target_dir: Path, base_dir: Path) -> Dict[str, Any]:
    try:
        location_name = post.location.name if post.location else None
    except ConnectionException:
        # Instagram occasionally returns 201/403 for location lookups; ignore location.
        location_name = None

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
        "comments": collect_comments(post),
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
) -> None:
    loader = build_loader(output_dir)
    logged_in_as = ensure_login(loader, username, password, sessionid)
    print(f"Logged in as {logged_in_as}")

    profile = Profile.from_username(loader.context, logged_in_as)
    records: List[Dict[str, Any]] = []

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path_tmp = json_path.with_suffix(json_path.suffix + ".tmp")

    first_record = True
    json_file = json_path_tmp.open("w", encoding="utf-8")
    json_file.write("[\n")

    try:
        for idx, post in enumerate(iter_saved_posts(profile, limit), start=1):
            target = f"{idx:04d}_{post.shortcode}"
            target_dir = output_dir / target

            if skip_existing and target_dir.exists():
                print(f"Skip existing {target}")
                record = serialize_post(post, target_dir, output_dir)
            else:
                print(f"Downloading {target} ...", flush=True)
                try:
                    loader.download_post(post, target=target)
                except InstaloaderException as exc:
                    print(f"Failed to download {target}: {exc}", file=sys.stderr)
                    continue
                record = serialize_post(post, target_dir, output_dir)

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
    )


if __name__ == "__main__":
    main()
