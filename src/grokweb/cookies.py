"""Read only selected Grok cookies; never export a Chrome database to disk.

The pinned browser-cookie3 decryptor is reused behind an exact SQL allowlist.
Its default loader uses substring domain matching and database-copy fallbacks,
so neither its loader nor its fallback Keychain password is used here.
"""

import asyncio
import hashlib
import hmac
import json
import os
import plistlib
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path

import browser_cookie3
from Cryptodome.Cipher import AES
from Cryptodome.Protocol.KDF import PBKDF2

from grokweb.errors import GrokError

COOKIE_NAMES = frozenset(
    {
        "sso",
        "sso-rw",
        "cf_clearance",
        "__cf_bm",
        "_cfuvid",
        "grok_device_id",
        "x-anonuserid",
        "x-userid",
        "x-challenge",
        "x-signature",
    }
)
CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"


def validate_profile(profile: str) -> str:
    if not re.fullmatch(r"[\w -]{1,80}", profile) or profile in {".", ".."}:
        raise GrokError("invalid_profile")
    return profile


def cookie_path(profile: str, root: Path = CHROME_ROOT) -> Path:
    folder = root / validate_profile(profile)
    for path in (folder / "Network/Cookies", folder / "Cookies"):
        if path.is_file():
            return path
    raise GrokError("profile_missing")


def chrome_user_agent() -> str:
    try:
        with Path("/Applications/Google Chrome.app/Contents/Info.plist").open("rb") as f:
            version = plistlib.load(f)["CFBundleShortVersionString"].split(".")[0]
        if not version.isdigit():
            raise ValueError
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
        )
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
        raise GrokError("profile_missing") from None


def keychain_password() -> bytes:
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "-q",
                "find-generic-password",
                "-w",
                "-a",
                "Chrome",
                "-s",
                "Chrome Safe Storage",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise GrokError("keychain_denied") from None
    if result.returncode or not result.stdout.strip():
        raise GrokError("keychain_denied")
    return result.stdout.strip()


class _MacDecryptor(browser_cookie3.Chrome):
    def __init__(self, password: bytes):
        # browser-cookie3 0.20.1's macOS _decrypt requires exactly these fields.
        self.iv = b" " * 16
        self.v10_key = PBKDF2(password, b"saltysalt", 16, 1003)


@dataclass(repr=False)
class ChromeSession:
    cookies: list[dict] = field(repr=False)
    user_agent: str

    def jar(self) -> CookieJar:
        jar = CookieJar()
        for row in self.cookies:
            jar.set_cookie(
                browser_cookie3.create_cookie(
                    row["domain"],
                    row["path"],
                    row["secure"],
                    row["expires"],
                    row["name"],
                    row["value"],
                    True,
                )
            )
        return jar


def read_chrome(profile: str = "Default", *, root: Path = CHROME_ROOT) -> ChromeSession:
    if sys.platform != "darwin":
        raise GrokError("unsupported_platform")
    path = cookie_path(profile, root)
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.execute("PRAGMA query_only = ON")
            version = int(db.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0])
            names = sorted(COOKIE_NAMES)
            rows = db.execute(
                "SELECT host_key, path, is_secure, expires_utc, name, value, encrypted_value "
                "FROM cookies WHERE host_key IN ('grok.com', '.grok.com') "
                f"AND name IN ({','.join('?' for _ in names)})",
                names,
            ).fetchall()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        raise GrokError("cookie_database") from None
    now = time.time()
    live = [r for r in rows if not r[3] or r[3] / 1_000_000 - 11644473600 > now]
    if not any(r[4] == "sso" for r in live):
        raise GrokError("auth_required", profile=profile)
    decryptor = _MacDecryptor(keychain_password())
    cookies = []
    for domain, path, secure, expires, name, value, encrypted in live:
        try:
            if not value and version >= 24:
                if encrypted[:3] != b"v10" or len(encrypted) < 51:
                    raise ValueError
                prefix = AES.new(decryptor.v10_key, AES.MODE_CBC, decryptor.iv).decrypt(
                    encrypted[3:35],
                )
                if not hmac.compare_digest(prefix, hashlib.sha256(domain.encode()).digest()):
                    raise ValueError
            value = decryptor._decrypt(value, encrypted, version >= 24)
        except Exception:
            raise GrokError("cookie_decryption") from None
        if not value or any(c in value for c in "\r\n;\x00"):
            continue
        cookies.append(
            {
                "domain": domain,
                "path": path,
                "secure": bool(secure),
                "expires": int(expires / 1_000_000 - 11644473600) if expires else None,
                "name": name,
                "value": value,
            }
        )
    if not any(c["name"] == "sso" and c["path"] == "/" for c in cookies):
        raise GrokError("auth_required", profile=profile)
    return ChromeSession(cookies, chrome_user_agent())


async def load_chrome(profile: str = "Default") -> ChromeSession:
    """Use a killable worker so cancellation also stops Keychain subprocesses.

    The worker communicates credentials through a private pipe, never a file or
    MCP stdout. stderr is suppressed; only controlled error codes are accepted.
    """
    validate_profile(profile)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "grokweb.cookies",
        profile,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(35):
            output, _ = await proc.communicate()
        data = json.loads(output)
        if "error" in data:
            raise GrokError(data["error"], profile=profile)
        return ChromeSession(**data)
    except (ValueError, TypeError):
        raise GrokError("cookie_decryption") from None
    except TimeoutError:
        raise GrokError("keychain_denied") from None
    finally:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()


def _worker() -> None:
    # Refuse accidental interactive invocation that would print secrets.
    if sys.stdout.isatty():
        raise SystemExit("Internal credential worker; use grokweb doctor instead.")
    try:
        session = read_chrome(sys.argv[1])
        data = {"cookies": session.cookies, "user_agent": session.user_agent}
    except GrokError as exc:
        data = {"error": exc.code}
    except Exception:
        data = {"error": "cookie_decryption"}
    sys.stdout.write(json.dumps(data))


if __name__ == "__main__":
    _worker()
