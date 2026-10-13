"""Offline cookie tests: every credential and database in this file is synthetic."""

import asyncio
import hashlib
import json
import signal
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import unquote, urlsplit

import pytest
from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad

from grokweb import cookies
from grokweb.errors import MESSAGES, GrokError, http_error

PASSWORD = b"synthetic-test-only-safe-storage-key"
USER_AGENT = "Synthetic Chrome test agent"
NOW = 1_800_000_000
ALLOWED_NAMES = {
    "sso", "sso-rw", "cf_clearance", "__cf_bm", "_cfuvid", "grok_device_id",
    "x-anonuserid", "x-userid", "x-challenge", "x-signature",
}


def _encrypted(value, domain, version=24):
    # Construct Chrome's format independently of the production PBKDF2 helper.
    key = hashlib.pbkdf2_hmac("sha1", PASSWORD, b"saltysalt", 1003, dklen=16)
    payload = value.encode("utf-8")
    if version >= 24:
        payload = hashlib.sha256(domain.encode("utf-8")).digest() + payload
    cipher = AES.new(key, AES.MODE_CBC, iv=b" " * 16)
    return b"v10" + cipher.encrypt(pad(payload, AES.block_size))


def _row(name="sso", *, domain=".grok.com", value="synthetic-session",
         expires=0, path="/", secure=1, version=24, encrypted=None):
    if encrypted is None:
        encrypted = _encrypted(value, domain, version)
    return domain, path, secure, expires, name, "", encrypted


def _chrome_time(unix_seconds):
    return int((unix_seconds + 11_644_473_600) * 1_000_000)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """Fail closed if a test accidentally reaches a real profile or OS service."""
    original_keychain = cookies.keychain_password
    fake_keychain = Mock(return_value=PASSWORD)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cookies, "keychain_password", fake_keychain)
    monkeypatch.setattr(cookies, "chrome_user_agent", Mock(return_value=USER_AGENT))
    monkeypatch.setattr(cookies, "time", SimpleNamespace(time=lambda: NOW))

    def forbidden(*args, **kwargs):
        pytest.fail("Offline test attempted an unmocked process, signal, or network call")

    async def forbidden_spawn(*args, **kwargs):
        forbidden()

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    monkeypatch.setattr(cookies.os, "killpg", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)

    real_connect = sqlite3.connect

    def confined_connect(database, *args, **kwargs):
        location = str(database)
        if location.startswith("file:"):
            location = unquote(urlsplit(location).path)
        assert Path(location).resolve().is_relative_to(tmp_path.resolve()), (
            "Only temporary test databases may be opened"
        )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", confined_connect)
    return SimpleNamespace(
        root=tmp_path / "Chrome", keychain=fake_keychain, keychain_impl=original_keychain,
    )


@pytest.fixture
def make_db(offline):
    def create(rows=None, *, version=24, profile="Default", network=True):
        path = offline.root / profile / ("Network/Cookies" if network else "Cookies")
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
            db.execute(
                "CREATE TABLE cookies (host_key TEXT, path TEXT, is_secure INTEGER, "
                "expires_utc INTEGER, name TEXT, value TEXT, encrypted_value BLOB)"
            )
            db.executemany(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?)",
                [_row(version=version)] if rows is None else rows,
            )
        return path

    return create


@pytest.mark.parametrize("version", [23, 24, 25])
@pytest.mark.parametrize("domain", ["grok.com", ".grok.com"])
def test_decrypts_v10_with_versioned_domain_prefix(make_db, offline, version, domain):
    make_db([_row(domain=domain, value="synthetic-sso", version=version)], version=version)

    session = cookies.read_chrome(root=offline.root)

    assert session.cookies == [{
        "domain": domain, "path": "/", "secure": True, "expires": None,
        "name": "sso", "value": "synthetic-sso",
    }]
    assert session.user_agent == USER_AGENT
    offline.keychain.assert_called_once_with()
    assert "synthetic-sso" not in repr(session)


def test_exact_allowlist_is_applied_in_sql_before_decryption(make_db, offline, monkeypatch):
    allowed = [
        _row(name, domain=domain, value=f"synthetic-{name}")
        for domain in ("grok.com", ".grok.com") for name in sorted(ALLOWED_NAMES)
    ]
    denied = [
        _row(domain=domain, encrypted=b"v10-invalid-ciphertext")
        for domain in (
            "evilgrok.com", "grok.com.evil.test", ".grok.com.evil.test",
            "api.grok.com", ".api.grok.com", "GROK.COM", "x.com", "example.test",
        )
    ] + [
        _row(name, encrypted=b"v10-invalid-ciphertext")
        for name in ("SSO", "sso-extra", "not-sso", "analytics", "")
    ]
    path = make_db(allowed + denied)
    before = path.read_bytes()
    statements = []
    connect = sqlite3.connect

    def trace_read(database, *args, **kwargs):
        assert kwargs.get("uri") is True
        assert "mode=ro" in str(database)
        db = connect(database, *args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", trace_read)
    original_decrypt = cookies._MacDecryptor._decrypt
    decrypted = []

    def track_decryption(self, value, encrypted, has_domain_hash=False):
        assert encrypted != b"v10-invalid-ciphertext", "Disallowed cookie reached decryptor"
        decrypted.append(encrypted)
        return original_decrypt(self, value, encrypted, has_domain_hash)

    monkeypatch.setattr(cookies._MacDecryptor, "_decrypt", track_decryption)
    session = cookies.read_chrome(root=offline.root)

    expected = {(domain, name) for domain in ("grok.com", ".grok.com") for name in ALLOWED_NAMES}
    assert {(row["domain"], row["name"]) for row in session.cookies} == expected
    assert len(session.cookies) == len(decrypted) == len(allowed)
    # Re-execute the actual SELECT to prove filtering occurred in SQLite, rather
    # than after fetching/decrypting unrelated browser credentials.
    selects = [sql for sql in statements if "from cookies" in sql.lower()]
    assert len(selects) == 1
    with connect(path) as db:
        selected = db.execute(selects[0]).fetchall()
    assert set(selected) == set(allowed)
    assert path.read_bytes() == before


@pytest.mark.parametrize("row", [
    _row(domain="grok.com.attacker.test", encrypted=b"v10-invalid"),
    _row(domain="sub.grok.com", encrypted=b"v10-invalid"),
    _row("sso-rw"),
    _row("not-sso", encrypted=b"v10-invalid"),
    _row(expires=_chrome_time(NOW - 1), encrypted=b"v10-invalid"),
    _row(expires=_chrome_time(NOW), encrypted=b"v10-invalid"),
], ids=["lookalike-domain", "subdomain", "sso-rw-only", "wrong-name", "expired", "expires-now"])
def test_missing_live_sso_does_not_access_keychain(make_db, offline, row):
    make_db([row], profile="Profile 2")

    with pytest.raises(GrokError) as exc:
        cookies.read_chrome("Profile 2", root=offline.root)

    assert exc.value.code == "auth_required"
    assert "Chrome Profile 2" in str(exc.value)
    offline.keychain.assert_not_called()


def test_expiry_session_cookies_and_cookie_jar(make_db, offline):
    make_db([
        _row(),
        _row("cf_clearance", expires=_chrome_time(NOW + 3600), path="/search", secure=0),
        _row("x-userid", expires=_chrome_time(NOW - 1), encrypted=b"v10-invalid"),
        _row("__cf_bm", expires=_chrome_time(NOW), encrypted=b"v10-invalid"),
    ])

    session = cookies.read_chrome(root=offline.root)
    jar = {cookie.name: cookie for cookie in session.jar()}

    assert set(jar) == {"sso", "cf_clearance"}
    assert jar["sso"].expires is None
    assert jar["sso"].secure is True
    assert jar["sso"].domain == ".grok.com"
    assert jar["sso"].value == "synthetic-session"
    assert jar["cf_clearance"].expires == NOW + 3600
    assert jar["cf_clearance"].path == "/search"
    assert jar["cf_clearance"].secure is False


@pytest.mark.parametrize("value", ["", "bad\rvalue", "bad\nvalue", "bad;value", "bad\x00value"])
def test_unsafe_auth_cookie_is_rejected(make_db, offline, value):
    make_db([_row(value=value)])
    with pytest.raises(GrokError) as exc:
        cookies.read_chrome(root=offline.root)
    assert exc.value.code == "auth_required"
    assert "bad" not in str(exc.value)


def test_sso_must_cover_root_path(make_db, offline):
    make_db([_row(path="/unrelated")])
    with pytest.raises(GrokError) as exc:
        cookies.read_chrome(root=offline.root)
    assert exc.value.code == "auth_required"


@pytest.mark.parametrize("kind", ["invalid-block", "wrong-key"])
def test_decryption_failure_is_sanitized(make_db, offline, kind):
    row = _row(encrypted=b"v10-invalid") if kind == "invalid-block" else _row()
    make_db([row])
    if kind == "wrong-key":
        offline.keychain.return_value = b"different-synthetic-key"
    with pytest.raises(GrokError) as exc:
        cookies.read_chrome(root=offline.root)
    assert exc.value.code == "cookie_decryption"
    assert str(exc.value) == f"cookie_decryption: {MESSAGES['cookie_decryption']}"


def test_v24_rejects_ciphertext_bound_to_another_domain(make_db, offline):
    make_db([_row(encrypted=_encrypted("synthetic-wrong-domain", "other.example.test"))])
    with pytest.raises(GrokError) as exc:
        cookies.read_chrome(root=offline.root)
    assert exc.value.code == "cookie_decryption"


def test_changed_database_is_reread_while_wal_writer_stays_open(make_db, offline):
    path = make_db()
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        first = cookies.read_chrome(root=offline.root)
        writer.execute(
            "UPDATE cookies SET encrypted_value=? WHERE name='sso'",
            (_encrypted("synthetic-renewed-session", ".grok.com"),),
        )
        writer.commit()
        second = cookies.read_chrome(root=offline.root)
        writer.execute("DELETE FROM cookies")
        writer.commit()
        with pytest.raises(GrokError) as exc:
            cookies.read_chrome(root=offline.root)

    assert first.cookies[0]["value"] == "synthetic-session"
    assert second.cookies[0]["value"] == "synthetic-renewed-session"
    assert exc.value.code == "auth_required"
    assert offline.keychain.call_count == 2


def test_selected_profile_prefers_network_database(make_db, offline):
    make_db([_row(value="synthetic-default")])
    make_db([_row(value="synthetic-legacy")], profile="Profile 1", network=False)
    selected = make_db([_row(value="synthetic-network")], profile="Profile 1")
    assert cookies.cookie_path("Profile 1", offline.root) == selected
    session = cookies.read_chrome("Profile 1", root=offline.root)
    assert session.cookies[0]["value"] == "synthetic-network"


def test_selected_profile_falls_back_to_legacy_cookie_location(make_db, offline):
    make_db([_row(value="synthetic-default")])
    selected = make_db([_row(value="synthetic-legacy")], profile="Profile 3", network=False)
    assert cookies.cookie_path("Profile 3", offline.root) == selected
    session = cookies.read_chrome("Profile 3", root=offline.root)
    assert session.cookies[0]["value"] == "synthetic-legacy"


@pytest.mark.parametrize(
    "profile", ["", ".", "..", "../Default", "/tmp", "a/b", "a\\b", "a\n", "a" * 81],
)
def test_invalid_profiles_are_rejected_before_keychain(offline, profile):
    with pytest.raises(GrokError) as exc:
        cookies.read_chrome(profile, root=offline.root)
    assert exc.value.code == "invalid_profile"
    offline.keychain.assert_not_called()


def test_missing_profile_does_not_fall_back_to_default(make_db, offline):
    make_db()
    with pytest.raises(GrokError) as exc:
        cookies.read_chrome("Profile 9", root=offline.root)
    assert exc.value.code == "profile_missing"
    offline.keychain.assert_not_called()


def test_unsupported_platform_does_not_open_database(offline, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(GrokError) as exc:
        cookies.read_chrome(root=offline.root)
    assert exc.value.code == "unsupported_platform"
    offline.keychain.assert_not_called()


@pytest.mark.parametrize(
    "damage", ["not-sqlite", "missing-version", "invalid-version", "missing-table"],
)
def test_database_errors_are_controlled_and_do_not_access_keychain(make_db, offline, damage):
    path = make_db()
    if damage == "not-sqlite":
        path.write_bytes(b"synthetic-private-database-details")
    else:
        with sqlite3.connect(path) as db:
            if damage == "missing-version":
                db.execute("DELETE FROM meta")
            elif damage == "invalid-version":
                db.execute("UPDATE meta SET value='synthetic-private-invalid-version'")
            else:
                db.execute("DROP TABLE cookies")
    with pytest.raises(GrokError) as exc:
        cookies.read_chrome(root=offline.root)
    assert exc.value.code == "cookie_database"
    assert "synthetic-private" not in str(exc.value)
    offline.keychain.assert_not_called()


@pytest.mark.parametrize("outcome", ["denied", "empty", "timeout", "os-error"])
def test_keychain_failures_never_fall_back_or_expose_output(offline, monkeypatch, outcome):
    run = Mock(return_value=SimpleNamespace(
        returncode=1 if outcome == "denied" else 0,
        stdout=b"synthetic-private-output" if outcome == "denied" else b" \n",
        stderr=b"synthetic-private-error",
    ))
    if outcome == "timeout":
        run.side_effect = subprocess.TimeoutExpired("synthetic-private-command", 30)
    elif outcome == "os-error":
        run.side_effect = OSError("synthetic-private-error")
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(GrokError) as exc:
        offline.keychain_impl()

    assert exc.value.code == "keychain_denied"
    assert "synthetic-private" not in str(exc.value)
    assert run.call_count == 1


def test_keychain_success_uses_chrome_safe_storage_without_shell(offline, monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=PASSWORD + b"\n"))
    monkeypatch.setattr(subprocess, "run", run)
    assert offline.keychain_impl() == PASSWORD
    run.assert_called_once_with(
        ["/usr/bin/security", "-q", "find-generic-password", "-w",
         "-a", "Chrome", "-s", "Chrome Safe Storage"],
        capture_output=True, timeout=30, check=False,
    )


def test_denied_keychain_propagates_without_trying_to_decrypt(make_db, offline, monkeypatch):
    make_db()
    offline.keychain.side_effect = GrokError("keychain_denied")
    decryptor = Mock(side_effect=AssertionError("Must not decrypt without Keychain permission"))
    monkeypatch.setattr(cookies, "_MacDecryptor", decryptor)
    with pytest.raises(GrokError) as exc:
        cookies.read_chrome(root=offline.root)
    assert exc.value.code == "keychain_denied"
    decryptor.assert_not_called()


@pytest.mark.parametrize("status, models, expected", [
    (301, False, "auth_required"), (302, False, "auth_required"),
    (307, False, "auth_required"), (401, False, "auth_required"),
    (403, False, "verification_required"), (429, False, "rate_limited"),
    (404, False, "model_unavailable"), (404, True, "models_unavailable"),
    (500, False, "upstream_error"),
])
def test_http_errors_have_distinct_controlled_messages(status, models, expected):
    error = http_error(status, models=models)
    assert error.code == expected
    assert str(error) == f"{expected}: {MESSAGES[expected]}"


def test_unknown_error_does_not_echo_upstream_secrets():
    error = GrokError("synthetic-private-upstream-details")
    assert error.code == "internal_error"
    assert "synthetic-private" not in str(error)


def _finished_worker(monkeypatch, payload):
    proc = SimpleNamespace(
        pid=12345, returncode=0,
        communicate=AsyncMock(return_value=(payload, None)), wait=AsyncMock(),
    )
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return proc, spawn


async def test_worker_receives_selected_profile_and_uses_private_pipe(monkeypatch):
    payload = {"cookies": [], "user_agent": USER_AGENT}
    proc, spawn = _finished_worker(monkeypatch, json.dumps(payload).encode())

    session = await cookies.load_chrome("Profile 1")

    assert session.cookies == []
    assert session.user_agent == USER_AGENT
    spawn.assert_awaited_once_with(
        sys.executable, "-m", "grokweb.cookies", "Profile 1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    proc.wait.assert_not_awaited()


@pytest.mark.parametrize("payload, expected", [
    (b'{"error":"auth_required"}', "auth_required"),
    (b'{"error":"keychain_denied"}', "keychain_denied"),
    (b'{"error":"synthetic-private-upstream"}', "internal_error"),
    (b"synthetic-private-non-json", "cookie_decryption"),
    (b"null", "cookie_decryption"),
    (b"{}", "cookie_decryption"),
])
async def test_worker_errors_are_sanitized(monkeypatch, payload, expected):
    _finished_worker(monkeypatch, payload)
    with pytest.raises(GrokError) as exc:
        await cookies.load_chrome("Profile 2")
    assert exc.value.code == expected
    assert "synthetic-private" not in str(exc.value)
    if expected == "auth_required":
        assert "Chrome Profile 2" in str(exc.value)


async def test_invalid_profile_never_spawns_worker():
    with pytest.raises(GrokError) as exc:
        await cookies.load_chrome("../Default")
    assert exc.value.code == "invalid_profile"


@pytest.mark.parametrize("already_exited", [False, True])
async def test_cancellation_kills_worker_group_and_reaps_process(monkeypatch, already_exited):
    started = asyncio.Event()

    async def communicate():
        started.set()
        await asyncio.Future()

    proc = SimpleNamespace(
        pid=12345, returncode=None, communicate=communicate, wait=AsyncMock(return_value=-9),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    kill = Mock(side_effect=ProcessLookupError if already_exited else None)
    monkeypatch.setattr(cookies.os, "killpg", kill)
    task = asyncio.create_task(cookies.load_chrome())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    kill.assert_called_once_with(proc.pid, signal.SIGKILL)
    proc.wait.assert_awaited_once_with()


async def test_worker_timeout_is_controlled_and_reaps_process(monkeypatch):
    proc = SimpleNamespace(
        pid=12345, returncode=None,
        communicate=AsyncMock(side_effect=TimeoutError), wait=AsyncMock(return_value=-9),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    kill = Mock()
    monkeypatch.setattr(cookies.os, "killpg", kill)

    with pytest.raises(GrokError) as exc:
        await cookies.load_chrome()

    assert exc.value.code == "keychain_denied"
    kill.assert_called_once_with(proc.pid, signal.SIGKILL)
    proc.wait.assert_awaited_once_with()
