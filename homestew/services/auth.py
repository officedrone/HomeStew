"""Single-user authentication: password hashing, session tokens, lockout.

One local account protects the whole app (every page load and API call).
Design decisions, all deliberate for a self-hosted single-user container:

* **Hashing** uses stdlib ``hashlib.scrypt`` (n=2**15, r=8, p=1 - about
  32 MiB per hash, comfortably inside the container's 512 MB cap) with a
  random salt. No third-party dependency; verification is constant-time.
* **Storage** is ``settings.PASSWORD_HASH`` in ``/data/settings.json`` via
  ``EDITABLE_SETTINGS``. It is deliberately NOT in ``SECRET_SETTINGS``:
  those values are encrypted with the master key, and deleting that key
  (Settings > Advanced) would make the password unreadable and silently
  disable authentication. A scrypt hash is not reversible anyway, and
  settings.json is already chmod-0600 and never included in backups.
* **Sessions** are stateless signed cookies: ``<expiry_ts>.<hmac>`` keyed
  by a signing key derived from the password hash itself (HMAC-SHA256).
  Changing or resetting the password therefore invalidates every issued
  cookie automatically - no session table, no cleanup job.
* **Lockout** is in-memory and keyed by client IP: after
  ``AUTH_MAX_FAILED_ATTEMPTS`` consecutive failures from one address, logins
  from it are refused for ``AUTH_LOCKOUT_MINUTES`` minutes. In-memory state
  dies with the container, which is fine - an attacker who can restart the
  container already owns the password file. The peer address comes straight
  from the TCP connection (never a proxy header), so it cannot be spoofed
  in this deployment (no reverse proxy).

This module must stay importable without FastAPI: ``python -m
homestew.auth_cli`` runs it inside the container to reset a lost password.
"""
import hashlib
import hmac
import json
import logging
import secrets as _secrets
import time
from typing import Optional

from homestew.config import save_settings_overrides, settings

logger = logging.getLogger(__name__)

# Name of the session cookie issued on login / account creation.
SESSION_COOKIE = "homestew_session"

# scrypt work factors: 128 * n * r bytes ≈ 32 MiB, ~0.1-0.3 s on modest HW.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32

# Session lifetimes (seconds). "Remember me" issues a persistent cookie;
# otherwise the cookie dies with the browser AND the token itself expires
# after SESSION_MAX_AGE_SESSION, so a stolen cookie file is still time-bounded.
SESSION_MAX_AGE_REMEMBER = 30 * 24 * 3600  # 30 days
SESSION_MAX_AGE_SESSION = 12 * 3600  # 12 hours

# Domain-separation salt for the session signing key derivation.
_SESSION_KDF_SALT = b"homestew-session-v1"


class PasswordError(Exception):
    """Raised when a stored password hash is missing or malformed."""


# ---------------------------------------------------------------------------
# Password hashing (stdlib scrypt)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Return a self-describing scrypt hash string for ``password``.

    Format: ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`` - the work factors
    travel with the hash so future parameter changes can still verify old
    hashes (new logins are re-hashed with current parameters).
    """
    import base64

    salt = _secrets.token_bytes(SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=HASH_BYTES, maxmem=256 * 1024 * 1024,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Constant-time check of ``password`` against a stored hash string.

    Returns False (never raises) for empty/malformed stored values so a
    hand-edited settings.json can never crash the login path - it just
    means "this password does not match".
    """
    import base64

    if not stored or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
    except ValueError:
        return False
    try:
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
            dklen=len(expected), maxmem=256 * 1024 * 1024,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# Password lifecycle (persisted through the settings-override machinery)
# ---------------------------------------------------------------------------

# The CLI (`docker exec ... auth_cli reset-password`) runs in a SEPARATE
# process and only writes settings.json; the running server would otherwise
# keep authenticating against the hash it loaded at startup until restarted -
# so a locked-out user's reset command would appear to do nothing. We treat
# the on-disk value as the source of truth for auth decisions, re-reading it
# only when the file's mtime moves (one cheap stat() per request).
_pw_cache: dict = {"mtime": None, "hash": ""}


def _current_password_hash() -> str:
    """PASSWORD_HASH from settings.json if it changed since we last looked."""
    path = settings.DATA_DIR / "settings.json"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return ""  # no settings file yet -> no password (fails closed)
    if mtime != _pw_cache["mtime"]:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            # Mid-write or unreadable: keep the last good value rather than
            # briefly treating the account as password-less. mtime is not
            # updated, so the next request retries.
            return _pw_cache["hash"]
        raw = data.get("PASSWORD_HASH", "") if isinstance(data, dict) else ""
        _pw_cache["mtime"] = mtime
        _pw_cache["hash"] = raw if isinstance(raw, str) else ""
    return _pw_cache["hash"]


def stored_password_hash() -> str:
    """Public accessor for the current password hash (disk-aware)."""
    return _current_password_hash()


def _prime_cache(stored: str) -> None:
    """Align the cache with a value we just wrote, avoiding a redundant read."""
    path = settings.DATA_DIR / "settings.json"
    try:
        _pw_cache["mtime"] = path.stat().st_mtime_ns
    except OSError:
        _pw_cache["mtime"] = None
    _pw_cache["hash"] = stored or ""


def password_configured() -> bool:
    """True once an account password has been created."""
    return bool(_current_password_hash().strip())


def set_password(new_password: str) -> None:
    """Hash and persist ``new_password``, then apply it to the live singleton.

    Persist first, memory second (same rule as ``resolve_setup_step``): a
    failed disk write must not leave an in-memory password that vanishes on
    restart while the old hash is still what callers authenticate against.
    Re-hashing also rotates the session signing key, invalidating all
    previously issued cookies - which is exactly the desired behaviour for
    both "change password" and CLI "reset password".
    """
    had_password = password_configured()
    stored = hash_password(new_password)
    save_settings_overrides({"PASSWORD_HASH": stored})
    settings.PASSWORD_HASH = stored
    _prime_cache(stored)
    logger.info("Account password %s; all existing sessions are now invalid.",
                "updated" if had_password else "created")


def clear_password() -> None:
    """Remove the account password (CLI aid; re-arms first-run setup)."""
    save_settings_overrides({"PASSWORD_HASH": ""})
    settings.PASSWORD_HASH = ""
    _prime_cache("")
    logger.warning("Account password cleared - authentication is disabled until a new one is created.")


# ---------------------------------------------------------------------------
# Stateless signed session tokens
# ---------------------------------------------------------------------------

def _session_signing_key() -> Optional[bytes]:
    """Signing key derived from the stored password hash, or None.

    Deriving (rather than using) the hash keeps domain separation: the
    value written to settings.json never travels on the wire, and rotating
    the password rotates this key, invalidating every outstanding cookie.
    """
    stored = _current_password_hash().strip()
    if not stored:
        return None
    return hmac.new(_SESSION_KDF_SALT, stored.encode("utf-8"), hashlib.sha256).digest()


def create_session_token(max_age_seconds: int, *, now: Optional[float] = None) -> str:
    """Signed token valid for ``max_age_seconds`` from now.

    ``now`` is a test seam (unix timestamp to sign against); production
    always uses the wall clock. The max-age floor of 1 s means a token can
    never be born already-expired through this API - tests build expired
    tokens by passing a past ``now`` with a short lifetime.
    """
    key = _session_signing_key()
    if key is None:
        raise PasswordError("No account password is configured.")
    expires_at = int(time.time() if now is None else now) + max(1, int(max_age_seconds))
    sig = hmac.new(key, str(expires_at).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{expires_at}.{sig}"


def verify_session_token(token: Optional[str]) -> bool:
    """True when ``token`` is authentic and unexpired for the CURRENT password.

    A changed/reset password changes the signing key, so old tokens fail
    here without any bookkeeping.
    """
    key = _session_signing_key()
    if key is None or not token or not isinstance(token, str):
        return False
    expires_at, sep, sig = token.partition(".")
    if not sep or not expires_at.isdigit():
        return False
    expected = hmac.new(key, expires_at.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    return int(expires_at) > time.time()


# ---------------------------------------------------------------------------
# Failed-login lockout (in-memory, per client IP)
# ---------------------------------------------------------------------------

# ip -> {"count": consecutive failures, "locked_until": unix ts}
_failures: dict = {}


def record_failure(client_ip: str) -> None:
    """Register one failed login attempt from ``client_ip``."""
    entry = _failures.get(client_ip) or {"count": 0, "locked_until": 0.0}
    # A lockout that already expired starts a fresh streak.
    if entry["locked_until"] and time.time() >= entry["locked_until"]:
        entry = {"count": 0, "locked_until": 0.0}
    entry["count"] += 1
    limit = max(1, int(settings.AUTH_MAX_FAILED_ATTEMPTS or 8))
    if entry["count"] >= limit:
        minutes = max(1, int(settings.AUTH_LOCKOUT_MINUTES or 5))
        entry["locked_until"] = time.time() + minutes * 60
        entry["count"] = 0  # next failure after the lockout starts a new streak
        logger.warning("Login lockout engaged for %s (%d min).", client_ip, minutes)
    _failures[client_ip] = entry


def reset_failures(client_ip: str) -> None:
    """Forget failures after a successful login."""
    _failures.pop(client_ip, None)


def lockout_remaining(client_ip: str) -> int:
    """Whole seconds this IP must still wait (0 when not locked out)."""
    entry = _failures.get(client_ip)
    if not entry:
        return 0
    remaining = entry["locked_until"] - time.time()
    return max(0, int(remaining + 0.999)) if remaining > 0 else 0


def failure_delay(client_ip: str) -> float:
    """Progressive pre-response delay (seconds) for a failing IP.

    Cheap brute-force friction below the lockout threshold; capped so a
    legitimate user mistyping once or twice is not punished noticeably.
    """
    entry = _failures.get(client_ip) or {"count": 0}
    return min(0.25 * (int(entry.get("count", 0)) + 1), 2.0)
