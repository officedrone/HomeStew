"""First-run setup wizard persistence (config.SETUP_STEPS + llm_configured).

The wizard offers each pending step once; skipping or saving a step records
the resolution in settings.json so it is never re-prompted on a later launch.
These tests cover the config-side bookkeeping the API endpoints build on.

Run from the repo root:  pytest tests/ -q
"""
import json

import pytest

from homestew import config as cfg


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Config singleton pointed at a temp DATA_DIR; state restored after."""
    saved = {k: getattr(cfg.settings, k) for k in cfg.EDITABLE_SETTINGS}
    saved_dir = cfg.settings.DATA_DIR
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    yield tmp_path
    for key, value in saved.items():
        setattr(cfg.settings, key, value)
    setattr(cfg.settings, "DATA_DIR", saved_dir)


class TestNormalizeSetupSteps:
    def test_keeps_only_valid_resolutions(self):
        assert cfg.normalize_setup_steps(
            {"llm": "saved", "device": "skipped"}
        ) == {"llm": "saved", "device": "skipped"}

    def test_drops_junk_values_and_keys(self):
        # A hand-edited/corrupted settings.json must not smuggle anything in.
        assert cfg.normalize_setup_steps(
            {"llm": "done", 5: "saved", "device": None}
        ) == {}

    def test_non_dict_is_empty(self):
        for value in (None, "skipped", ["llm"], 3):
            assert cfg.normalize_setup_steps(value) == {}


class TestResolveSetupStep:
    def test_persists_and_survives_reload(self, isolated):
        cfg.settings.SETUP_STEPS = {}
        steps = cfg.resolve_setup_step("secrets_key", "skipped")
        assert steps == {"secrets_key": "skipped"}

        raw = json.loads((isolated / "settings.json").read_text(encoding="utf-8"))
        assert raw["SETUP_STEPS"] == {"secrets_key": "skipped"}

        # A restart (reload) must see the same resolution - that is what
        # keeps a skipped step from coming back.
        cfg.settings.SETUP_STEPS = {}
        cfg.load_settings_overrides()
        assert cfg.settings.SETUP_STEPS == {"secrets_key": "skipped"}

    def test_merges_with_existing_resolutions(self, isolated):
        cfg.settings.SETUP_STEPS = {"llm": "saved"}
        steps = cfg.resolve_setup_step("device", "skipped")
        assert steps == {"llm": "saved", "device": "skipped"}

    def test_later_resolution_overwrites_earlier(self, isolated):
        # e.g. a step saved from another browser tab after being skipped.
        cfg.settings.SETUP_STEPS = {"llm": "skipped"}
        steps = cfg.resolve_setup_step("llm", "saved")
        assert steps == {"llm": "saved"}

    def test_write_failure_leaves_memory_untouched(self, tmp_path, monkeypatch):
        # If the volume rejects the write, the step must NOT be marked
        # resolved in memory - otherwise it would silently return on restart.
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="ascii")  # not a directory
        monkeypatch.setattr(cfg.settings, "DATA_DIR", blocker)
        cfg.settings.SETUP_STEPS = {}
        with pytest.raises(OSError):
            cfg.resolve_setup_step("llm", "skipped")
        assert cfg.settings.SETUP_STEPS == {}


class TestLlmLooksConfigured:
    """The AI step is only offered while the LLM looks unconfigured."""

    def _reset_llm(self, monkeypatch):
        defaults = cfg.Settings.model_fields
        monkeypatch.setattr(cfg.settings, "LLM_API_KEY", "")
        monkeypatch.setattr(
            cfg.settings, "LLM_BASE_URL", defaults["LLM_BASE_URL"].default
        )
        monkeypatch.setattr(cfg.settings, "LLM_MODEL", defaults["LLM_MODEL"].default)
        cfg.settings.SETUP_STEPS = {}

    def test_defaults_are_unconfigured(self, isolated, monkeypatch):
        self._reset_llm(monkeypatch)
        assert cfg.llm_looks_configured() is False

    def test_api_key_counts_as_configured(self, isolated, monkeypatch):
        self._reset_llm(monkeypatch)
        cfg.settings.LLM_API_KEY = "sk-dev"
        assert cfg.llm_looks_configured() is True

    def test_saved_wizard_step_counts_as_configured(self, isolated, monkeypatch):
        self._reset_llm(monkeypatch)
        cfg.settings.SETUP_STEPS = {"llm": "saved"}
        assert cfg.llm_looks_configured() is True

    def test_skipped_wizard_step_does_not_count(self, isolated, monkeypatch):
        # Skipping means "not now" for the wizard's flow, but the AI step is
        # resolved separately by setup_steps - llm_configured stays False so
        # other UI (chat banner) still knows nothing was set up.
        self._reset_llm(monkeypatch)
        cfg.settings.SETUP_STEPS = {"llm": "skipped"}
        assert cfg.llm_looks_configured() is False

    def test_custom_base_url_counts_as_configured(self, isolated, monkeypatch):
        # Someone deliberately pointed HomeStew at a server via env/compose.
        self._reset_llm(monkeypatch)
        cfg.settings.LLM_BASE_URL = "http://ollama:11434/v1"
        assert cfg.llm_looks_configured() is True

    def test_custom_model_counts_as_configured(self, isolated, monkeypatch):
        self._reset_llm(monkeypatch)
        cfg.settings.LLM_MODEL = "qwen3.5:4b"
        assert cfg.llm_looks_configured() is True
