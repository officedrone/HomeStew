"""Execution of the manage_devices tool call made by the LLM.

Like the calendar tool, the chat exposes ONE action-based tool for the whole
device registry (create / update / list / add_attribute / remove_attribute)
instead of several small ones: local models pick the right action from a
single well-described schema far more reliably than they juggle multiple
tools, and one call keeps the trace readable in the UI.

Deletion is deliberately NOT offered to the model - removing a device also
removes its stored manuals and attributes, which is too destructive to trust
to a hallucinated id. A delete request instead returns an internal link that
opens that device's editor in the Devices tab so the USER presses Delete.

Device scoping mirrors the calendar/search tools: when the chat is filtered
to a device (``chat_device_id``), every mutation may only target that device
- other devices are invisible and untouchable from this conversation, and
the LLM can never widen the scope. Creating a new device stays allowed: it
touches no existing record.

Every handler returns a plain-text report aimed at the model: what changed,
the ids involved and enough detail for the model to confirm without guessing.
Errors come back as text too - raising here would surface a generic "tool
failed" message instead of something the model can explain or retry from.
"""
import json
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from homestew.db import get_db_context
from homestew.services.warranty import WARRANTY_UNITS, compute_warranty_end

logger = logging.getLogger(__name__)

_ACTIONS = (
    "search_devices",
    "create",
    "update",
    "list",
    "add_attribute",
    "remove_attribute",
    "delete",
)

# Words that carry no device-identifying signal in a resolution query
# ("what memory does my work laptop support"). Unlike the search engine's
# stopword list, everyday nouns like 'laptop' or 'phone' are NOT listed:
# they legitimately match device names/brands and only get down-weighted
# naturally when they appear in many devices.
_IDENTIFY_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can",
        "could", "did", "do", "does", "for", "from", "had", "has", "have",
        "he", "her", "his", "how", "i", "if", "in", "is", "it", "its",
        "me", "my", "no", "not", "of", "on", "or", "our", "please", "she",
        "so", "tell", "than", "that", "the", "their", "them", "then",
        "there", "they", "this", "to", "too", "was", "we", "were", "what",
        "when", "where", "which", "who", "why", "will", "with", "would",
        "you", "your",
    }
)

# Optional text columns an empty string clears (mirrors how the calendar tool
# treats an empty start_time). Required columns (name/brand/model) never clear.
_CLEARABLE = ("description", "serial_number", "product_number")

# Columns read back after every mutation so reports and list rows share one
# shape. Kept local on purpose: services must not import from homestew.api.
_DEVICE_COLUMNS = (
    "id, name, brand, model, description, serial_number, product_number, "
    "purchase_date, warranty_length, warranty_unit, warranty_end"
)
# Same columns qualified for the devices/manuals join in _list (GROUP BY d.id
# requires unambiguous names there).
_DEVICE_COLUMNS_D = ", ".join(
    f"d.{c.strip()}" for c in _DEVICE_COLUMNS.split(",")
)


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _coerce_device_id(value: Any) -> Optional[int]:
    """int(value), or -1 as a sentinel for 'bad id', None passthrough."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _clean_text(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _link_label(name: Any) -> str:
    """Device name made safe to use as Markdown link text.

    Brackets/parens in a name ("TV (2020)") would otherwise break or shift
    the [label](href) syntax the chat renderer parses.
    """
    text = str(name or "").strip()
    for ch in "[]()" :
        text = text.replace(ch, " ")
    return " ".join(text.split())


def device_link_markdown(name: Any, device_id: int) -> str:
    """Markdown link that opens this device's editor in the Devices tab.

    Every mutation report ends with this link so the model can hand the user
    a one-click jump to the device it just changed (the chat click handler
    routes #edit-device-<id>, see frontend/app.js). Public: calendar_tool
    reuses it for events that belong to a device.
    """
    return f"[{_link_label(name)}](#edit-device-{device_id})"


def device_link_note(name: Any, device_id: int) -> str:
    """Report suffix telling the model to pass the device link through."""
    return (
        "\nThe user can open this device with one click - end your answer "
        f"with this link, reproduced verbatim (same href, device name as "
        f"visible text): {device_link_markdown(name, device_id)}"
    )


def _warranty_phrase(row: Dict[str, Any]) -> str:
    """One-line warranty summary for reports (mirrors chat.py's roster)."""
    parts = []
    if row.get("purchase_date"):
        parts.append(f"purchased {str(row['purchase_date'])[:10]}")
    if row.get("warranty_length") is not None and row.get("warranty_unit"):
        parts.append(f"{row['warranty_length']} {row['warranty_unit']} warranty")
    if row.get("warranty_end"):
        parts.append(f"ends {str(row['warranty_end'])[:10]}")
    return (" | Warranty: " + ", ".join(parts)) if parts else ""


def _device_line(row: Dict[str, Any], manual_count: Optional[int] = None) -> str:
    """One-line summary of a device with everything the model needs."""
    bits = [f'"{row["name"]}" ({row["brand"]} {row["model"]})']
    if row.get("serial_number"):
        bits.append(f"serial={row['serial_number']}")
    if manual_count is not None:
        bits.append(f"{manual_count} manual(s)")
    line = " ".join(bits) + _warranty_phrase(row)
    if row.get("description"):
        line += f' - note: "{row["description"]}"'
    return line


async def _get_device_row(db, device_id: int) -> Optional[Dict[str, Any]]:
    cursor = await db.execute(
        f"SELECT {_DEVICE_COLUMNS} FROM devices WHERE id = ?", (device_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _get_attributes(db, device_id: int) -> list:
    cursor = await db.execute(
        "SELECT id, attribute_name, attribute_value FROM device_attributes "
        "WHERE device_id = ? ORDER BY id",
        (device_id,),
    )
    return [dict(a) for a in await cursor.fetchall()]


async def _load_scoped_device(
    db, raw_id: Any, chat_device_id: Optional[int]
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Fetch a device and enforce the chat's device scope."""
    device_id = _coerce_device_id(raw_id)
    if device_id is None or device_id == -1:
        return None, (
            "Error: 'device_id' must be a real integer from the conversation's "
            "device list. Use action='list' to find ids."
        )
    row = await _get_device_row(db, device_id)
    if not row:
        return None, (
            f"Error: no device with id={device_id}. Call action='list' to see "
            "the real device ids - never invent one."
        )
    # In a device-filtered chat every other device must stay invisible.
    if chat_device_id is not None and device_id != chat_device_id:
        return None, (
            f"Error: device id={device_id} is not the device this conversation "
            f"is filtered to (device_id={chat_device_id}). You may only manage "
            "that one device here."
        )
    return row, ""


async def _create(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    name = _clean_text(args.get("name"), 200)
    brand = _clean_text(args.get("brand"), 100)
    model = _clean_text(args.get("model"), 100)
    missing = [
        k for k, v in (("name", name), ("brand", brand), ("model", model)) if not v
    ]
    if missing:
        return (
            "Error: 'name', 'brand' and 'model' are all required to create a "
            f"device. Missing: {', '.join(missing)}. Ask the user for them."
        )

    purchase = _parse_date(args.get("purchase_date")) if args.get("purchase_date") else None
    if args.get("purchase_date") and purchase is None:
        return "Error: 'purchase_date' must be an ISO date (YYYY-MM-DD)."

    length = args.get("warranty_length")
    if length not in (None, ""):
        try:
            length = max(0, int(length))
        except (TypeError, ValueError):
            return "Error: 'warranty_length' must be a non-negative integer."
    else:
        length = None

    unit = _clean_text(args.get("warranty_unit"), 10).lower() or None
    if unit is not None and unit not in WARRANTY_UNITS:
        return (
            f"Error: unknown warranty_unit '{unit}'. Use one of: "
            + ", ".join(WARRANTY_UNITS)
            + "."
        )

    explicit_end = (
        _parse_date(args.get("warranty_end")) if args.get("warranty_end") else None
    )
    if args.get("warranty_end") and explicit_end is None:
        return "Error: 'warranty_end' must be an ISO date (YYYY-MM-DD)."

    end = compute_warranty_end(purchase, length, unit, explicit_end)

    async with get_db_context() as db:
        # Cheap twin-guard: small models happily re-create a device they just
        # created when asked to "add it again". Point them at the original.
        cursor = await db.execute(
            "SELECT id FROM devices WHERE lower(name) = lower(?) "
            "AND lower(brand) = lower(?) AND lower(model) = lower(?)",
            (name, brand, model),
        )
        twin = await cursor.fetchone()
        if twin:
            return (
                f"Nothing created: device id={twin['id']} already has exactly "
                f'this name/brand/model ("{name}" {brand} {model}). If the '
                "user wants to change something about it, use action='update' "
                "with that device_id instead."
            )

        cursor = await db.execute(
            """
            INSERT INTO devices (name, brand, model, description, serial_number,
                product_number, purchase_date, warranty_length, warranty_unit,
                warranty_end)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                brand,
                model,
                _clean_text(args.get("description"), 1000),
                _clean_text(args.get("serial_number"), 100) or None,
                _clean_text(args.get("product_number"), 100) or None,
                purchase.isoformat() if purchase else None,
                length,
                unit,
                end.isoformat() if end else None,
            ),
        )
        await db.commit()
        new_id = cursor.lastrowid
        row = await _get_device_row(db, new_id)

    logger.info("Device tool created device id=%s (%s)", new_id, name)
    # One-click follow-up: this link opens the device editor and immediately
    # starts the manual search (see the chat-message click handler in app.js).
    link = f"[{_link_label(name)}](#fetch-manuals-{new_id})"
    return (
        f"Created device id={new_id}: {_device_line(row, manual_count=0)}. "
        "Manuals are not attached by this tool. Tell the user the device was "
        "created with its details, then end your answer with a sentence like "
        "'Please navigate to <link> to search for and download manuals.' "
        f"where <link> is exactly {link} - keep the href verbatim and use the "
        "device name as the visible link text (never 'Device #<id>')."
    )


async def _update(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    async with get_db_context() as db:
        if args.get("device_id") in (None, "") and chat_device_id is not None:
            # In a filtered chat the current device is an implicit target.
            args = {**args, "device_id": chat_device_id}
        row, err = await _load_scoped_device(db, args.get("device_id"), chat_device_id)
        if err:
            return err

        fields: Dict[str, Any] = {}

        for key in ("name", "brand", "model"):
            if args.get(key) not in (None, ""):
                value = _clean_text(args[key], 200 if key == "name" else 100)
                if not value:
                    return f"Error: '{key}' cannot be cleared - it is required."
                fields[key] = value

        for key in _CLEARABLE:
            if args.get(key) is not None:
                text = _clean_text(args[key], 1000 if key == "description" else 100)
                # Empty string clears the column, same rule as calendar times.
                fields[key] = text or None

        touched_warranty = False
        if args.get("purchase_date") is not None:
            if str(args["purchase_date"]).strip() == "":
                fields["purchase_date"] = None
            else:
                parsed = _parse_date(args["purchase_date"])
                if parsed is None:
                    return "Error: 'purchase_date' must be an ISO date (YYYY-MM-DD)."
                fields["purchase_date"] = parsed.isoformat()
            touched_warranty = True

        if args.get("warranty_length") is not None:
            text = str(args["warranty_length"]).strip()
            if text == "":
                fields["warranty_length"] = None
            else:
                try:
                    fields["warranty_length"] = max(0, int(text))
                except ValueError:
                    return "Error: 'warranty_length' must be a non-negative integer."
            touched_warranty = True

        if args.get("warranty_unit") is not None:
            text = str(args["warranty_unit"]).strip().lower()
            if text == "":
                fields["warranty_unit"] = None
            elif text not in WARRANTY_UNITS:
                return (
                    f"Error: unknown warranty_unit '{text}'. Use one of: "
                    + ", ".join(WARRANTY_UNITS)
                    + "."
                )
            else:
                fields["warranty_unit"] = text
            touched_warranty = True

        explicit_end: Optional[date] = None
        if args.get("warranty_end") is not None:
            text = str(args["warranty_end"]).strip()
            if text == "":
                # Clearing the end date re-enables auto-computation.
                fields["warranty_end"] = None
                touched_warranty = True
            else:
                explicit_end = _parse_date(text)
                if explicit_end is None:
                    return "Error: 'warranty_end' must be an ISO date (YYYY-MM-DD)."
                fields["warranty_end"] = explicit_end.isoformat()

        # Keep the API's rule: warranty_end follows purchase/length/unit
        # unless the caller pinned it explicitly. Merge current row values so
        # a partial update still recomputes correctly.
        if touched_warranty and "warranty_end" not in fields:
            merged = {**row, **fields}
            end = compute_warranty_end(
                _parse_date(merged.get("purchase_date")),
                merged.get("warranty_length"),
                merged.get("warranty_unit"),
                explicit_end,
            )
            fields["warranty_end"] = end.isoformat() if end else None

        if not fields:
            return (
                "Nothing to change: pass at least one field to update (name, "
                "brand, model, description, serial_number, product_number, "
                "purchase_date, warranty_length, warranty_unit, warranty_end). "
                "An empty string clears an optional field."
            )

        updates = ", ".join(f"{key} = ?" for key in fields)
        params = list(fields.values()) + [row["id"]]
        await db.execute(
            f"UPDATE devices SET {updates}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            params,
        )
        await db.commit()

        updated = await _get_device_row(db, row["id"])
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM manuals WHERE device_id = ?", (row["id"],)
        )
        count = (await cursor.fetchone())["n"]

    changed = ", ".join(sorted(fields))
    return (
        f"Updated device id={row['id']} ({changed}): "
        f"{_device_line(updated, manual_count=count)}"
        + device_link_note(updated["name"], row["id"])
    )


async def _delete_guidance(
    args: Dict[str, Any], chat_device_id: Optional[int]
) -> str:
    """Refuse a delete politely and hand back an editor link for the user."""
    async with get_db_context() as db:
        if args.get("device_id") in (None, "") and chat_device_id is not None:
            args = {**args, "device_id": chat_device_id}
        row, err = await _load_scoped_device(db, args.get("device_id"), chat_device_id)
        if err:
            return err

    # The visible text is the device name on purpose - users recognise
    # "Kitchen Fridge", not "Edit Device #7" (the model must pass this link
    # through verbatim, see the instruction below).
    link = f"[{_link_label(row['name'])}](#edit-device-{row['id']})"
    return (
        f"Device id={row['id']} (\"{row['name']}\") was NOT deleted: HomeStew "
        "never lets the assistant delete devices because that also removes "
        "the manuals and custom attributes the user stored. Tell the user to "
        f"confirm it themselves - include this link so one click opens its "
        f"editor in the Devices tab, where the Delete button lives: {link}. "
        "Pass the link through verbatim with the device name as its visible "
        "text (never 'Edit Device #<id>'). Do not claim anything was deleted."
    )


async def _add_attribute(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    attr_name = _clean_text(args.get("attribute_name"), 100)
    attr_value = _clean_text(args.get("attribute_value"), 500)
    if not attr_name or not attr_value:
        return (
            "Error: 'attribute_name' and 'attribute_value' are both required "
            "to add a custom attribute."
        )

    async with get_db_context() as db:
        if args.get("device_id") in (None, "") and chat_device_id is not None:
            args = {**args, "device_id": chat_device_id}
        row, err = await _load_scoped_device(db, args.get("device_id"), chat_device_id)
        if err:
            return err

        cursor = await db.execute(
            """
            INSERT INTO device_attributes (device_id, attribute_name, attribute_value)
            VALUES (?, ?, ?)
            RETURNING id
            """,
            (row["id"], attr_name, attr_value),
        )
        attr_id = (await cursor.fetchone())["id"]
        await db.commit()

    return (
        f'Added attribute id={attr_id} to device id={row["id"]} '
        f'("{row["name"]}"): {attr_name}: {attr_value}'
        + device_link_note(row["name"], row["id"])
    )


async def _remove_attribute(
    args: Dict[str, Any], chat_device_id: Optional[int]
) -> str:
    async with get_db_context() as db:
        if args.get("device_id") in (None, "") and chat_device_id is not None:
            args = {**args, "device_id": chat_device_id}
        row, err = await _load_scoped_device(db, args.get("device_id"), chat_device_id)
        if err:
            return err

        attrs = await _get_attributes(db, row["id"])
        target_ids = []

        raw_attr_id = args.get("attribute_id")
        if raw_attr_id not in (None, ""):
            try:
                attr_id = int(raw_attr_id)
            except (TypeError, ValueError):
                return "Error: 'attribute_id' must be a number. Use action='list' to see attribute ids."
            target_ids = [a["id"] for a in attrs if a["id"] == attr_id]
            if not target_ids:
                return (
                    f"Error: device id={row['id']} has no attribute id={attr_id}. "
                    "Use action='list' to see its real attribute ids."
                )
        else:
            name = _clean_text(args.get("attribute_name"), 100).lower()
            if not name:
                return (
                    "Error: 'attribute_name' or 'attribute_id' is required to "
                    "remove an attribute."
                )
            target_ids = [a["id"] for a in attrs if a["attribute_name"].lower() == name]
            if not target_ids:
                listed = ", ".join(a["attribute_name"] for a in attrs) or "(none)"
                return (
                    f"Error: device id={row['id']} has no attribute named "
                    f"'{args.get('attribute_name')}'. Its attributes are: {listed}."
                )
            if len(target_ids) > 1:
                detail = ", ".join(
                    f"id={a['id']}: {a['attribute_value']}"
                    for a in attrs
                    if a["id"] in target_ids
                )
                return (
                    f"Error: several attributes share the name "
                    f"'{args.get('attribute_name')}' ({detail}). Retry with "
                    "the exact attribute_id."
                )

        await db.execute(
            f"DELETE FROM device_attributes WHERE id = ?", (target_ids[0],)
        )
        await db.commit()
        removed = next(a for a in attrs if a["id"] == target_ids[0])

    return (
        f'Removed attribute "{removed["attribute_name"]}" from device '
        f'id={row["id"]} ("{row["name"]}").'
        + device_link_note(row["name"], row["id"])
    )


def _identify_tokens(query: Any) -> Tuple[List[str], str]:
    """(loose tokens, full phrase) of a resolution query, lowercased.

    Quoted runs are kept as one phrase token; stopwords and 1-char tokens
    are dropped - they would match half the registry without identifying
    anything. Returns ([], '') for an empty/stopword-only query.
    """
    raw = str(query or "").lower()
    phrase_tokens: List[str] = []
    for chunk in re.findall(r'"([^"]+)"', raw):
        toks = _tokenize_words(chunk)
        if toks:
            phrase_tokens.append(" ".join(toks))
    loose = [
        t
        for t in _tokenize_words(re.sub(r'"[^"]*"', " ", raw))
        if len(t) > 1 and t not in _IDENTIFY_STOPWORDS
    ]
    # A phrase is just a strong hint; its words also join the loose set so a
    # device matching only some of them can still surface.
    tokens = list(dict.fromkeys(loose + [t for p in phrase_tokens for t in p.split()]))
    return tokens, " ".join(phrase_tokens)


def _tokenize_words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", text)


def _field_hit(token: str, field_tokens: set) -> bool:
    """Token matches a field: exact token equality, or prefix for long tokens.

    Prefix matching (>=5 chars, so 'laptop' finds 'laptops') mirrors the
    search engine's mid-word rule while keeping short acronyms strict -
    'ram' must not match inside 'program'.
    """
    if token in field_tokens:
        return True
    return len(token) >= 5 and any(ft.startswith(token) for ft in field_tokens)


def _hits_identity(fields: Dict[str, List[str]], tokens: List[str]) -> bool:
    """True when every token appears somewhere in name/brand/model."""
    identity = set()
    for key in ("name", "brand", "model"):
        identity.update(fields[key])
    return all(_field_hit(t, identity) for t in tokens)


async def _search_devices(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    """Resolve a free-text device reference ('my work laptop') to real devices.

    This is the FIRST step of any device question: users name devices by
    nickname, not brand/model, and a manuals search for 'work laptop' finds
    nothing because no manual contains the user's nickname. Matching happens
    over each device's own entry (name/brand/model/description/attributes)
    with IDF-style token weights computed across the registry: words shared
    by every device ('home') carry no signal, distinctive ones identify a
    unit on their own.
    """
    tokens, phrase = _identify_tokens(args.get("query"))
    if not tokens:
        return (
            "Error: 'query' must name the device in the user's words, e.g. "
            "'work laptop'. Only question words were given."
        )

    async with get_db_context() as db:
        where = "WHERE id = ?" if chat_device_id is not None else ""
        params: Tuple = (chat_device_id,) if chat_device_id is not None else ()
        cursor = await db.execute(
            f"SELECT {_DEVICE_COLUMNS} FROM devices {where} ORDER BY id", params
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        attr_cursor = await db.execute(
            "SELECT device_id, attribute_name, attribute_value "
            "FROM device_attributes ORDER BY id"
        )
        attrs_by_device: Dict[int, List[str]] = {}
        for a in await attr_cursor.fetchall():
            attrs_by_device.setdefault(a["device_id"], []).append(
                f'{a["attribute_name"]}: {a["attribute_value"]}'
            )

    if not rows:
        return "No devices are registered yet, so none can match."

    # Per-device field texts (token sets for strict matching).
    enriched = []
    for row in rows:
        fields = {
            "name": _tokenize_words(str(row.get("name") or "").lower()),
            "brand": _tokenize_words(str(row.get("brand") or "").lower()),
            "model": _tokenize_words(str(row.get("model") or "").lower()),
        }
        attr_list = attrs_by_device.get(row["id"], [])
        other_text = " ".join(
            [
                str(row.get("description") or ""),
                str(row.get("serial_number") or ""),
                str(row.get("product_number") or ""),
            ]
            + attr_list
        ).lower()
        fields["other"] = _tokenize_words(other_text)
        full_text = " ".join(
            [
                str(row.get("name") or ""),
                str(row.get("brand") or ""),
                str(row.get("model") or ""),
                other_text,
            ]
        ).lower()
        all_tokens = (
            set(fields["name"]) | set(fields["brand"])
            | set(fields["model"]) | set(fields["other"])
        )
        enriched.append(
            (row, fields, " ".join(full_text.split()), attr_list, all_tokens)
        )

    # Tokens no device mentions at all ('memory', 'support' in "what memory
    # does my work laptop support") carry zero identifying signal: drop them
    # before weighting so they cannot dilute a real match's coverage.
    present = [t for t in tokens if any(_field_hit(t, e[4]) for e in enriched)]
    if not present:
        return (
            f"No registered device matches '{_clean_text(args.get('query'), 100)}'. "
            "Try again with different words from the user's question (drop "
            "question words, keep nouns like 'laptop' or 'fridge'), call "
            "action='list' to see every device, or fall back to a plain "
            "search_manuals query across all devices."
        )

    # Token weight = 1 - ((df-1) / total): a word unique to one device gets
    # ~1.0 and identifies it alone, a word in every device still keeps a
    # small positive share (a single-device registry must match at all).
    weights = {
        t: max(
            0.05,
            1.0 - (sum(1 for e in enriched if _field_hit(t, e[4])) - 1) / len(rows),
        )
        for t in present
    }
    total_weight = sum(weights.values()) or 1.0

    scored = []
    for row, fields, full_text, attr_list, all_tokens in enriched:
        hit_tokens = [t for t in present if _field_hit(t, all_tokens)]
        if not hit_tokens:
            continue
        coverage = sum(weights[t] for t in hit_tokens) / total_weight
        rare_hit = any(weights[t] >= 0.7 for t in hit_tokens)
        if not (rare_hit or coverage >= 0.5):
            continue
        # Tier: exact phrase > all identifying tokens inside name/brand/model
        # > partial (attribute/description-only) match.
        if phrase and phrase in full_text:
            tier = 0
        elif _hits_identity(fields, present):
            tier = 1
        else:
            tier = 2
        # Ascending sort: best (lowest) tier first, then highest coverage.
        scored.append((tier, -coverage, row, attr_list))

    ordered = sorted(scored, key=lambda item: item[:2])
    matches = [(row, attrs) for _t, _c, row, attrs in ordered[:5]]

    # A candidate that clearly dominates the rest is not ambiguous even when
    # weaker devices matched some words: 'work laptop' must resolve to the
    # device whose NAME holds both words, not ask about a monitor that only
    # matches 'work'. Dominance = strictly best tier, or (same tier) at least
    # double the runner-up's coverage while covering most of the query.
    if len(ordered) > 1:
        (t0, nc0), (t1, nc1) = ordered[0][:2], ordered[1][:2]
        c0, c1 = -nc0, -nc1
        dominant = t0 < t1 or (c0 >= 0.75 and c0 >= 2 * c1)
        if dominant:
            matches = matches[:1]

    if not matches:
        return (
            f"No registered device matches '{_clean_text(args.get('query'), 100)}'. "
            "Try again with different words from the user's question (drop "
            "question words, keep nouns like 'laptop' or 'fridge'), call "
            "action='list' to see every device, or fall back to a plain "
            "search_manuals query across all devices."
        )

    lines = []
    for row, attr_list in matches:
        line = f'- device_id={row["id"]}: {_device_line(row)}'
        if attr_list:
            line += " | attributes: " + "; ".join(attr_list)
        lines.append(line)

    if len(matches) == 1:
        row = matches[0][0]
        return (
            f"Found exactly ONE device matching the question:\n"
            + "\n".join(lines)
            + f"\nThis is the device the user means. Rewrite their manual "
            f'search query to use its brand and model instead of the ' \
            f'nickname: "{row["brand"]} {row["model"]} <the spec words from '
            'the question>" (e.g. "what memory does my work laptop support" '
            f'-> "{row["brand"]} {row["model"]} memory"), then call '
            f'search_manuals with device_id={row["id"]}. Check the '
            'attributes above first - one may already answer the question '
            'without any search. When you mention this device in your '
            "answer, include its link verbatim: "
            + device_link_markdown(row["name"], row["id"])
        )

    return (
        f"{len(matches)} devices match the question - it is AMBIGUOUS:\n"
        + "\n".join(lines)
        + "\nAsk the user which of these they mean and stop; do NOT search "
        "manuals or guess one yet. When answering, mention each candidate "
        "with its link (verbatim) so the user can open it: "
        + ", ".join(
            device_link_markdown(row["name"], row["id"]) for row, _attrs in matches
        )
    )


async def _list(args: Dict[str, Any], chat_device_id: Optional[int]) -> str:
    """List devices with ids, manual counts and attributes."""
    filter_device = None
    if chat_device_id is not None:
        filter_device = chat_device_id
    elif args.get("device_id") not in (None, ""):
        filter_device = _coerce_device_id(args.get("device_id"))
        async with get_db_context() as db:
            # Small models invent ids; an unknown one would list nothing and
            # the model would report an empty registry. Say what happened.
            if filter_device == -1 or not await _get_device_row(db, filter_device):
                return (
                    f"Error: device_id={args.get('device_id')} does not exist. "
                    "Omit it to list every device."
                )

    async with get_db_context() as db:
        where = "WHERE d.id = ?" if filter_device is not None else ""
        params = (filter_device,) if filter_device is not None else ()
        cursor = await db.execute(
            f"""
            SELECT {_DEVICE_COLUMNS_D}, COUNT(m.id) AS manual_count
            FROM devices d
            LEFT JOIN manuals m ON d.id = m.device_id
            {where}
            GROUP BY d.id
            ORDER BY d.id
            """,
            params,
        )
        rows = [dict(r) for r in await cursor.fetchall()]

        attr_cursor = await db.execute(
            "SELECT id, device_id, attribute_name, attribute_value "
            "FROM device_attributes ORDER BY id"
        )
        attrs_by_device: Dict[int, list] = {}
        for a in await attr_cursor.fetchall():
            attrs_by_device.setdefault(a["device_id"], []).append(dict(a))

    if not rows:
        return (
            "No devices are registered yet. The user can add one with the "
            "'+ New Device' button, or you can create one with action='create'."
        )

    lines = []
    for r in rows[:30]:
        line = f'- device_id={r["id"]}: {_device_line(r, manual_count=r["manual_count"])}'
        attrs = attrs_by_device.get(r["id"], [])
        if attrs:
            line += " | attributes: " + "; ".join(
                f'id={a["id"]} {a["attribute_name"]}: {a["attribute_value"]}'
                for a in attrs
            )
        lines.append(line)

    total = len(rows)
    shown = len(lines)
    header = f"{total} device(s)" + (f" (showing first {shown}):" if total > shown else ":")
    return "\n".join([header] + lines)


async def execute_device_tool(
    args: Dict[str, Any], chat_device_id: Optional[int]
) -> str:
    """Run one manage_devices call and return the text report for the LLM."""
    action = str(args.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        return (
            f"Error: unknown action '{action}'. Use one of: "
            + ", ".join(_ACTIONS)
            + "."
        )

    logger.info(
        "Executing device tool: %s (device filter=%s)", action, chat_device_id
    )
    try:
        if action == "search_devices":
            return await _search_devices(args, chat_device_id)
        if action == "create":
            return await _create(args, chat_device_id)
        if action == "update":
            return await _update(args, chat_device_id)
        if action == "delete":
            # Never destructive - see the module docstring.
            return await _delete_guidance(args, chat_device_id)
        if action == "add_attribute":
            return await _add_attribute(args, chat_device_id)
        if action == "remove_attribute":
            return await _remove_attribute(args, chat_device_id)
        return await _list(args, chat_device_id)
    except Exception as exc:  # noqa: BLE001 - report to the model, not a crash
        logger.error("Device tool call failed: %s", exc)
        return f"Error executing device action '{action}': {exc}"


def parse_tool_arguments(tool_call: Any) -> Dict[str, Any]:
    """Extract an argument dict from an SDK object or plain-dict tool call."""
    if hasattr(tool_call, "function"):
        raw = tool_call.function.arguments
    else:
        raw = tool_call["function"]["arguments"]
    try:
        args = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return args if isinstance(args, dict) else {}
