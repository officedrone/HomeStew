"""Manual download API endpoints (user-approved flow).

Successor to ``homestew/api/downloads.py``. The old auto-pilot flow
(SSE stream that searched and downloaded everything unattended) is replaced
by two steps the UI drives:

1. ``GET  /downloads/{device_id}/search`` - find candidates, flag the ones
   already stored for this device (matched on their recorded source URL).
2. ``POST /downloads/store`` - fetch ONE candidate the user approved. A URL
   that is already stored answers 409 so the UI can ask "replace?" and retry
   with ``replace=true``; nothing is ever overwritten silently.

The manual-listing and inline file-serving endpoints keep their exact old
paths (``/{device_id}/manuals``, ``/manuals/{manual_id}/file``) because
search-result links, chat citations and the edit modal all point at them.
"""
import logging
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from homestew.config import settings
from homestew.db import get_db_context
from homestew.models.schemas import (
    Manual,
    ManualCandidate,
    ManualSearchResponse,
    StoreManualRequest,
    StoreManualResponse,
)
from homestew.services.downloader import (
    ManualDownloadError,
    ManualExistsError,
    find_manual_candidates,
    store_manual_for_device,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/downloads", tags=["downloads"])


@router.get("/{device_id}/search", response_model=ManualSearchResponse)
async def search_manuals(device_id: int):
    """Find manual PDF candidates for a device without downloading anything.

    Each candidate carries the display name, source domain and an
    ``already_downloaded`` flag so the UI can highlight what is stored and
    only offer fresh downloads for the rest.
    """
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT id, brand, model FROM devices WHERE id = ?", (device_id,)
        )
        device = await cursor.fetchone()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {device_id} not found",
        )

    candidates, error = await find_manual_candidates(device_id, device["brand"], device["model"])
    return ManualSearchResponse(
        candidates=[ManualCandidate(**c) for c in candidates],
        error_detail=error,
    )


@router.post("/store", response_model=StoreManualResponse)
async def store_manual(request: StoreManualRequest):
    """Download one user-approved candidate and register + index it.

    409 when the URL is already stored for this device: the caller confirms
    with the user and re-sends with ``replace=true`` to refresh in place.
    """
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT id, brand, model FROM devices WHERE id = ?", (request.device_id,)
        )
        device = await cursor.fetchone()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {request.device_id} not found",
        )

    try:
        result = await store_manual_for_device(
            device_id=device["id"],
            brand=device["brand"],
            model=device["model"],
            url=request.url,
            title=request.title,
            replace=request.replace,
        )
    except ManualExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "already_downloaded",
                "message": str(exc),
                "filename": exc.filename,
            },
        )
    except ManualDownloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not download the manual: {exc}",
        )

    return StoreManualResponse(**result)


@router.get("/{device_id}/manuals", response_model=List[Manual])
async def list_device_manuals(device_id: int):
    """List all manuals for a specific device."""
    async with get_db_context() as db:
        # Check if device exists
        cursor = await db.execute("SELECT id FROM devices WHERE id = ?", (device_id,))
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found",
            )

        # Get manuals
        cursor = await db.execute(
            """
            SELECT id, device_id, filename, filepath, page_count, indexed_at
            FROM manuals
            WHERE device_id = ?
            ORDER BY indexed_at DESC
            """,
            (device_id,),
        )
        rows = await cursor.fetchall()

    return [Manual(**dict(row)) for row in rows]


@router.get("/manuals/{manual_id}/file")
async def get_manual_file(manual_id: int):
    """Serve a stored manual PDF inline (for search-result links).

    The browser's built-in PDF viewer honours the ``#page=N`` fragment, so
    search results link to ``/api/downloads/manuals/{id}/file#page=N``.
    """
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT filename, filepath FROM manuals WHERE id = ?", (manual_id,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manual {manual_id} not found",
        )

    path = Path(row["filepath"])
    # Only serve files that live inside the managed devices directory.
    try:
        resolved = path.resolve()
        allowed_root = settings.DEVICES_DIR.resolve()
        if not str(resolved).startswith(str(allowed_root)) or not resolved.is_file():
            raise ValueError("outside devices dir")
    except (OSError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Manual file not found on disk",
        )

    return FileResponse(
        str(resolved),
        media_type="application/pdf",
        content_disposition_type="inline",
    )
