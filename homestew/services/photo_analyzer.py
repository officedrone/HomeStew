"""Vision analysis for device photos.

One LLM round-trip per photo, in two modes:

* **fresh** - the photo has no ``ai_notes`` yet: describe what is visible
  (model numbers, part sizes, condition, anything useful about the object).
* **compare** - the photo already has confirmed ``ai_notes``: pass them into
  the prompt and ask the model to report only a *material* difference.

Either way the result is a *proposal*: it lands in ``pending_ai_notes`` and
only reaches ``ai_notes`` when the user accepts it (the suggestion endpoint
in api/devices.py). The LLM itself never writes ``ai_notes``.

The prompt asks for strict JSON because the caller must distinguish
"nothing changed" from "here are new notes"; small local models tend to
wrap JSON in prose or code fences, so parsing is forgiving.
"""
import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from homestew.config import settings
from homestew.db import get_db_context
from homestew.services.llm_client import _pick

logger = logging.getLogger(__name__)

# Background-analysis progress state (upload-time "process now" runs here).
# Same single-process-dict pattern as indexer._INDEX_STATE.
_ANALYZE_STATE = {
    "running": False,
    "current_photo_id": None,
    "last_error": None,
}


def get_analyze_status() -> dict:
    """Snapshot of the current/last background photo-analysis job."""
    return dict(_ANALYZE_STATE)


@dataclass
class PhotoAnalysisResult:
    """What a vision pass concluded about one photo.

    ``changed=False`` means nothing worth recording (compare mode saw no
    material difference, or the model returned an empty description).
    ``notes`` is proposal text - never written to ai_notes directly.
    """
    changed: bool
    notes: Optional[str] = None
    reason: Optional[str] = None


FRESH_ANALYSIS_PROMPT = (
    "You are cataloguing a photo of a household device or one of its parts "
    "(for example an HVAC filter). Describe what is visible in 2-6 short "
    "bullet points, prioritising practical identification details: brand, "
    "model or part numbers, sizes/specifications printed on the item, and "
    "its apparent condition. Only state what you can actually read or see; "
    "never guess numbers. Respond with JSON only, no other text:\n"
    '{{"changed": true, "notes": "<bullet points joined by newlines>", '
    '"reason": "first analysis"}}'
)

COMPARE_ANALYSIS_PROMPT = (
    "You are re-examining a photo of a household device or part. These are "
    "the notes currently on file for it:\n\n"
    "---CURRENT NOTES---\n{ai_notes}\n---END NOTES---\n\n"
    "Compare the photo against those notes. If you now see a *material* "
    "difference (a readable model/part number that was missing, a changed "
    "condition, a wrong value), produce updated complete notes combining "
    "what still holds with the correction. If nothing material changed, do "
    "not invent edits. Respond with JSON only, no other text:\n"
    '{{"changed": true/false, "notes": "<full updated notes, or empty when '
    'unchanged>", "reason": "<what differs and why, or why unchanged>"}}'
)


def _photo_data_url(data: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def _extract_text(response) -> str:
    """Pull assistant text out of any response shape the client can return.

    Covers the SDK chat-completions object, the raw chat-completions dict,
    and the Responses API dict (``output_text`` shortcut or walking the
    ``output`` items' content parts).
    """
    # Responses API dicts carry output_text directly on modern servers.
    text = _pick(response, "output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    choices = _pick(response, "choices")
    if choices:
        message = _pick(choices[0], "message")
        content = _pick(message, "content")
        if isinstance(content, str):
            return content.strip()
        # Some servers return content as a list of parts.
        if isinstance(content, list):
            return "".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()

    output = _pick(response, "output")
    if isinstance(output, list):
        chunks = []
        for item in output:
            for part in (_pick(item, "content") or []):
                t = _pick(part, "text")
                if isinstance(t, str):
                    chunks.append(t)
        return "".join(chunks).strip()
    return ""


def _parse_analysis(text: str) -> PhotoAnalysisResult:
    """Parse the model's JSON reply, tolerating fences and stray prose."""
    candidate = text.strip()
    # Strip a markdown code fence if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    # Grab the outermost {...} if prose surrounded it.
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        # Unparseable but non-empty: treat the whole reply as notes so a
        # user can still salvage a first-pass analysis.
        if text.strip():
            return PhotoAnalysisResult(changed=True, notes=text.strip()[:4000],
                                       reason="unparsed model reply")
        return PhotoAnalysisResult(changed=False)

    if not isinstance(data, dict):
        return PhotoAnalysisResult(changed=False)
    notes = (data.get("notes") or "").strip()
    changed = bool(data.get("changed")) and bool(notes)
    reason = (data.get("reason") or "").strip() or None
    return PhotoAnalysisResult(
        changed=changed,
        notes=notes[:4000] if notes else None,
        reason=reason[:500] if reason else None,
    )


async def analyze_photo(photo_id: int, device_id: int) -> PhotoAnalysisResult:
    """Run one vision pass over a stored photo.

    Does NOT write to the database - the caller decides where the result
    goes (suggestion fields via API, pending fields via background job).
    Raises on missing files / LLM errors; callers surface or log them.
    """
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT p.filepath, p.mime_type, p.label, p.notes, p.ai_notes, "
            "d.name AS device_name, d.brand AS device_brand, d.model AS device_model "
            "FROM device_photos p JOIN devices d ON d.id = p.device_id "
            "WHERE p.id = ? AND p.device_id = ?",
            (photo_id, device_id),
        )
        row = await cursor.fetchone()
    if not row:
        raise ValueError(f"Photo {photo_id} not found for device {device_id}")

    path = Path(row["filepath"])
    if not path.is_file():
        raise FileNotFoundError(f"Photo file missing on disk: {path}")
    data = await asyncio.to_thread(path.read_bytes)

    # User label/notes steer what to look for but are clearly framed as
    # context, so the model doesn't echo them back as observations.
    user_context = []
    if row["device_name"]:
        user_context.append(f"Device: {row['device_name']}")
    if row["device_brand"] or row["device_model"]:
        user_context.append(
            "Brand/model: " + " ".join(str(x) for x in (row["device_brand"], row["device_model"]) if x)
        )
    if row["label"]:
        user_context.append(f"User label for this photo: {row['label']}")
    if row["notes"]:
        user_context.append(f"User notes: {row['notes']}")

    if row["ai_notes"]:
        prompt = COMPARE_ANALYSIS_PROMPT.replace("{ai_notes}", row["ai_notes"])
    else:
        prompt = FRESH_ANALYSIS_PROMPT
    if user_context:
        prompt = "\n".join(user_context) + "\n\n" + prompt

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": _photo_data_url(data, row["mime_type"])},
                },
            ],
        }
    ]

    from homestew.api.chat import get_llm_client

    client = get_llm_client()
    # chat_completion is synchronous (SDK or requests); keep the event loop free.
    response = await asyncio.to_thread(client.chat_completion, messages)
    return _parse_analysis(_extract_text(response))


async def analyze_photo_job(photo_id: int, device_id: int) -> None:
    """Background wrapper used by uploads with 'process now'.

    Writes the outcome into the photo's *pending* fields (a suggestion the
    user confirms in the UI). Never touches ai_notes. Failures are recorded
    in _ANALYZE_STATE and logged - an upload must not fail because the LLM
    did.
    """
    _ANALYZE_STATE["running"] = True
    _ANALYZE_STATE["current_photo_id"] = photo_id
    _ANALYZE_STATE["last_error"] = None
    try:
        result = await analyze_photo(photo_id, device_id)
        if result.changed and result.notes:
            async with get_db_context() as db:
                await db.execute(
                    "UPDATE device_photos SET pending_ai_notes = ?, "
                    "pending_reason = ?, proposed_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (result.notes, result.reason, photo_id),
                )
                await db.commit()
    except Exception as exc:
        _ANALYZE_STATE["last_error"] = str(exc)
        logger.error(f"Background photo analysis failed for photo {photo_id}: {exc}")
    finally:
        _ANALYZE_STATE["running"] = False
        _ANALYZE_STATE["current_photo_id"] = None


def vision_enabled() -> bool:
    """Whether photo-analysis features should be offered in UI/API."""
    return bool(settings.LLM_SUPPORTS_VISION)
