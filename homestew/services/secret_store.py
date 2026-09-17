"""Encryption for persisted secret settings (API keys, webhook tokens).

Secrets saved through the Settings UI are stored in ``/data/settings.json``.
To keep them unreadable at rest — in backups, volume copies or accidental
git commits — each secret value is encrypted with AES-256-GCM before it is
written to disk. The master key lives outside the settings file and is
resolved at boot (see :func:`get_secret_store`):

1. an explicit key mounted at ``SECRETS_KEY_FILE`` (by default the Docker
   secret ``/run/secrets/homestew_secret_key``) — wins when present, for
   setups that keep the key outside the data volume;
2. otherwise a 32-byte key **auto-generated on first boot** and persisted at
   ``<DATA_DIR>/.secrets_key`` (mode 0600), i.e. inside the Docker named
   volume — no user setup step, survives rebuilds and container recreation.

Stored value format (a single string, so settings.json keeps its shape)::

    enc:v1:<kid>:<nonce_b64url>:<ciphertext_b64url>

* ``kid`` — key fingerprint: the first 8 bytes of SHA-256 over the derived
  AES key (the master key itself is never stored). Lets a wrong key file be
  reported precisely instead of failing as generic "bad data".
* The setting name (e.g. ``LLM_API_KEY``) is used as *additional
  authenticated data*, so a ciphertext cannot be copied from one setting
  into another without detection.

Values without the ``enc:v1:`` prefix are treated as legacy plaintext and
are migrated to encrypted form on the next boot (see config.py).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if TYPE_CHECKING:  # avoid a circular import at runtime (config imports us)
    from homestew.config import Settings

logger = logging.getLogger(__name__)

PREFIX = "enc:v1"
KEY_LENGTH = 32  # bytes of master-key material expected in the key file
_KDF_SALT = b"homestew-v1"
_KDF_INFO = b"aes-256-gcm/settings"


class SecretError(Exception):
    """Base class for secret-store failures (message is safe to log)."""


class WrongKeyError(SecretError):
    """Ciphertext was produced with a different master key."""

    def __init__(self, expected_kid: str, found_kid: str) -> None:
        self.expected_kid = expected_kid
        self.found_kid = found_kid
        super().__init__(
            f"stored with key {found_kid}, but the mounted key file is {expected_kid}"
        )


class TamperedError(SecretError):
    """Ciphertext failed authentication (corrupted or tampered data)."""


def is_encrypted(value: str) -> bool:
    """True when *value* is one of our ``enc:v1:...`` tokens."""
    return isinstance(value, str) and value.startswith(PREFIX + ":")


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded)


def _read_key_file(path: Path) -> Optional[bytes]:
    """Return 32 bytes of key material from *path*, or None with a warning.

    The file may hold raw 32 bytes, or base64/hex of 32 bytes (whitespace,
    newlines and a UTF-8 BOM are stripped). Never logs key material — only
    why loading failed, so the app can fall back to another key source with
    an actionable warning instead of crashing.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning(
            "Secrets key file %s not readable (%s).",
            path, exc.strerror or exc.__class__.__name__,
        )
        return None
    # utf-8-sig strips a BOM some Windows editors add; whitespace/newlines
    # come from echo/openssl output.
    text = raw.decode("utf-8-sig", errors="replace").strip()
    candidates: list[bytes] = []
    try:
        candidates.append(base64.b64decode(text, validate=True))
    except (binascii.Error, ValueError):
        pass
    if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
        try:
            candidates.append(bytes.fromhex(text))
        except ValueError:
            pass
    candidates.append(raw.strip())
    for candidate in candidates:
        if len(candidate) == KEY_LENGTH:
            return candidate
    logger.error(
        "Secrets key file %s must contain exactly %d bytes of key material "
        "(raw, base64 or hex). Generate one with: openssl rand -base64 32.",
        path, KEY_LENGTH,
    )
    return None


def load_master_key(key_file: Optional[Path]) -> Optional[bytes]:
    """Read the 32-byte master key from *key_file*, or None when unavailable."""
    if key_file is None:
        return None
    return _read_key_file(Path(key_file))


# File name for the auto-generated key inside DATA_DIR. Leading dot keeps it
# out of the way of the app's own data files (settings.json, *.db).
AUTO_KEY_FILENAME = ".secrets_key"


def _create_key_file(path: Path) -> Optional[bytes]:
    """Create *path* with a fresh random key, or None when that failed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL: two containers sharing the volume must not both generate —
        # whoever loses the race reads the winner's file instead.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    except OSError as exc:
        logger.error(
            "Could not create the auto-generated secrets key at %s (%s): "
            "secrets will be stored UNENCRYPTED.",
            path, exc.strerror or exc.__class__.__name__,
        )
        return None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(AESGCM.generate_key(bit_length=256))
    except OSError as exc:
        logger.error(
            "Could not write the auto-generated secrets key to %s (%s): "
            "secrets will be stored UNENCRYPTED.",
            path, exc.strerror or exc.__class__.__name__,
        )
        try:
            path.unlink()
        except OSError:
            pass
        return None
    try:
        # Some platforms ignore the open() mode; chmod makes it explicit.
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.info(
        "Generated a new master key at %s (mode 0600). Secrets are encrypted "
        "at rest with it — include this file in volume backups, and keep it "
        "out of git. To manage the key yourself instead, mount a key file at "
        "SECRETS_KEY_FILE (see README 'Secrets').", path,
    )
    return _read_key_file(path)


def ensure_master_key(
    key_file: Optional[Path], data_dir: Path
) -> tuple[Optional[bytes], str]:
    """Resolve the master key to boot with, generating one when needed.

    Returns ``(key, source)`` where *source* is ``"mounted"`` (explicit key
    file), ``"generated"`` (auto-generated key in the data volume, created on
    first boot or reused from it) or ``"none"`` — no key available, so the
    app falls back to plaintext and the Settings UI warns. An explicit mount
    at *key_file* always wins.
    """
    mounted = load_master_key(key_file)
    if mounted is not None:
        return mounted, "mounted"
    auto_path = Path(data_dir) / AUTO_KEY_FILENAME
    key = _read_key_file(auto_path) if auto_path.exists() else None
    if key is None:
        # Not there yet (or unreadable): try to create it. If another
        # container wins the O_EXCL race, read its file instead.
        key = _create_key_file(auto_path) or (
            _read_key_file(auto_path) if auto_path.exists() else None
        )
    return (key, "generated") if key is not None else (None, "none")


class SecretStore:
    """Encrypts/decrypts secret values with a key derived from the master key."""

    def __init__(self, master_key: Optional[bytes]) -> None:
        self._aesgcm: Optional[AESGCM] = None
        self.key_id: Optional[str] = None
        # Where the key came from: "mounted" | "generated" | "none". Set by
        # get_secret_store(); kept as an attribute so callers (logs, UI) can
        # explain the current key state without seeing key material.
        self.key_source: str = "none"
        if master_key is not None:
            derived = HKDF(
                algorithm=hashes.SHA256(),
                length=KEY_LENGTH,
                salt=_KDF_SALT,
                info=_KDF_INFO,
            ).derive(master_key)
            self._aesgcm = AESGCM(derived)
            # Fingerprint of the *derived* key; safe to store next to the
            # ciphertexts (deriving it back requires the master key).
            self.key_id = _b64e(hashlib.sha256(derived).digest()[:8])

    @property
    def available(self) -> bool:
        return self._aesgcm is not None

    def encrypt(self, name: str, value: str) -> str:
        """Encrypt *value* for setting *name*. Raises SecretError if no key."""
        if self._aesgcm is None:
            raise SecretError("no master key loaded")
        nonce = os.urandom(12)  # 96-bit GCM standard nonce size
        ct = self._aesgcm.encrypt(nonce, value.encode("utf-8"), name.encode("utf-8"))
        return f"{PREFIX}:{self.key_id}:{_b64e(nonce)}:{_b64e(ct)}"

    def decrypt(self, name: str, token: str) -> str:
        """Decrypt a stored ``enc:v1:...`` token for setting *name*.

        Raises WrongKeyError when the embedded key fingerprint differs,
        TamperedError on an authentication failure, SecretError on malformed
        input or when no master key is loaded.
        """
        parts = token.split(":")
        if len(parts) != 5 or parts[0] != "enc" or parts[1] != "v1":
            raise SecretError("malformed encrypted value")
        _, _, kid, nonce_b64, ct_b64 = parts
        if self._aesgcm is None:
            raise SecretError(
                f"value was stored with key {kid}, but no master key is available"
            )
        if kid != self.key_id:
            raise WrongKeyError(self.key_id, kid)
        try:
            plaintext = self._aesgcm.decrypt(
                _b64d(nonce_b64), _b64d(ct_b64), name.encode("utf-8")
            )
        except InvalidTag as exc:
            raise TamperedError(f"authentication failed for key {kid}") from exc
        except (binascii.Error, ValueError) as exc:
            raise SecretError("malformed encrypted value") from exc
        return plaintext.decode("utf-8", errors="replace")

    def __repr__(self) -> str:  # never expose key material
        return f"SecretStore(available={self.available}, key_id={self.key_id!r})"


def get_secret_store(app_settings: "Settings") -> SecretStore:
    """Build the boot-time SecretStore for *app_settings*.

    Resolves the master key via :func:`ensure_master_key` (mounted file,
    else auto-generated in DATA_DIR) and records where it came from on the
    returned store as ``key_source``.
    """
    key, source = ensure_master_key(
        app_settings.SECRETS_KEY_FILE, app_settings.DATA_DIR
    )
    store = SecretStore(key)
    store.key_source = source  # type: ignore[attr-defined]
    if source == "none":
        logger.error(
            "No secrets master key available and none could be created in %s: "
            "secrets will be stored UNENCRYPTED. Check that the data directory "
            "is writable, or mount a key file at SECRETS_KEY_FILE "
            "(see README 'Secrets').", app_settings.DATA_DIR,
        )
    return store
