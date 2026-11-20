#!/usr/bin/env python3
"""Read and decrypt Chrome cookies on Linux (GNOME keyring).

Used by the browser fallback so Playwright can open Instagram pages without
launching the user's live profile.

    python chrome_cookies.py --domain instagram.com
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def chrome_root() -> Path:
    return Path.home() / ".config" / "google-chrome"


def key_from_keyring() -> bytes:
    try:
        out = subprocess.check_output(
            ["secret-tool", "search", "--all", "application", "chrome"],
            text=True,
            timeout=10,
        )
        for line in out.splitlines():
            if line.strip().startswith("secret ="):
                value = line.split("=", 1)[1].strip()
                # Chrome stores the keyring password as the base64 text itself.
                return value.encode()
    except Exception:
        pass
    return b"peanuts"


def decrypt_value(encrypted: bytes, key_material: bytes, db_version: int) -> str:
    if encrypted[:3] not in (b"v10", b"v11"):
        return encrypted.decode("utf-8", "replace")
    key = hashlib.pbkdf2_hmac("sha1", key_material, b"saltysalt", 1, 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    plain = cipher.update(encrypted[3:]) + cipher.finalize()
    if db_version >= 24:
        plain = plain[32:]
    pad = plain[-1] if plain else 0
    if 0 < pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    return plain.decode("utf-8", "replace")


def load_cookies(domain: str = "instagram.com") -> Dict[str, str]:
    root = chrome_root()
    cookies_db = root / "Default" / "Cookies"
    if not cookies_db.exists():
        return {}
    with tempfile.TemporaryDirectory(prefix="chrome-cookies-") as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copyfile(cookies_db, copy)
        conn = sqlite3.connect(copy)
        try:
            version = int(
                conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0]
            )
        except Exception:
            version = 0
        rows = conn.execute(
            "SELECT host_key, name, value, encrypted_value FROM cookies "
            "WHERE host_key LIKE ?",
            (f"%{domain}%",),
        ).fetchall()
        conn.close()
    key_material = key_from_keyring()
    cookies: Dict[str, str] = {}
    for host, name, value, encrypted in rows:
        if value:
            cookies[name] = value
        elif encrypted:
            decrypted = decrypt_value(encrypted, key_material, version)
            if decrypted:
                cookies[name] = decrypted
    return cookies


def playwright_cookies(domain: str = "instagram.com") -> List[dict]:
    return [
        {
            "name": name,
            "value": value,
            "domain": ".instagram.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "Lax",
        }
        for name, value in load_cookies(domain).items()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Chrome cookies")
    parser.add_argument("--domain", default="instagram.com")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    cookies = load_cookies(args.domain)
    if args.json:
        print(json.dumps(cookies, indent=2))
    else:
        for name, value in cookies.items():
            print(f"{name} = {value}")


if __name__ == "__main__":
    main()
