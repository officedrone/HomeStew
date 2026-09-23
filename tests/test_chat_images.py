"""Tests for chat image support: schema validation + multimodal payload.

Covers the two pieces added for vision input:
  * ``ChatMessage`` validation (data-URL shape, count/size caps, user-only).
  * ``to_openai_messages`` - only the newest user turn keeps its images;
    older image turns collapse to a text placeholder.
Plus ``friendly_llm_error`` mapping a text-only-model rejection to guidance,
and ``reject_images_without_vision`` gating uploads on the saved "Model
supports Vision" setting.

No network and no LLM: these are pure functions over Pydantic models.
"""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from homestew.api.chat import (
    friendly_llm_error,
    reject_images_without_vision,
    to_openai_messages,
)
from homestew.models.schemas import (
    MAX_CHAT_IMAGES,
    ChatMessage,
)

# A tiny but valid base64 payload (decodes to "hi"); the validator only
# checks the data-URL SHAPE, it never decodes the bytes.
PNG_URL = "data:image/png;base64,aGk="
JPEG_URL = "data:image/jpeg;base64,aGk="


def _user(content="", images=None):
    return ChatMessage(role="user", content=content, images=images or [])


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------

class TestChatMessageValidation:
    def test_text_only_message_is_valid(self):
        msg = ChatMessage(role="user", content="hello")
        assert msg.images == []

    def test_image_only_message_allowed(self):
        # Empty caption is fine when a photo carries the meaning.
        msg = _user("", [PNG_URL])
        assert msg.content == "" and len(msg.images) == 1

    def test_empty_text_and_no_images_rejected(self):
        with pytest.raises(ValidationError, match="text or at least one image"):
            ChatMessage(role="user", content="   ")

    def test_images_only_on_user_role(self):
        with pytest.raises(ValidationError, match="Only user messages"):
            ChatMessage(role="assistant", content="ok", images=[PNG_URL])

    def test_rejects_remote_url(self):
        # Remote URLs would be an SSRF vector and local servers don't fetch.
        with pytest.raises(ValidationError, match="inline data:image"):
            _user("look", ["https://evil.example/x.png"])

    def test_rejects_non_image_data_url(self):
        with pytest.raises(ValidationError, match="inline data:image"):
            _user("look", ["data:text/html;base64,PGI+"])

    def test_rejects_svg_type(self):
        # SVG can carry script; only raster types are accepted.
        with pytest.raises(ValidationError, match="inline data:image"):
            _user("look", ["data:image/svg+xml;base64,PHN2Zz4="])

    def test_accepts_each_supported_type(self):
        for url in (
            "data:image/png;base64,aGk=",
            "data:image/jpeg;base64,aGk=",
            "data:image/jpg;base64,aGk=",
            "data:image/webp;base64,aGk=",
            "data:image/gif;base64,aGk=",
        ):
            assert _user("x", [url]).images == [url]

    def test_image_count_capped(self):
        urls = [PNG_URL] * (MAX_CHAT_IMAGES + 1)
        with pytest.raises(ValidationError, match="At most"):
            _user("many", urls)

    def test_oversized_image_rejected(self):
        huge = "data:image/png;base64," + ("A" * 5_000_000)
        with pytest.raises(ValidationError, match="too large"):
            _user("big", [huge])


# --------------------------------------------------------------------------
# Multimodal payload building
# --------------------------------------------------------------------------

class TestToOpenAIMessages:
    def test_plain_text_stays_string_content(self):
        out = to_openai_messages([_user("hi"), ChatMessage(role="assistant", content="yo")])
        assert out == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"},
        ]

    def test_newest_user_turn_becomes_multimodal(self):
        out = to_openai_messages([_user("what is this?", [PNG_URL])])
        assert out[0]["role"] == "user"
        parts = out[0]["content"]
        assert isinstance(parts, list)
        assert parts[0] == {"type": "text", "text": "what is this?"}
        assert parts[1] == {"type": "image_url", "image_url": {"url": PNG_URL}}

    def test_image_only_turn_has_no_text_part(self):
        out = to_openai_messages([_user("", [PNG_URL])])
        parts = out[0]["content"]
        assert all(p["type"] == "image_url" for p in parts)
        assert len(parts) == 1

    def test_multiple_images_each_become_a_part(self):
        out = to_openai_messages([_user("two", [PNG_URL, JPEG_URL])])
        img_parts = [p for p in out[0]["content"] if p["type"] == "image_url"]
        assert len(img_parts) == 2

    def test_only_last_user_turn_keeps_images(self):
        # First image turn collapses; the newest keeps its photo.
        msgs = [
            _user("first", [PNG_URL]),
            ChatMessage(role="assistant", content="ok"),
            _user("second", [JPEG_URL]),
        ]
        out = to_openai_messages(msgs)
        # Older image turn -> placeholder, string content.
        assert out[0]["content"] == "first\n[image attached]"
        # Newest user turn -> multimodal array with the JPEG.
        newest_urls = [
            p["image_url"]["url"] for p in out[2]["content"] if p["type"] == "image_url"
        ]
        assert newest_urls == [JPEG_URL]

    def test_no_images_anywhere_is_all_strings(self):
        msgs = [_user("a"), ChatMessage(role="assistant", content="b"), _user("c")]
        out = to_openai_messages(msgs)
        assert all(isinstance(m["content"], str) for m in out)


# --------------------------------------------------------------------------
# Friendly error mapping (non-vision model rejection)
# --------------------------------------------------------------------------

class TestFriendlyLLMError:
    def test_vision_unsupported_maps_to_guidance(self):
        exc = Exception("this model does not support images")
        msg = friendly_llm_error(exc)
        assert "cannot read images" in msg
        assert "vision model" in msg

    def test_generic_error_keeps_detail(self):
        exc = Exception("connection refused")
        msg = friendly_llm_error(exc)
        assert "Chat processing failed" in msg
        assert "connection refused" in msg


# --------------------------------------------------------------------------
# Vision gating: images require the saved "Model supports Vision" setting
# --------------------------------------------------------------------------

class TestRejectImagesWithoutVision:
    def test_image_without_vision_flag_is_refused(self, monkeypatch):
        from homestew import config as cfg

        monkeypatch.setattr(cfg.settings, "LLM_SUPPORTS_VISION", False)
        with pytest.raises(HTTPException) as excinfo:
            reject_images_without_vision([_user("look", [PNG_URL])])
        assert excinfo.value.status_code == 415
        # The message must name the checkbox so the user can fix it.
        assert "Model supports Vision" in str(excinfo.value.detail)

    def test_image_with_vision_flag_passes(self, monkeypatch):
        from homestew import config as cfg

        monkeypatch.setattr(cfg.settings, "LLM_SUPPORTS_VISION", True)
        reject_images_without_vision([_user("look", [PNG_URL])])  # no raise

    def test_text_only_message_never_raises(self, monkeypatch):
        from homestew import config as cfg

        monkeypatch.setattr(cfg.settings, "LLM_SUPPORTS_VISION", False)
        reject_images_without_vision([_user("hello")])  # no images -> fine

    def test_only_newest_user_turn_matters(self, monkeypatch):
        # An older image turn (which collapses to a text marker before the
        # LLM sees it) must not trip the guard when the newest turn is plain.
        from homestew import config as cfg

        monkeypatch.setattr(cfg.settings, "LLM_SUPPORTS_VISION", False)
        msgs = [
            _user("first", [PNG_URL]),
            ChatMessage(role="assistant", content="ok"),
            _user("follow-up with no photo"),
        ]
        reject_images_without_vision(msgs)  # no raise

    def test_empty_history_is_fine(self, monkeypatch):
        from homestew import config as cfg

        monkeypatch.setattr(cfg.settings, "LLM_SUPPORTS_VISION", False)
        reject_images_without_vision([])  # no user turn at all -> fine
