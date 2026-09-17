"""Tests for secret-at-rest encryption (services/secret_store.py + config glue).

Run from the repo root:  pytest tests/ -q
"""
import base64
import json

import pytest

from homestew import config as cfg
from homestew.services.secret_store import (
    AUTO_KEY_FILENAME,
    SecretError,
    SecretStore,
    TamperedError,
    WrongKeyError,
    ensure_master_key,
    is_encrypted,
    load_master_key,
)

KEY_A = b"\x11" * 32
KEY_B = b"\x22" * 32


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Config singleton pointed at a temp DATA_DIR; state restored after."""
    saved_values = {k: getattr(cfg.settings, k) for k in cfg.EDITABLE_SETTINGS}
    saved_encrypted = cfg.settings.secrets_encrypted
    saved_unreadable = cfg.settings.unreadable_secrets
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    yield tmp_path
    for key, value in saved_values.items():
        setattr(cfg.settings, key, value)
    cfg.settings.secrets_encrypted = saved_encrypted
    cfg.settings.unreadable_secrets = saved_unreadable


@pytest.fixture
def store_a(monkeypatch):
    """A SecretStore with KEY_A installed as config's active store."""
    store = SecretStore(KEY_A)
    monkeypatch.setattr(cfg, "_secret_store", store)
    return store


# ---------------------------------------------------------------------------
# secret_store unit behavior
# ---------------------------------------------------------------------------

class TestSecretStore:
    def test_round_trip(self):
        store = SecretStore(KEY_A)
        token = store.encrypt("LLM_API_KEY", "sk-test-abc")
        assert is_encrypted(token)
        assert "sk-test-abc" not in token
        assert store.decrypt("LLM_API_KEY", token) == "sk-test-abc"

    def test_nonce_unique_per_encrypt(self):
        store = SecretStore(KEY_A)
        t1 = store.encrypt("LLM_API_KEY", "same")
        t2 = store.encrypt("LLM_API_KEY", "same")
        assert t1 != t2  # random nonce ⇒ distinct ciphertexts

    def test_wrong_key_reports_kids(self):
        token = SecretStore(KEY_A).encrypt("LLM_API_KEY", "secret")
        other = SecretStore(KEY_B)
        with pytest.raises(WrongKeyError) as exc:
            other.decrypt("LLM_API_KEY", token)
        assert exc.value.expected_kid == other.key_id
        assert exc.value.found_kid != other.key_id

    def test_tampered_ciphertext_rejected(self):
        store = SecretStore(KEY_A)
        enc, v1, kid, nonce_b64, ct_b64 = store.encrypt("LLM_API_KEY", "secret").split(":")
        raw = bytearray(base64.urlsafe_b64decode(ct_b64 + "=="))
        raw[0] ^= 0xFF
        tampered = ":".join([enc, v1, kid, nonce_b64, base64.urlsafe_b64encode(bytes(raw)).decode()])
        with pytest.raises(TamperedError):
            store.decrypt("LLM_API_KEY", tampered)

    def test_aad_binding_across_settings(self):
        """A ciphertext for one setting must not decrypt as another."""
        store = SecretStore(KEY_A)
        token = store.encrypt("LLM_API_KEY", "secret")
        with pytest.raises(TamperedError):
            store.decrypt("NOTIFY_WEBHOOK_TOKEN", token)

    def test_no_key_store(self):
        store = SecretStore(None)
        assert not store.available
        with pytest.raises(SecretError):
            store.encrypt("LLM_API_KEY", "x")
        # Decrypting a stored token without any key is an error, not a crash.
        token = SecretStore(KEY_A).encrypt("LLM_API_KEY", "x")
        with pytest.raises(SecretError):
            store.decrypt("LLM_API_KEY", token)

    @pytest.mark.parametrize("value", ["", "sk-plaintext", "ollama", "enc:v2:abc"])
    def test_is_encrypted(self, value):
        assert is_encrypted(value) == (value.startswith("enc:v1:"))

    def test_repr_hides_key(self):
        assert "AQE" not in repr(SecretStore(b"\x01" * 32))  # no key material


class TestLoadMasterKey:
    def test_base64_file(self, tmp_path):
        path = tmp_path / "key"
        path.write_text(base64.b64encode(KEY_A).decode() + "\n", encoding="ascii")
        assert load_master_key(path) == KEY_A

    def test_hex_file(self, tmp_path):
        path = tmp_path / "key"
        path.write_text(KEY_A.hex(), encoding="ascii")
        assert load_master_key(path) == KEY_A

    def test_raw_bytes_file(self, tmp_path):
        path = tmp_path / "key"
        path.write_bytes(b"\x01" * 32)
        assert load_master_key(path) == b"\x01" * 32

    def test_missing_file_returns_none(self, tmp_path):
        assert load_master_key(tmp_path / "nope") is None

    @pytest.mark.parametrize("content", ["short", "", "not base64 at all !!"])
    def test_invalid_content_returns_none(self, tmp_path, content):
        path = tmp_path / "key"
        path.write_text(content, encoding="ascii")
        assert load_master_key(path) is None


# ---------------------------------------------------------------------------
# config.py integration: save (encrypt) / load (decrypt, migrate)
# ---------------------------------------------------------------------------

class TestConfigSecrets:
    def test_save_encrypts_and_load_round_trips(self, isolated, store_a):
        cfg.save_settings_overrides({"LLM_API_KEY": "sk-secret-123", "THEME": "dark"})
        raw = (isolated / "settings.json").read_text(encoding="utf-8")
        assert "sk-secret-123" not in raw  # nothing plaintext on disk
        assert "enc:v1:" in raw
        assert json.loads(raw)["THEME"] == "dark"

        cfg.settings.LLM_API_KEY = ""
        cfg.load_settings_overrides()
        assert cfg.settings.LLM_API_KEY == "sk-secret-123"
        assert cfg.settings.secrets_encrypted is True
        assert cfg.settings.unreadable_secrets == ()

    def test_clear_secret_persists_empty(self, isolated, store_a):
        cfg.save_settings_overrides({"NOTIFY_WEBHOOK_TOKEN": "tok"})
        cfg.save_settings_overrides({"NOTIFY_WEBHOOK_TOKEN": ""})
        cfg.settings.NOTIFY_WEBHOOK_TOKEN = "stale"
        cfg.load_settings_overrides()
        assert cfg.settings.NOTIFY_WEBHOOK_TOKEN == ""

    def test_save_preserves_existing_ciphertext(self, isolated, store_a):
        cfg.save_settings_overrides({"LLM_API_KEY": "sk-keep"})
        token_before = json.loads((isolated / "settings.json").read_text())["LLM_API_KEY"]
        cfg.save_settings_overrides({"THEME": "light"})  # unrelated change
        data = json.loads((isolated / "settings.json").read_text())
        assert data["LLM_API_KEY"] == token_before

    def test_plaintext_migrated_on_load(self, isolated, store_a):
        (isolated / "settings.json").write_text(
            json.dumps({"LLM_API_KEY": "sk-legacy", "LLM_MODEL": "llama3.2"}),
            encoding="utf-8",
        )
        cfg.load_settings_overrides()
        assert cfg.settings.LLM_API_KEY == "sk-legacy"  # usable immediately
        raw = (isolated / "settings.json").read_text(encoding="utf-8")
        assert "sk-legacy" not in raw  # rewritten as ciphertext
        assert json.loads(raw)["LLM_MODEL"] == "llama3.2"

    def test_unreadable_secret_treated_as_unset(self, isolated, monkeypatch):
        SecretStore(KEY_A)  # produce a token with key A...
        token = SecretStore(KEY_A).encrypt("LLM_API_KEY", "sk-old")
        (isolated / "settings.json").write_text(
            json.dumps({"LLM_API_KEY": token}), encoding="utf-8"
        )
        monkeypatch.setattr(cfg, "_secret_store", SecretStore(KEY_B))  # boot with key B
        cfg.load_settings_overrides()
        assert cfg.settings.LLM_API_KEY == ""
        assert cfg.settings.unreadable_secrets == ("LLM_API_KEY",)

    def test_no_key_stores_plaintext_and_loads(self, isolated, monkeypatch):
        monkeypatch.setattr(cfg, "_secret_store", SecretStore(None))
        cfg.save_settings_overrides({"LLM_API_KEY": "sk-dev"})
        raw = (isolated / "settings.json").read_text(encoding="utf-8")
        assert json.loads(raw)["LLM_API_KEY"] == "sk-dev"  # dev fallback: plaintext

        cfg.settings.LLM_API_KEY = ""
        cfg.load_settings_overrides()  # must not raise on the plaintext value
        assert cfg.settings.LLM_API_KEY == "sk-dev"
        assert cfg.settings.secrets_encrypted is False


class TestEnsureMasterKey:
    """Boot-time key resolution: mounted file > auto-generated in DATA_DIR."""

    def test_mounted_key_wins(self, tmp_path):
        mounted = tmp_path / "mounted_key"
        mounted.write_bytes(KEY_A)
        data_dir = tmp_path / "data"
        key, source = ensure_master_key(mounted, data_dir)
        assert (key, source) == (KEY_A, "mounted")
        # The auto-generated file must not be created when a mount works.
        assert not (data_dir / AUTO_KEY_FILENAME).exists()

    def test_generates_on_first_boot(self, tmp_path):
        data_dir = tmp_path / "data"
        key, source = ensure_master_key(None, data_dir)
        assert source == "generated"
        assert key is not None and len(key) == 32
        auto = data_dir / AUTO_KEY_FILENAME
        assert auto.read_bytes() == key

    def test_reuses_existing_generated_key(self, tmp_path):
        data_dir = tmp_path / "data"
        first, _ = ensure_master_key(None, data_dir)
        second, source = ensure_master_key(None, data_dir)
        assert source == "generated"
        assert first == second  # stable across restarts

    def test_mounted_unreadable_falls_back_to_generated(self, tmp_path):
        bad = tmp_path / "bad_key"
        bad.write_text("not a key", encoding="ascii")  # wrong length/content
        data_dir = tmp_path / "data"
        key, source = ensure_master_key(bad, data_dir)
        assert source == "generated" and key is not None

    def test_no_writable_data_dir_returns_none(self, tmp_path):
        # A file where the data dir should be makes creation fail -> "none",
        # which puts the app in plaintext mode instead of crashing.
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="ascii")
        key, source = ensure_master_key(None, blocker / "data")
        assert (key, source) == (None, "none")


class TestBaseUrlPinning:
    """The saved API key may only be sent to the saved base URL."""

    def test_normalize(self):
        from homestew.api.settings import _normalize_base_url

        assert _normalize_base_url("HTTP://LocalHost:11434/v1/") == \
            _normalize_base_url("http://localhost:11434/v1")
        assert _normalize_base_url("https://api.openai.com/v1") != \
            _normalize_base_url("https://evil.example.com/v1")
