"""Device management API endpoints."""
import logging
import re
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from typing import List

from homestew.config import settings
from homestew.db import get_db_context
from homestew.models.schemas import Device, DeviceCreate, DeviceResponse, DeviceAttribute, Manual
from homestew.services.indexer import index_manual
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
    "d.warranty_end, d.created_at, d.updated_at"
)
DEVICE_COLUMNS_PLAIN = DEVICE_COLUMNS.replace("d.", "")


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


def _row_to_response(row, manual_count: int, attributes=None) -> DeviceResponse:
    """Build a DeviceResponse from a device row (with optional attributes)."""
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
        created_at=row['created_at'],
        updated_at=row['updated_at'],
        manual_count=manual_count or 0,
        attributes=attributes or [],
    )


@router.post("", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_device(device: DeviceCreate):
    """Add a new device to HomeStew."""
    purchase_date, warranty_length, warranty_unit, warranty_end = _warranty_fields(device)
    async with get_db_context() as db:
        cursor = await db.execute(
            f"""
            INSERT INTO devices (name, brand, model, description, serial_number,
                product_number, purchase_date, warranty_length, warranty_unit, warranty_end)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            )
        )
        
        row = await cursor.fetchone()
        await db.commit()
        
        return _row_to_response(row, 0)


@router.get("", response_model=List[DeviceResponse])
async def list_devices():
    """List all devices with their manual counts."""
    async with get_db_context() as db:
        cursor = await db.execute(
            f"""
            SELECT 
                {DEVICE_COLUMNS},
                COUNT(m.id) as manual_count
            FROM devices d
            LEFT JOIN manuals m ON d.id = m.device_id
            GROUP BY d.id
            ORDER BY d.created_at DESC
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
            _row_to_response(row, row['manual_count'], attrs_by_device.get(row['id'], []))
            for row in rows
        ]


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(device_id: int):
    """Get a specific device with details."""
    async with get_db_context() as db:
        cursor = await db.execute(
            f"""
            SELECT 
                {DEVICE_COLUMNS},
                COUNT(m.id) as manual_count
            FROM devices d
            LEFT JOIN manuals m ON d.id = m.device_id
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
        
        return _row_to_response(row, row['manual_count'], attributes)


@router.put("/{device_id}", response_model=DeviceResponse)
async def update_device(device_id: int, device: DeviceCreate):
    """Update an existing device."""
    async with get_db_context() as db:
        # Check if device exists
        cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Update device (warranty_end auto-computed from purchase date + length)
        purchase_date, warranty_length, warranty_unit, warranty_end = _warranty_fields(device)
        cursor = await db.execute(
            f"""
            UPDATE devices 
            SET name = ?, brand = ?, model = ?, description = ?, serial_number = ?, product_number = ?,
                purchase_date = ?, warranty_length = ?, warranty_unit = ?, warranty_end = ?,
                updated_at = CURRENT_TIMESTAMP
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
        
        return _row_to_response(row, count_row['count'], attributes)


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

        # Drop the device's full-text index rows so search results don't keep
        # pointing at now-deleted manuals.
        cursor = await db.execute(
            "SELECT rowid FROM pdf_index WHERE device_id = ?",
            (device_id,)
        )
        for r in await cursor.fetchall():
            await db.execute("DELETE FROM pdf_index WHERE rowid = ?", (r["rowid"],))

        await db.commit()


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
