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
from datetime import date
from typing import Any, Dict, Optional, Tuple

from homestew.db import get_db_context
from homestew.services.warranty import WARRANTY_UNITS, compute_warranty_end

logger = logging.getLogger(__name__)

_ACTIONS = (
    "create",
    "update",
    "list",
    "add_attribute",
    "remove_attribute",
    "delete",
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
    return (
        f"Created device id={new_id}: {_device_line(row, manual_count=0)}. "
        "Manuals are not attached by this tool - tell the user they can upload "
        "or fetch manuals for it in the Devices tab."
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
    return f"Updated device id={row['id']} ({changed}): {_device_line(updated, manual_count=count)}"


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

    link = f"[open the editor for \u201c{row['name']}\u201d](#edit-device-{row['id']})"
    return (
        f"Device id={row['id']} (\"{row['name']}\") was NOT deleted: HomeStew "
        "never lets the assistant delete devices because that also removes "
        "the manuals and custom attributes the user stored. Tell the user to "
        f"confirm it themselves - include this link so one click opens its "
        f"editor in the Devices tab, where the Delete button lives: {link}. "
        "Do not claim anything was deleted."
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
