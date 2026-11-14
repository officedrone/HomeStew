"""Device management API endpoints."""
import logging
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from typing import List

from homestew.config import settings
from homestew.db import get_db_context
from homestew.models.schemas import (
    Device,
    DeviceAttribute,
    DeviceCategoryUpdate,
    DeviceCreate,
    DeviceIconOption,
    DevicePhoto,
    DevicePhotoSuggestionDecision,
    DevicePhotoUpdate,
    DeviceReorderRequest,
    DeviceResponse,
    Manual,
)
from homestew.services.device_icons import search_icon_keys
from homestew.services.indexer import index_manual
from homestew.services.photo_images import sniff_image
from homestew.services.warranty import warranty_fields

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["devices"])

# Maximum accepted size for an uploaded manual (100 MB).
MAX_UPLOAD_SIZE = 100 * 1024 * 1024

# Columns shared by every device SELECT so responses stay consistent.
# RETURNING clauses don't allow table prefixes, hence the plain variant.
DEVICE_COLUMNS = (
    "d.id, d.name, d.brand, d.model, d.description, d.serial_number, "
    "d.product_number, d.purchase_date, d.warranty_length, d.warranty_unit, "
    "d.warranty_end, d.icon, d.category_id, d.sort_order, d.created_at, d.updated_at"
)
DEVICE_COLUMNS_PLAIN = DEVICE_COLUMNS.replace("d.", "")

# Joined onto every device SELECT so responses carry the category name too.
CATEGORY_JOIN = "LEFT JOIN categories c ON d.category_id = c.id"


async def _next_sort_order(db, category_id) -> int:
    """Sort position for a device entering ``category_id``: first in its bucket.

    New / re-categorised devices appear at the TOP of their section (matching
    the previous newest-first grid), while drag-and-drop assigns explicit
    0..n-1 positions via PUT /devices/reorder.
    """
    cursor = await db.execute(
        "SELECT COALESCE(MIN(sort_order), 1) - 1 AS s FROM devices "
        "WHERE (? IS NULL AND category_id IS NULL OR category_id = ?)",
        (category_id, category_id),
    )
    row = await cursor.fetchone()
    return row["s"]


def _warranty_fields(device: DeviceCreate) -> tuple:
    """Return (purchase_date, warranty_length, warranty_unit, warranty_end).

    ``warranty_end`` is taken from the request when provided; otherwise it is
    computed from purchase date + warranty length/unit (months/years clamp
    the day-of-month, e.g. Jan 31 + 1 month -> Feb 28). The arithmetic lives
    in services/warranty.py so the LLM device tool applies the same rule.
    """
    return warranty_fields(
        device.purchase_date,
        device.warranty_length,
        device.warranty_unit,
        device.warranty_end,
    )


def _row_to_response(
    row,
    manual_count: int,
    attributes=None,
    photo_count: int = 0,
    unanalyzed_photo_count: int = 0,
) -> DeviceResponse:
    """Build a DeviceResponse from a device row (with optional attributes).

    ``category_name`` is only present when the SELECT joined ``categories``;
    plain RETURNING rows (create/update) leave it null and the frontend
    resolves the name from its cached category list.
    """
    return DeviceResponse(
        id=row['id'],
        name=row['name'],
        brand=row['brand'],
        model=row['model'],
        description=row['description'],
        serial_number=row['serial_number'],
        product_number=row['product_number'],
        purchase_date=row['purchase_date'],
        warranty_length=row['warranty_length'],
        warranty_unit=row['warranty_unit'],
        warranty_end=row['warranty_end'],
        icon=row['icon'],
        category_id=row['category_id'],
        category_name=row['category_name'] if 'category_name' in row.keys() else None,
        created_at=row['created_at'],
        updated_at=row['updated_at'],
        manual_count=manual_count or 0,
        attributes=attributes or [],
        photo_count=photo_count or 0,
        unanalyzed_photo_count=unanalyzed_photo_count or 0,
    )


@router.post("", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_device(device: DeviceCreate):
    """Add a new device to HomeStew."""
    purchase_date, warranty_length, warranty_unit, warranty_end = _warranty_fields(device)
    async with get_db_context() as db:
        sort_order = await _next_sort_order(db, device.category_id)
        cursor = await db.execute(
            f"""
            INSERT INTO devices (name, brand, model, description, serial_number,
                product_number, purchase_date, warranty_length, warranty_unit, warranty_end, icon,
                category_id, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING {DEVICE_COLUMNS_PLAIN}
            """,
            (
                device.name, 
                device.brand, 
                device.model, 
                device.description or "",
                device.serial_number,
                device.product_number,
                purchase_date,
                warranty_length,
                warranty_unit,
                warranty_end,
                device.icon,
                device.category_id,
                sort_order,
            )
        )
        
        row = await cursor.fetchone()
        await db.commit()

        resp = _row_to_response(row, 0)
        # RETURNING has no join, so fetch the category name for the response.
        if device.category_id is not None:
            cat_cur = await db.execute(
                "SELECT name FROM categories WHERE id = ?", (device.category_id,)
            )
            cat_row = await cat_cur.fetchone()
            resp.category_name = cat_row["name"] if cat_row else None
        return resp


@router.get("", response_model=List[DeviceResponse])
async def list_devices():
    """List all devices with their manual counts."""
    async with get_db_context() as db:
        cursor = await db.execute(
            f"""
            SELECT 
                {DEVICE_COLUMNS},
                c.name AS category_name,
                COUNT(m.id) as manual_count,
                (SELECT COUNT(*) FROM device_photos p WHERE p.device_id = d.id) AS photo_count,
                (SELECT COUNT(*) FROM device_photos p
                 WHERE p.device_id = d.id AND p.ai_notes IS NULL) AS unanalyzed_photo_count
            FROM devices d
            LEFT JOIN manuals m ON d.id = m.device_id
            {CATEGORY_JOIN}
            GROUP BY d.id
            ORDER BY d.sort_order ASC, d.created_at DESC
            """
        )
        
        rows = await cursor.fetchall()
        
        # Fetch every custom attribute in one query and bucket them by device,
        # so the sidebar accordion can show them without per-device requests.
        attr_cursor = await db.execute(
            "SELECT id, device_id, attribute_name, attribute_value, created_at FROM device_attributes"
        )
        attrs_by_device = {}
        for attr in await attr_cursor.fetchall():
            attrs_by_device.setdefault(attr['device_id'], []).append(
                DeviceAttribute(
                    id=attr['id'],
                    device_id=attr['device_id'],
                    attribute_name=attr['attribute_name'],
                    attribute_value=attr['attribute_value'],
                    created_at=attr['created_at']
                )
            )
        
        return [
            _row_to_response(
                row,
                row['manual_count'],
                attrs_by_device.get(row['id'], []),
                photo_count=row['photo_count'],
                unanalyzed_photo_count=row['unanalyzed_photo_count'],
            )
            for row in rows
        ]


@router.get("/icons", response_model=List[DeviceIconOption])
async def list_device_icons(q: str = ""):
    """Return the selectable device icons, optionally filtered by ``q``.

    The picker dialog ships the whole catalog and filters it client-side for
    instant results; this endpoint exists for programmatic callers (and any
    future UI) that want the same search the manage_devices tool's
    ``search_icons`` action applies. An empty ``q`` returns every icon.
    Registered before ``/{device_id}`` so the literal path wins.
    """
    return [
        DeviceIconOption(key=key, label=label)
        for key, label in search_icon_keys(q)
    ]


@router.put("/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_devices(payload: DeviceReorderRequest):
    """Persist one bucket's order (and membership) after a drag-and-drop.

    Every listed device is moved into ``category_id`` (null = Uncategorized)
    and gets sort_order = its position in the list, so moving a card between
    sections is a single call with the target bucket's full id list. The
    source bucket keeps its relative order untouched. Registered before
    ``/{device_id}`` so the literal path wins over the int-typed route.
    """
    async with get_db_context() as db:
        if payload.device_ids:
            placeholders = ",".join("?" * len(payload.device_ids))
            cursor = await db.execute(
                f"SELECT id FROM devices WHERE id IN ({placeholders})",
                list(payload.device_ids),
            )
            found = {row["id"] for row in await cursor.fetchall()}
            missing = [did for did in payload.device_ids if did not in found]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unknown device ids: {missing}",
                )
        for position, device_id in enumerate(payload.device_ids):
            await db.execute(
                "UPDATE devices SET category_id = ?, sort_order = ? WHERE id = ?",
                (payload.category_id, position, device_id),
            )
        await db.commit()


@router.put("/{device_id}/category", status_code=status.HTTP_204_NO_CONTENT)
async def move_device_category(device_id: int, payload: DeviceCategoryUpdate):
    """Move one device to a category (null = Uncategorized), first in its list.

    Narrow counterpart to the full-replace PUT /{device_id}: drag-and-drop
    uses /reorder, this serves simple single-device moves (and clients that
    don't know the bucket's full order).
    """
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT id, category_id FROM devices WHERE id = ?", (device_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found",
            )
        if row["category_id"] != payload.category_id:
            sort_order = await _next_sort_order(db, payload.category_id)
            await db.execute(
                "UPDATE devices SET category_id = ?, sort_order = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (payload.category_id, sort_order, device_id),
            )
        else:
            await db.execute(
                "UPDATE devices SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (device_id,),
            )
        await db.commit()


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(device_id: int):
    """Get a specific device with details."""
    async with get_db_context() as db:
        cursor = await db.execute(
            f"""
            SELECT 
                {DEVICE_COLUMNS},
                c.name AS category_name,
                COUNT(m.id) as manual_count,
                (SELECT COUNT(*) FROM device_photos p WHERE p.device_id = d.id) AS photo_count,
                (SELECT COUNT(*) FROM device_photos p
                 WHERE p.device_id = d.id AND p.ai_notes IS NULL) AS unanalyzed_photo_count
            FROM devices d
            LEFT JOIN manuals m ON d.id = m.device_id
            {CATEGORY_JOIN}
            WHERE d.id = ?
            GROUP BY d.id
            """,
            (device_id,)
        )
        
        row = await cursor.fetchone()
        
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Get device attributes
        attr_cursor = await db.execute(
            "SELECT * FROM device_attributes WHERE device_id = ?", (device_id,)
        )
        attrs = await attr_cursor.fetchall()
        
        attributes = [
            DeviceAttribute(
                id=attr['id'],
                device_id=attr['device_id'],
                attribute_name=attr['attribute_name'],
                attribute_value=attr['attribute_value'],
                created_at=attr['created_at']
            )
            for attr in attrs
        ]
        
        return _row_to_response(
            row,
            row['manual_count'],
            attributes,
            photo_count=row['photo_count'],
            unanalyzed_photo_count=row['unanalyzed_photo_count'],
        )


@router.put("/{device_id}", response_model=DeviceResponse)
async def update_device(device_id: int, device: DeviceCreate):
    """Update an existing device."""
    async with get_db_context() as db:
        # Check if device exists (and whether its category is changing — a
        # moved device jumps to the top of its new bucket).
        cursor = await db.execute(
            "SELECT id, category_id FROM devices WHERE id = ?", (device_id,)
        )
        existing = await cursor.fetchone()
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Update device (warranty_end auto-computed from purchase date + length)
        purchase_date, warranty_length, warranty_unit, warranty_end = _warranty_fields(device)
        sort_order = (
            await _next_sort_order(db, device.category_id)
            if existing["category_id"] != device.category_id
            else None
        )
        if sort_order is None:
            cursor = await db.execute(
                f"""
                UPDATE devices 
                SET name = ?, brand = ?, model = ?, description = ?, serial_number = ?, product_number = ?,
                    purchase_date = ?, warranty_length = ?, warranty_unit = ?, warranty_end = ?, icon = ?,
                    category_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                RETURNING {DEVICE_COLUMNS_PLAIN}
                """,
                (
                    device.name, 
                    device.brand, 
                    device.model, 
                    device.description or "",
                    device.serial_number,
                    device.product_number,
                    purchase_date,
                    warranty_length,
                    warranty_unit,
                    warranty_end,
                    device.icon,
                    device.category_id,
                    device_id
                )
            )
        else:
            cursor = await db.execute(
                f"""
                UPDATE devices 
                SET name = ?, brand = ?, model = ?, description = ?, serial_number = ?, product_number = ?,
                    purchase_date = ?, warranty_length = ?, warranty_unit = ?, warranty_end = ?, icon = ?,
                    category_id = ?, sort_order = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                RETURNING {DEVICE_COLUMNS_PLAIN}
                """,
                (
                    device.name, 
                    device.brand, 
                    device.model, 
                    device.description or "",
                    device.serial_number,
                    device.product_number,
                    purchase_date,
                    warranty_length,
                    warranty_unit,
                    warranty_end,
                    device.icon,
                    device.category_id,
                    sort_order,
                    device_id
                )
            )
        
        row = await cursor.fetchone()
        await db.commit()
        
        # Get updated manual count
        cursor = await db.execute(
            "SELECT COUNT(*) as count FROM manuals WHERE device_id = ?", (device_id,)
        )
        count_row = await cursor.fetchone()
        
        # Get device attributes
        attr_cursor = await db.execute(
            "SELECT * FROM device_attributes WHERE device_id = ?", (device_id,)
        )
        attrs = await attr_cursor.fetchall()
        
        attributes = [
            DeviceAttribute(
                id=attr['id'],
                device_id=attr['device_id'],
                attribute_name=attr['attribute_name'],
                attribute_value=attr['attribute_value'],
                created_at=attr['created_at']
            )
            for attr in attrs
        ]
        
        resp = _row_to_response(row, count_row['count'], attributes)
        # RETURNING has no join, so fetch the category name for the response.
        if device.category_id is not None:
            cat_cur = await db.execute(
                "SELECT name FROM categories WHERE id = ?", (device.category_id,)
            )
            cat_row = await cat_cur.fetchone()
            resp.category_name = cat_row["name"] if cat_row else None
        return resp


@router.post("/{device_id}/attributes", response_model=DeviceAttribute)
async def add_device_attribute(device_id: int, attribute_name: str, attribute_value: str):
    """Add a custom attribute to a device."""
    async with get_db_context() as db:
        # Check if device exists
        cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Add attribute
        cursor = await db.execute(
            """
            INSERT INTO device_attributes (device_id, attribute_name, attribute_value)
            VALUES (?, ?, ?)
            RETURNING id, device_id, attribute_name, attribute_value, created_at
            """,
            (device_id, attribute_name, attribute_value)
        )
        
        row = await cursor.fetchone()
        await db.commit()
        
        return DeviceAttribute(
            id=row['id'],
            device_id=row['device_id'],
            attribute_name=row['attribute_name'],
            attribute_value=row['attribute_value'],
            created_at=row['created_at']
        )


@router.delete("/{device_id}/attributes/{attribute_id}")
async def remove_device_attribute(device_id: int, attribute_id: int):
    """Remove a custom attribute from a device."""
    async with get_db_context() as db:
        # Check if attribute exists and belongs to this device
        cursor = await db.execute(
            "SELECT id FROM device_attributes WHERE id = ? AND device_id = ?", 
            (attribute_id, device_id)
        )
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Attribute {attribute_id} not found for this device"
            )
        
        # Delete attribute
        await db.execute(
            "DELETE FROM device_attributes WHERE id = ?", (attribute_id,)
        )
        await db.commit()


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------

# Columns shared by every device_photos SELECT (filepath stays server-side;
# the file endpoint derives it from the row internally).
PHOTO_COLUMNS = (
    "id, device_id, filename, label, notes, ai_notes, ai_analyzed_at, "
    "pending_ai_notes, pending_reason, proposed_at, mime_type, width, "
    "height, size_bytes, created_at, updated_at"
)

# Upload extension -> canonical MIME, used only as a fallback hint; the
# authoritative type comes from sniffing the bytes.
_PHOTO_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _photo_to_model(row) -> DevicePhoto:
    return DevicePhoto(**{k: row[k] for k in row.keys()})


async def _require_device(db, device_id: int):
    cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
    if not await cursor.fetchone():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id} not found"
        )


async def _require_photo(db, device_id: int, photo_id: int):
    cursor = await db.execute(
        "SELECT id, filepath, pending_ai_notes FROM device_photos "
        "WHERE id = ? AND device_id = ?",
        (photo_id, device_id),
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo {photo_id} not found for device {device_id}"
        )
    return row


@router.get("/{device_id}/photos", response_model=List[DevicePhoto])
async def list_device_photos(device_id: int):
    """List the photos attached to a device, newest first."""
    async with get_db_context() as db:
        await _require_device(db, device_id)
        cursor = await db.execute(
            f"SELECT {PHOTO_COLUMNS} FROM device_photos WHERE device_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (device_id,),
        )
        return [_photo_to_model(row) for row in await cursor.fetchall()]


@router.post("/{device_id}/photos", response_model=DevicePhoto, status_code=status.HTTP_201_CREATED)
async def upload_device_photo(
    device_id: int,
    file: UploadFile = File(...),
    label: str = Form(""),
    notes: str = Form(""),
    analyze: bool = Form(False),
):
    """Attach a photo to a device (e.g. the filter it takes).

    The browser downscales before upload; the server only validates: magic
    bytes must identify a PNG/JPEG/WebP/GIF, the payload must fit
    ``PHOTO_MAX_SIZE_MB`` and its header dimensions ``PHOTO_MAX_DIM_PX``.
    When ``analyze`` (or ``PHOTO_AUTO_ANALYZE_DEFAULT``) is set and the LLM
    has vision, a background analysis writes a *suggestion* into the pending
    fields - it never lands in ``ai_notes`` without the user confirming.
    """
    async with get_db_context() as db:
        await _require_device(db, device_id)

    data = await file.read()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is empty."
        )
    max_bytes = settings.PHOTO_MAX_SIZE_MB * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Photo too large. Maximum size is {settings.PHOTO_MAX_SIZE_MB} MB."
        )

    sniffed = sniff_image(data)
    if sniffed is None:
        # A client that skipped the canvas re-encode can still upload a
        # format we recognize by extension; refuse everything else outright.
        guessed = _PHOTO_EXT_MIME.get(Path(file.filename or "").suffix.lower())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "The file does not appear to be a supported image "
                f"(PNG, JPEG, WebP or GIF).{'' if guessed is None else ' Detected extension suggests ' + guessed + ', but the content did not match.'}"
            )
        )
    mime_type, width, height = sniffed

    if width and height and max(width, height) > settings.PHOTO_MAX_DIM_PX:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Photo is {width}x{height}px; the maximum dimension is "
                f"{settings.PHOTO_MAX_DIM_PX}px. Shrink it and try again."
            )
        )

    # Store under the device's photos directory with a safe unique name.
    photo_dir = settings.DEVICES_DIR / str(device_id) / "photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _unique_filename(photo_dir, _sanitize_photo_filename(file.filename or "", mime_type))
    filepath = photo_dir / safe_name
    try:
        filepath.write_bytes(data)
    except OSError as exc:
        logger.error(f"Failed to save uploaded photo for device {device_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store the uploaded file."
        )

    async with get_db_context() as db:
        cursor = await db.execute(
            """
            INSERT INTO device_photos (device_id, filename, filepath, label, notes,
                mime_type, width, height, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                device_id, safe_name, str(filepath),
                (label or "").strip() or None,
                (notes or "").strip() or None,
                mime_type, width, height, len(data),
            )
        )
        photo_id = (await cursor.fetchone())["id"]
        await db.commit()

        if analyze or settings.PHOTO_AUTO_ANALYZE_DEFAULT:
            # Analysis failures must never lose the upload, and the response
            # should not wait on the LLM round-trip.
            import asyncio

            from homestew.services.photo_analyzer import analyze_photo_job

            asyncio.create_task(analyze_photo_job(photo_id, device_id))

        cursor = await db.execute(
            f"SELECT {PHOTO_COLUMNS} FROM device_photos WHERE id = ?", (photo_id,)
        )
        return _photo_to_model(await cursor.fetchone())


@router.get("/{device_id}/photos/{photo_id}", response_model=DevicePhoto)
async def get_device_photo(device_id: int, photo_id: int):
    """Fetch one photo's metadata (label, notes, AI notes, pending suggestion)."""
    async with get_db_context() as db:
        await _require_photo(db, device_id, photo_id)
        cursor = await db.execute(
            f"SELECT {PHOTO_COLUMNS} FROM device_photos WHERE id = ?", (photo_id,)
        )
        return _photo_to_model(await cursor.fetchone())


@router.put("/{device_id}/photos/{photo_id}", response_model=DevicePhoto)
async def update_device_photo(device_id: int, photo_id: int, update: DevicePhotoUpdate):
    """Edit a photo's user-owned label/notes (AI fields are untouched)."""
    async with get_db_context() as db:
        await _require_photo(db, device_id, photo_id)
        await db.execute(
            "UPDATE device_photos SET label = ?, notes = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (
                (update.label or "").strip() or None,
                (update.notes or "").strip() or None,
                photo_id,
            ),
        )
        await db.commit()
        cursor = await db.execute(
            f"SELECT {PHOTO_COLUMNS} FROM device_photos WHERE id = ?", (photo_id,)
        )
        return _photo_to_model(await cursor.fetchone())


@router.delete("/{device_id}/photos/{photo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device_photo(device_id: int, photo_id: int):
    """Delete a photo: file on disk and DB row."""
    async with get_db_context() as db:
        photo = await _require_photo(db, device_id, photo_id)
        try:
            Path(photo["filepath"]).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"Could not delete photo file {photo['filepath']}: {exc}")
        await db.execute("DELETE FROM device_photos WHERE id = ?", (photo_id,))
        await db.commit()


@router.get("/{device_id}/photos/{photo_id}/file")
async def get_device_photo_file(device_id: int, photo_id: int):
    """Serve the stored image inline (thumbnails and lightbox both use it).

    Same containment guard as the manual file endpoint: only files inside
    the managed devices directory are served.
    """
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT filepath, mime_type FROM device_photos WHERE id = ? AND device_id = ?",
            (photo_id, device_id),
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo {photo_id} not found for device {device_id}",
        )

    path = Path(row["filepath"])
    try:
        resolved = path.resolve()
        allowed_root = settings.DEVICES_DIR.resolve()
        if not str(resolved).startswith(str(allowed_root)) or not resolved.is_file():
            raise ValueError("outside devices dir")
    except (OSError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Photo file not found on disk",
        )

    return FileResponse(
        str(resolved),
        media_type=row["mime_type"],
        content_disposition_type="inline",
    )


@router.post("/{device_id}/photos/{photo_id}/analyze", response_model=DevicePhoto)
async def analyze_device_photo(device_id: int, photo_id: int):
    """Run a vision pass on a photo now (the "process later" checkbox).

    The result is stored as a pending suggestion; the response returns the
    updated row so the UI can render the confirm/decline card immediately.
    """
    if not settings.LLM_SUPPORTS_VISION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vision is not enabled in Settings > LLM.",
        )
    async with get_db_context() as db:
        await _require_photo(db, device_id, photo_id)

    from homestew.services.photo_analyzer import analyze_photo

    try:
        result = await analyze_photo(photo_id, device_id)
    except Exception as exc:
        logger.error(f"Photo analysis failed for photo {photo_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Vision analysis failed: {exc}",
        )

    async with get_db_context() as db:
        if result.changed and result.notes:
            await db.execute(
                "UPDATE device_photos SET pending_ai_notes = ?, pending_reason = ?, "
                "proposed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (result.notes, result.reason, photo_id),
            )
            await db.commit()
        cursor = await db.execute(
            f"SELECT {PHOTO_COLUMNS} FROM device_photos WHERE id = ?", (photo_id,)
        )
        return _photo_to_model(await cursor.fetchone())


@router.post("/{device_id}/photos/{photo_id}/suggestion", response_model=DevicePhoto)
async def decide_photo_suggestion(
    device_id: int, photo_id: int, decision: DevicePhotoSuggestionDecision
):
    """Accept or decline a pending AI-notes suggestion.

    Accept copies the proposal into ``ai_notes`` (the only way that field is
    ever written) and timestamps it; decline just clears the pending fields.
    """
    async with get_db_context() as db:
        photo = await _require_photo(db, device_id, photo_id)
        if not photo["pending_ai_notes"]:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This photo has no pending suggestion.",
            )
        if decision.accept:
            await db.execute(
                "UPDATE device_photos SET ai_notes = pending_ai_notes, "
                "ai_analyzed_at = CURRENT_TIMESTAMP, pending_ai_notes = NULL, "
                "pending_reason = NULL, proposed_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (photo_id,),
            )
        else:
            await db.execute(
                "UPDATE device_photos SET pending_ai_notes = NULL, "
                "pending_reason = NULL, proposed_at = NULL WHERE id = ?",
                (photo_id,),
            )
        await db.commit()
        cursor = await db.execute(
            f"SELECT {PHOTO_COLUMNS} FROM device_photos WHERE id = ?", (photo_id,)
        )
        return _photo_to_model(await cursor.fetchone())


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(device_id: int):
    """Delete a device and all its manuals."""
    async with get_db_context() as db:
        # Check if device exists
        cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Delete device (cascades to manuals and attributes)
        await db.execute("DELETE FROM devices WHERE id = ?", (device_id,))

        # Runtime connections don't enable foreign keys, so photo rows are
        # removed explicitly (their files go with the directory below).
        await db.execute("DELETE FROM device_photos WHERE device_id = ?", (device_id,))

        # Drop the device's full-text index rows so search results don't keep
        # pointing at now-deleted manuals.
        cursor = await db.execute(
            "SELECT rowid FROM pdf_index WHERE device_id = ?",
            (device_id,)
        )
        for r in await cursor.fetchall():
            await db.execute("DELETE FROM pdf_index WHERE rowid = ?", (r["rowid"],))

        await db.commit()

    # The whole per-device directory (manuals + photos) is disposable once
    # the rows are gone; without this, files leak on disk after deletes.
    device_dir = settings.DEVICES_DIR / str(device_id)
    try:
        if device_dir.is_dir():
            shutil.rmtree(device_dir)
    except OSError as exc:
        logger.warning(f"Could not remove device directory {device_dir}: {exc}")


def _sanitize_filename(filename: str) -> str:
    """Keep only a safe basename for an uploaded file."""
    # Strip any client-provided path components and unsafe characters.
    name = Path(filename).name
    name = re.sub(r"[^\w.\- ]+", "_", name).strip() or "manual.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def _unique_filename(device_dir: Path, filename: str) -> str:
    """Avoid overwriting an existing manual with the same name."""
    candidate = device_dir / filename
    if not candidate.exists():
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while (device_dir / f"{stem}_{counter}{suffix}").exists():
        counter += 1
    return f"{stem}_{counter}{suffix}"


_PHOTO_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _sanitize_photo_filename(filename: str, mime_type: str) -> str:
    """Safe basename for an uploaded photo, extension from the sniffed type.

    The client's declared name is only a label; the extension always comes
    from what the bytes actually are so stored files match their MIME.
    """
    name = Path(filename).name
    stem = re.sub(r"[^\w.\- ]+", "_", Path(name).stem).strip() or "photo"
    return f"{stem}{_PHOTO_MIME_EXT.get(mime_type, '.bin')}"


@router.post("/{device_id}/manuals/upload", response_model=Manual, status_code=status.HTTP_201_CREATED)
async def upload_manual(device_id: int, file: UploadFile = File(...)):
    """Upload a PDF manual for a device.

    The file is stored exactly like an automatically downloaded manual
    (under ``data/devices/<id>/manuals/``), recorded in the ``manuals``
    table and indexed for full-text search.
    """
    async with get_db_context() as db:
        cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )

    # Validate content type / extension.
    filename = file.filename or "manual.pdf"
    is_pdf_type = (file.content_type or "").lower() == "application/pdf"
    if not (is_pdf_type or filename.lower().endswith(".pdf")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF files are supported."
        )

    # Read the file and enforce a size limit.
    data = await file.read()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is empty."
        )
    if len(data) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024 * 1024)} MB."
        )

    # Basic PDF magic-number check, same as the downloader does after fetching.
    if not data.startswith(b"%PDF"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The file does not appear to be a valid PDF."
        )

    # Save under the device's manuals directory, like downloaded manuals.
    device_dir = settings.DEVICES_DIR / str(device_id) / "manuals"
    device_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _unique_filename(device_dir, _sanitize_filename(filename))
    filepath = device_dir / safe_name

    try:
        filepath.write_bytes(data)
    except OSError as exc:
        logger.error(f"Failed to save uploaded manual for device {device_id}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store the uploaded file."
        )

    # Record in the database and index for search.
    async with get_db_context() as db:
        cursor = await db.execute(
            """
            INSERT INTO manuals (device_id, filename, filepath)
            VALUES (?, ?, ?)
            RETURNING id, device_id, filename, filepath, page_count, indexed_at
            """,
            (device_id, safe_name, str(filepath))
        )
        row = await cursor.fetchone()
        await db.commit()

    manual_id = row["id"]
    if settings.INDEX_AUTO_ON_UPLOAD:
        try:
            await index_manual(
                manual_id=manual_id,
                device_id=device_id,
                pdf_path=str(filepath),
                filename=safe_name,
            )
        except Exception as exc:  # indexing failure shouldn't lose the upload
            logger.error(f"Failed to index uploaded manual {safe_name}: {exc}")
    else:
        logger.info(
            f"Auto-indexing is off; uploaded manual {safe_name} needs a "
            "re-index from Settings > Search to become searchable"
        )

    return Manual(
        id=row["id"],
        device_id=row["device_id"],
        filename=row["filename"],
        filepath=row["filepath"],
        page_count=row["page_count"],
        indexed_at=row["indexed_at"],
    )


@router.delete("/{device_id}/manuals/{manual_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_manual(device_id: int, manual_id: int):
    """Delete a single manual: file on disk, DB row and its search index entries."""
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT id, filename, filepath FROM manuals WHERE id = ? AND device_id = ?",
            (manual_id, device_id)
        )
        manual = await cursor.fetchone()
        if not manual:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Manual {manual_id} not found for device {device_id}"
            )

        # Remove the stored PDF file (if it still exists on disk).
        try:
            Path(manual["filepath"]).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"Could not delete manual file {manual['filepath']}: {exc}")

        # Drop its full-text index rows via the manual_id link. Fall back to
        # device + filename for any legacy rows indexed before manual_id.
        cursor = await db.execute(
            "SELECT rowid FROM pdf_index WHERE manual_id = ?",
            (manual_id,)
        )
        rows = await cursor.fetchall()
        if not rows:
            cursor = await db.execute(
                "SELECT rowid FROM pdf_index WHERE device_id = ? AND filename = ?",
                (device_id, manual["filename"])
            )
            rows = await cursor.fetchall()
        for row in rows:
            await db.execute("DELETE FROM pdf_index WHERE rowid = ?", (row["rowid"],))

        await db.execute("DELETE FROM manuals WHERE id = ?", (manual_id,))
        await db.commit()
