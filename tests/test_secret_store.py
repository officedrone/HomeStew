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
    create_generated_master_key,
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


@pytest.fixture
def runtime_key_env(tmp_path, monkeypatch):
    """Config pointed at a temp DATA_DIR with no mounted key; full restore.

    Unlike the fixtures above (which use monkeypatch for _secret_store), the
    runtime create/delete functions reassign that module global directly,
    so this fixture snapshots and restores it explicitly.
    """
    saved = {k: getattr(cfg.settings, k) for k in cfg.SECRET_SETTINGS}
    saved["store"] = cfg._secret_store
    saved["data_dir"] = cfg.settings.DATA_DIR
    saved["key_file"] = cfg.settings.SECRETS_KEY_FILE
    saved_encrypted = cfg.settings.secrets_encrypted
    saved_unreadable = cfg.settings.unreadable_secrets
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cfg.settings, "SECRETS_KEY_FILE", None)
    yield tmp_path
    cfg._secret_store = saved["store"]
    for key in cfg.SECRET_SETTINGS:
        setattr(cfg.settings, key, saved[key])
    setattr(cfg.settings, "DATA_DIR", saved["data_dir"])
    setattr(cfg.settings, "SECRETS_KEY_FILE", saved["key_file"])
    cfg.settings.secrets_encrypted = saved_encrypted
    cfg.settings.unreadable_secrets = saved_unreadable


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
    """Boot-time key resolution: mounted file > existing managed key.

    Boot never creates a key — creation is an explicit one-time action
    (first-run wizard / Settings > Advanced), see the runtime tests below.
    """

    def test_mounted_key_wins(self, tmp_path):
        mounted = tmp_path / "mounted_key"
        mounted.write_bytes(KEY_A)
        data_dir = tmp_path / "data"
        key, source = ensure_master_key(mounted, data_dir)
        assert (key, source) == (KEY_A, "mounted")
        # The managed file must not be created when a mount works.
        assert not (data_dir / AUTO_KEY_FILENAME).exists()

    def test_fresh_boot_without_key_returns_none(self, tmp_path):
        # No auto-generation at boot: a fresh install starts keyless so the
        # UI can offer a one-time creation instead of hiding it in logs.
        data_dir = tmp_path / "data"
        key, source = ensure_master_key(None, data_dir)
        assert (key, source) == (None, "none")
        assert not (data_dir / AUTO_KEY_FILENAME).exists()

    def test_reuses_existing_generated_key(self, tmp_path):
        data_dir = tmp_path / "data"
        created = create_generated_master_key(data_dir)
        key, source = ensure_master_key(None, data_dir)
        assert source == "generated"
        assert key == created  # stable across restarts

    def test_mounted_unreadable_falls_back_to_existing_managed(self, tmp_path):
        bad = tmp_path / "bad_key"
        bad.write_text("not a key", encoding="ascii")  # wrong length/content
        data_dir = tmp_path / "data"
        created = create_generated_master_key(data_dir)
        key, source = ensure_master_key(bad, data_dir)
        assert (key, source) == (created, "generated")

    def test_mounted_unreadable_without_managed_returns_none(self, tmp_path):
        bad = tmp_path / "bad_key"
        bad.write_text("not a key", encoding="ascii")
        data_dir = tmp_path / "data"
        key, source = ensure_master_key(bad, data_dir)
        assert (key, source) == (None, "none")
        assert not (data_dir / AUTO_KEY_FILENAME).exists()

    def test_unreadable_managed_file_returns_none(self, tmp_path):
        # A corrupt managed file is reported as keyless — never replaced.
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        (data_dir / AUTO_KEY_FILENAME).write_text("garbage", encoding="ascii")
        key, source = ensure_master_key(None, data_dir)
        assert (key, source) == (None, "none")


class TestCreateGeneratedMasterKey:
    def test_creates_once_never_overwrites(self, tmp_path):
        data_dir = tmp_path / "data"
        key = create_generated_master_key(data_dir)
        assert key is not None and len(key) == 32
        assert (data_dir / AUTO_KEY_FILENAME).read_bytes() == key
        # Second call must not touch the existing key.
        assert create_generated_master_key(data_dir) is None
        assert (data_dir / AUTO_KEY_FILENAME).read_bytes() == key

    def test_unwritable_data_dir_returns_none(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="ascii")
        assert create_generated_master_key(blocker / "data") is None


class TestRuntimeKeyManagement:
    """create/delete_master_key_runtime: wizard + Settings > Advanced."""

    def test_create_then_boot_reads_it(self, runtime_key_env):
        monkey_store = SecretStore(None)
        cfg._secret_store = monkey_store
        ok, reason = cfg.create_master_key_runtime()
        assert (ok, reason) == (True, "created")
        assert cfg.settings.secrets_encrypted is True
        auto = cfg.settings.DATA_DIR / AUTO_KEY_FILENAME
        assert auto.exists()
        # A restart resolves the same key from disk.
        key, source = ensure_master_key(None, cfg.settings.DATA_DIR)
        assert (key, source) == (auto.read_bytes(), "generated")

    def test_create_migrates_plaintext_secrets(self, runtime_key_env):
        # Keyless mode stored a plaintext secret; creating the key must pick
        # it up and re-encrypt it on disk.
        cfg._secret_store = SecretStore(None)
        cfg.save_settings_overrides({"LLM_API_KEY": "sk-dev"})
        ok, _ = cfg.create_master_key_runtime()
        assert ok is True
        assert cfg.settings.LLM_API_KEY == "sk-dev"
        raw = (cfg.settings.DATA_DIR / "settings.json").read_text(encoding="utf-8")
        assert "sk-dev" not in raw and "enc:v1:" in raw

    def test_create_when_key_exists_is_noop(self, runtime_key_env, monkeypatch):
        store = SecretStore(KEY_A)
        monkeypatch.setattr(cfg, "_secret_store", store)
        ok, reason = cfg.create_master_key_runtime()
        assert (ok, reason) == (False, "exists")
        assert cfg._secret_store is store
        assert not (cfg.settings.DATA_DIR / AUTO_KEY_FILENAME).exists()

    def test_create_with_mounted_file_present_is_refused(self, runtime_key_env):
        mounted = runtime_key_env / "mounted_key"
        mounted.write_text("broken key file", encoding="ascii")  # unreadable
        cfg._secret_store = SecretStore(None)
        object.__setattr__(cfg.settings, "SECRETS_KEY_FILE", mounted)
        ok, reason = cfg.create_master_key_runtime()
        assert (ok, reason) == (False, "mounted")
        assert not (cfg.settings.DATA_DIR / AUTO_KEY_FILENAME).exists()

    def test_create_with_corrupt_managed_file_fails(self, runtime_key_env):
        auto = cfg.settings.DATA_DIR / AUTO_KEY_FILENAME
        auto.parent.mkdir(parents=True)
        auto.write_text("garbage", encoding="ascii")
        cfg._secret_store = SecretStore(None)
        ok, reason = cfg.create_master_key_runtime()
        assert (ok, reason) == (False, "failed")
        assert auto.read_text(encoding="ascii") == "garbage"  # untouched

    def test_delete_makes_secrets_unreadable(self, runtime_key_env):
        # A managed key file on disk (holding KEY_A) + a store using it.
        auto = cfg.settings.DATA_DIR / AUTO_KEY_FILENAME
        auto.parent.mkdir(parents=True)
        auto.write_bytes(KEY_A)
        cfg._secret_store = SecretStore(KEY_A)
        cfg.save_settings_overrides({"LLM_API_KEY": "sk-secret"})
        # save_settings_overrides only writes the file; reload so memory holds
        # the decrypted value, exactly as a restart would.
        cfg.load_settings_overrides()
        assert cfg.settings.LLM_API_KEY == "sk-secret"
        ok, reason = cfg.delete_master_key_runtime()
        assert (ok, reason) == (True, "deleted")
        auto = cfg.settings.DATA_DIR / AUTO_KEY_FILENAME
        assert not auto.exists()
        # Ciphertext survives on disk but is unreadable without the key.
        raw = (cfg.settings.DATA_DIR / "settings.json").read_text(encoding="utf-8")
        assert "enc:v1:" in raw
        assert cfg.settings.LLM_API_KEY == ""
        assert cfg.settings.unreadable_secrets == ("LLM_API_KEY",)
        assert cfg.settings.secrets_encrypted is False

    def test_delete_mounted_key_is_refused(self, runtime_key_env, monkeypatch):
        store = SecretStore(KEY_A)
        store.key_source = "mounted"
        monkeypatch.setattr(cfg, "_secret_store", store)
        ok, reason = cfg.delete_master_key_runtime()
        assert (ok, reason) == (False, "mounted")
        assert cfg._secret_store is store

    def test_delete_without_key_reports_none(self, runtime_key_env):
        cfg._secret_store = SecretStore(None)
        ok, reason = cfg.delete_master_key_runtime()
        assert (ok, reason) == (False, "none")

    def test_recreate_after_delete_keeps_new_key_usable(self, runtime_key_env):
        cfg._secret_store = SecretStore(None)
        ok, _ = cfg.create_master_key_runtime()
        assert ok is True
        cfg.save_settings_overrides({"LLM_API_KEY": "sk-one"})
        assert cfg.delete_master_key_runtime()[0] is True
        assert cfg.settings.LLM_API_KEY == ""  # old ciphertext unreadable
        assert cfg.create_master_key_runtime()[0] is True
        cfg.save_settings_overrides({"LLM_API_KEY": "sk-two"})
        cfg.load_settings_overrides()  # memory reflects disk, as after a restart
        assert cfg.settings.LLM_API_KEY == "sk-two"
        raw = (cfg.settings.DATA_DIR / "settings.json").read_text(encoding="utf-8")
        assert "sk-two" not in raw


class TestBaseUrlPinning:
    """The saved API key may only be sent to the saved base URL."""

    def test_normalize(self):
        from homestew.api.settings import _normalize_base_url

        assert _normalize_base_url("HTTP://LocalHost:11434/v1/") == \
            _normalize_base_url("http://localhost:11434/v1")
        assert _normalize_base_url("https://api.openai.com/v1") != \
            _normalize_base_url("https://evil.example.com/v1")
