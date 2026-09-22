"""Tests for the single-user authentication layer (services/auth.py,
api/auth.py, and the middleware in main.py).

No async pytest runner exists in this repo - FastAPI's TestClient is fully
synchronous, so plain test functions are enough. DATA_DIR is monkeypatched
per test (same pattern as test_setup_wizard.py) so PASSWORD_HASH persists to
a throwaway settings.json and never leaks between tests; the module-global
lockout table in services/auth is cleared around each test because TestClient
always appears as one client IP.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from homestew.config import settings
from homestew.services import auth


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + no password + empty lockout/password caches."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "PASSWORD_HASH", "")
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})
    yield tmp_path
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})


@pytest.fixture()
def client(env):
    """TestClient with lifespan (init_db on the throwaway DATA_DIR)."""
    from homestew.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Password hashing (stdlib scrypt)
# ---------------------------------------------------------------------------

def test_hash_and_verify_roundtrip():
    stored = auth.hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert auth.verify_password("correct horse battery staple", stored)
    assert not auth.verify_password("wrong password here", stored)


@pytest.mark.parametrize("bad", [None, "", "garbage", "scrypt$x", "scrypt$2$1$1$!!$!!"])
def test_verify_password_malformed_stored_is_false_not_error(bad):
    assert auth.verify_password("anything", bad) is False


def test_set_password_persists_plain_hash_to_settings_json(env):
    auth.set_password("a-good-password")
    raw = (env / "settings.json").read_text(encoding="utf-8")
    # Not in SECRET_SETTINGS -> stored as the scrypt hash itself, never enc:
    assert '"PASSWORD_HASH": "scrypt$' in raw
    assert settings.PASSWORD_HASH.startswith("scrypt$")
    assert auth.password_configured()


def test_clear_password(env):
    auth.set_password("a-good-password")
    auth.clear_password()
    assert not auth.password_configured()
    assert settings.PASSWORD_HASH == ""


def test_out_of_process_hash_change_is_honored(env, monkeypatch):
    """A CLI reset (separate process) rewrites settings.json only; the running
    server must pick up the new hash without a restart. Regression guard for
    the stale-in-memory-PASSWORD_HASH bug found in e2e."""
    auth.set_password("first-password")

    # Simulate another process writing a fresh scrypt hash to disk, bypassing
    # this process's set_password()/settings singleton entirely.
    other_hash = auth.hash_password("second-password")
    (env / "settings.json").write_text(
        json.dumps({"PASSWORD_HASH": other_hash}), encoding="utf-8"
    )

    assert auth.stored_password_hash() == other_hash
    assert auth.verify_password("second-password", auth.stored_password_hash())
    assert not auth.verify_password("first-password", auth.stored_password_hash())


def test_unreadable_settings_keeps_last_good_hash(env):
    """A mid-write/corrupt settings.json must not briefly disable the password."""
    auth.set_password("a-good-password")
    good = auth.stored_password_hash()
    (env / "settings.json").write_text("{ this is not json", encoding="utf-8")
    # mtime moved but parse failed -> last known hash retained, still enforced.
    assert auth.stored_password_hash() == good


# ---------------------------------------------------------------------------
# Stateless signed session tokens
# ---------------------------------------------------------------------------

def test_session_token_roundtrip_and_expiry(env):
    auth.set_password("a-good-password")
    token = auth.create_session_token(3600)
    assert auth.verify_session_token(token)
    # Signed two hours ago with a 1 h lifetime -> expired against the real clock.
    expired = auth.create_session_token(3600, now=time.time() - 7200)
    assert not auth.verify_session_token(expired)


def test_session_token_tamper_and_garbage_rejected(env):
    auth.set_password("a-good-password")
    token = auth.create_session_token(3600)
    expires_at, _, sig = token.partition(".")
    forged = f"{int(expires_at) + 10_000}.{sig}"  # extend validity -> bad sig
    assert not auth.verify_session_token(forged)
    for junk in [None, "", "x", "1.", ".abc", "notanumber.abc"]:
        assert not auth.verify_session_token(junk)


def test_password_change_invalidates_existing_sessions(env):
    auth.set_password("first-password")
    token = auth.create_session_token(3600)
    assert auth.verify_session_token(token)
    auth.set_password("second-password")  # signing key derives from the hash
    assert not auth.verify_session_token(token)


# ---------------------------------------------------------------------------
# Lockout bookkeeping (unit level; API-level lockout tested below)
# ---------------------------------------------------------------------------

def test_lockout_engages_after_max_attempts(env, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MAX_FAILED_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "AUTH_LOCKOUT_MINUTES", 5)
    assert auth.lockout_remaining("1.2.3.4") == 0
    for _ in range(2):
        auth.record_failure("1.2.3.4")
        assert auth.lockout_remaining("1.2.3.4") == 0
    auth.record_failure("1.2.3.4")  # third failure reaches the limit
    remaining = auth.lockout_remaining("1.2.3.4")
    assert 0 < remaining <= 300
    # Other IPs are unaffected; success resets this one.
    assert auth.lockout_remaining("5.6.7.8") == 0
    auth.reset_failures("1.2.3.4")
    assert auth.lockout_remaining("1.2.3.4") == 0


def test_lockout_expires(env, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MAX_FAILED_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "AUTH_LOCKOUT_MINUTES", 1)
    auth.record_failure("9.9.9.9")
    assert auth.lockout_remaining("9.9.9.9") > 0
    # Rewind the lockout timestamp instead of sleeping a minute.
    auth._failures["9.9.9.9"]["locked_until"] = time.time() - 1
    assert auth.lockout_remaining("9.9.9.9") == 0


# ---------------------------------------------------------------------------
# API: middleware allowlist + status endpoint
# ---------------------------------------------------------------------------

def test_middleware_blocks_api_but_allows_health_and_auth_pages(client):
    r = client.get("/api/devices")
    assert r.status_code == 401
    assert r.json()["detail"] == "Not authenticated"
    # Healthcheck (Docker) and the login screen's own assets stay reachable.
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code in (200, 404)  # index.html may be absent locally
    r = client.get("/static/app.js")
    assert r.status_code != 401  # exempt; 404 is fine when static/ is not mounted
    # Auth-exempt API routes are reachable without a session.
    assert client.get("/api/auth/status").status_code == 200


def test_status_reveals_only_two_booleans(client):
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json() == {"password_configured": False, "authenticated": False}


# ---------------------------------------------------------------------------
# API: setup / login / logout / password change
# ---------------------------------------------------------------------------

def test_setup_creates_account_and_signs_in(client):
    r = client.post(
        "/api/auth/setup",
        json={"password": "first-account-pw", "confirm_password": "first-account-pw"},
    )
    assert r.status_code == 201
    assert r.json() == {"password_configured": True, "authenticated": True}
    # The session cookie now unlocks the API...
    assert client.get("/api/devices").status_code == 200
    # ...and setup is permanently closed (409) - this is what makes the
    # forced create-account step impossible to skip or replay.
    r = client.post(
        "/api/auth/setup",
        json={"password": "another-password", "confirm_password": "another-password"},
    )
    assert r.status_code == 409


def test_setup_rejects_mismatch_and_short(client):
    r = client.post("/api/auth/setup", json={"password": "abcdefgh", "confirm_password": "xyz12345"})
    assert r.status_code == 400
    r = client.post("/api/auth/setup", json={"password": "short", "confirm_password": "short"})
    assert r.status_code == 422  # min_length on the schema


def test_login_remember_me_cookie_attributes(client):
    auth.set_password("account-password")

    bad = client.post("/api/auth/login", json={"password": "nope-nope-nope", "remember_me": False})
    assert bad.status_code == 401

    good = client.post("/api/auth/login", json={"password": "account-password", "remember_me": True})
    assert good.status_code == 200
    assert auth.verify_session_token(good.cookies.get(auth.SESSION_COOKIE))
    set_cookie = good.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie
    assert "max-age=2592000" in set_cookie  # 30 days

    # Without remember-me: no Max-Age (browser-session cookie)...
    anon = TestClient(client.app)
    sess = anon.post("/api/auth/login", json={"password": "account-password", "remember_me": False})
    assert sess.status_code == 200
    assert "max-age" not in sess.headers["set-cookie"].lower()

    # Logout clears the cookie and the API locks again.
    out = client.post("/api/auth/logout")
    assert out.status_code == 200
    assert client.get("/api/devices").status_code == 401


def test_login_lockout_returns_423(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MAX_FAILED_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "AUTH_LOCKOUT_MINUTES", 5)
    # Skip the progressive pre-response delay - tests must stay fast.
    monkeypatch.setattr(auth, "failure_delay", lambda ip: 0)
    auth.set_password("account-password")

    for _ in range(3):
        r = client.post("/api/auth/login", json={"password": "wrong-password"})
        assert r.status_code == 401
    # Even the CORRECT password is refused while locked out, with a
    # Retry-After hint. (Per-IP isolation of the lockout table is covered by
    # test_lockout_engages_after_max_attempts - TestClient always reports the
    # same client host, so it cannot be exercised through HTTP here.)
    locked = client.post("/api/auth/login", json={"password": "account-password"})
    assert locked.status_code == 423
    assert "Try again in" in locked.json()["detail"]
    assert int(locked.headers["retry-after"]) > 0


def test_password_change_rotates_sessions(client):
    auth.set_password("original-password")
    login = client.post("/api/auth/login", json={"password": "original-password", "remember_me": True})
    assert login.status_code == 200

    # Wrong current password is refused.
    r = client.put("/api/auth/password", json={
        "current_password": "wrong-current", "new_password": "replacement-pw",
        "confirm_password": "replacement-pw"})
    assert r.status_code == 403

    r = client.put("/api/auth/password", json={
        "current_password": "original-password", "new_password": "replacement-pw",
        "confirm_password": "replacement-pw"})
    assert r.status_code == 200
    # This browser stays signed in (fresh cookie was issued)...
    assert client.get("/api/devices").status_code == 200
    # ...but a session created before the change is dead: a fresh TestClient
    # holding the pre-change cookie would 401; simplest proof is that the old
    # password no longer logs in anywhere.
    anon = TestClient(client.app)
    assert anon.post("/api/auth/login", json={"password": "original-password"}).status_code == 401
    assert anon.post("/api/auth/login", json={"password": "replacement-pw"}).status_code == 200


def test_settings_expose_lockout_fields_not_the_hash(client):
    auth.set_password("account-password")
    client.post("/api/auth/login", json={"password": "account-password"})
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["password_configured"] is True
    assert "password_hash" not in body
    assert body["auth_max_failed_attempts"] == settings.AUTH_MAX_FAILED_ATTEMPTS

    r = client.put("/api/settings", json={"auth_max_failed_attempts": 4, "auth_lockout_minutes": 10})
    assert r.status_code == 200
    assert r.json()["auth_max_failed_attempts"] == 4
    assert settings.AUTH_MAX_FAILED_ATTEMPTS == 4


# ---------------------------------------------------------------------------
# CLI (docker exec -it homestew python -m homestew.auth_cli ...)
# ---------------------------------------------------------------------------

def test_cli_reset_password_via_env(env, monkeypatch, capsys):
    from homestew import auth_cli

    assert auth_cli.cmd_status() == 0
    assert "Password configured: False" in capsys.readouterr().out

    monkeypatch.setenv("HOMESTEW_PASSWORD", "cli-reset-password")
    assert auth_cli.main(["reset-password"]) == 0
    out = capsys.readouterr().out
    assert "created successfully" in out and "sessions are now invalid" in out
    assert auth.verify_password("cli-reset-password", settings.PASSWORD_HASH)

    # Reset again over an existing account -> "updated".
    monkeypatch.setenv("HOMESTEW_PASSWORD", "cli-reset-password-2")
    assert auth_cli.main(["reset-password"]) == 0
    assert "updated successfully" in capsys.readouterr().out

    assert auth_cli.main(["clear-password"]) == 0
    capsys.readouterr()
    # Nothing left to clear -> exit code 1.
    assert auth_cli.main(["clear-password"]) == 1
