"""Manual download API endpoints."""
import os
import json
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse, FileResponse
from typing import List, AsyncGenerator
import asyncio

from homestew.config import settings
from homestew.db import get_db_context
from homestew.models.schemas import DownloadStatus, Manual, DownloadTriggerRequest
from homestew.services.indexer import index_manual

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/downloads", tags=["downloads"])


async def _register_and_index_manuals(
    device_id: int,
    device_dir: Path,
    filenames: List[str],
) -> int:
    """Insert downloaded PDFs into the manuals table and index them for search.

    Returns the number of successfully indexed manuals. The inserts are
    committed (and their connection closed) BEFORE indexing: index_manual
    opens its own connection, and SQLite rejects a second writer while this
    transaction is still open ("database is locked").
    """
    registered = []
    async with get_db_context() as db:
        for filename in filenames:
            filepath = str(device_dir / filename)

            cursor = await db.execute(
                """
                INSERT INTO manuals (device_id, filename, filepath)
                VALUES (?, ?, ?)
                RETURNING id
                """,
                (device_id, filename, filepath)
            )
            row = await cursor.fetchone()
            if row:
                registered.append((row['id'], filepath, filename))

        await db.commit()

    indexed_count = 0
    for manual_id, filepath, filename in registered:
        success = await index_manual(
            manual_id=manual_id,
            device_id=device_id,
            pdf_path=filepath,
            filename=filename
        )
        if success:
            indexed_count += 1

    return indexed_count


@router.post("/trigger", response_model=DownloadStatus)
async def trigger_manual_download(request: DownloadTriggerRequest):
    """Trigger automatic manual download for a device (legacy endpoint)."""
    async with get_db_context() as db:
        # Get device info
        cursor = await db.execute(
            "SELECT id, name, brand, model FROM devices WHERE id = ?",
            (request.device_id,)
        )
        device = await cursor.fetchone()
        
        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {request.device_id} not found"
            )
    
    # Create device directory for manuals
    device_dir = settings.DEVICES_DIR / str(request.device_id) / "manuals"
    
    # Download manuals (legacy non-streaming version)
    from homestew.services.manual_downloader import download_manuals_for_device
    downloaded_count, filenames, error_msg = download_manuals_for_device(
        brand=device['brand'],
        model=device['model'],
        device_dir=device_dir,
        max_downloads=settings.MANUAL_MAX_DOWNLOADS
    )
    
    if downloaded_count == 0:
        # Use the specific error message from the downloader, or fallback to generic
        error_detail = error_msg or "Failed to search and download manuals. Please check your internet connection or try again later."
        return DownloadStatus(
            success=False,
            message="No manuals found or download failed",
            downloaded_count=0,
            error_detail=error_detail
        )
    
    # Register the downloaded PDFs, committing before indexing (index_manual
    # writes on its own connection; see _register_and_index_manuals).
    registered = []
    async with get_db_context() as db:
        for filename in filenames:
            filepath = str(device_dir / filename)
            
            # Add to database
            cursor = await db.execute(
                """
                INSERT INTO manuals (device_id, filename, filepath)
                VALUES (?, ?, ?)
                RETURNING id
                """,
                (request.device_id, filename, filepath)
            )
            row = await cursor.fetchone()
            if row:
                registered.append((row['id'], filepath, filename))
        
        await db.commit()
    
    indexed_count = 0
    for manual_id, filepath, filename in registered:
        if await index_manual(
            manual_id=manual_id,
            device_id=request.device_id,
            pdf_path=filepath,
            filename=filename
        ):
            indexed_count += 1
    
    return DownloadStatus(
        success=True,
        message=f"Downloaded and indexed {indexed_count} manuals",
        downloaded_count=indexed_count
    )


@router.get("/stream/{device_id}")
async def stream_download_progress(device_id: int):
    """Stream download progress updates via Server-Sent Events."""
    import asyncio
    from queue import Queue, Empty
    import threading
    
    async with get_db_context() as db:
        # Get device info
        cursor = await db.execute(
            "SELECT id, name, brand, model FROM devices WHERE id = ?",
            (device_id,)
        )
        device = await cursor.fetchone()
        
        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
    
    # Create device directory for manuals
    device_dir = settings.DEVICES_DIR / str(device_id) / "manuals"
    
    # Queue to hold progress messages
    message_queue = Queue()
    download_complete = asyncio.Event()

    def _sse(payload: dict) -> str:
        """Serialize a payload as a single SSE data frame (safely escaped)."""
        return "data: " + json.dumps(payload) + "\n\n"

    def progress_callback(message):
        """Callback that forwards structured progress messages to the queue.

        ``download_manuals_for_device`` emits dicts; older callers may still
        pass plain strings, so normalize both into SSE frames.
        """
        try:
            if isinstance(message, dict):
                message_queue.put(_sse(message))
            else:
                message_queue.put(_sse({"type": "step", "message": str(message), "status": "searching"}))
        except Exception as e:
            logger.error(f"Failed to put message in queue: {e}")

    async def generate_progress():
        # Start download in a background thread
        def run_download():
            try:
                from homestew.services.manual_downloader import download_manuals_for_device

                downloaded_count, filenames, error_msg = download_manuals_for_device(
                    brand=device['brand'],
                    model=device['model'],
                    device_dir=device_dir,
                    max_downloads=settings.MANUAL_MAX_DOWNLOADS,
                    progress_callback=progress_callback
                )

                # Register and index the downloaded PDFs so they become
                # searchable. This runs in a worker thread, so use asyncio.run
                # (the thread gets its own event loop).
                if filenames:
                    try:
                        downloaded_count = asyncio.run(
                            _register_and_index_manuals(device_id, device_dir, filenames)
                        )
                    except Exception as exc:
                        logger.error(f"Failed to index downloaded manuals: {exc}")

                # Emit exactly one terminal event. The frontend renders the
                # outcome (and any error detail) from this single message so we
                # never show two red X's for the same failure.
                if len(filenames) > 0:
                    progress_callback({
                        "type": "complete",
                        "downloaded_count": downloaded_count,
                        "filenames": filenames,
                        "success": True,
                    })
                else:
                    error_detail = error_msg or "No manuals found"
                    logger.warning(f"Manual download failed for device {device_id}: {error_detail}")
                    progress_callback({
                        "type": "complete",
                        "downloaded_count": 0,
                        "error_detail": error_detail,
                        "success": False,
                    })

            except Exception as e:
                logger.exception(f"Unexpected error during manual download for device {device_id}")
                progress_callback({"type": "error", "message": str(e)})
            finally:
                download_complete.set()
        
        # Run download in background thread
        thread = threading.Thread(target=run_download)
        thread.start()
        
        try:
            while not download_complete.is_set():
                try:
                    # Get message from queue with timeout
                    message = message_queue.get(timeout=0.5)
                    yield message
                except Empty:
                    # No message yet, continue waiting
                    await asyncio.sleep(0.1)
            
            # Drain any remaining messages
            while not message_queue.empty():
                try:
                    message = message_queue.get_nowait()
                    yield message
                except Empty:
                    break
                    
        except Exception as e:
            logger.error(f"Error in streaming: {e}")
    
    return StreamingResponse(
        generate_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@router.get("/{device_id}/manuals", response_model=List[Manual])
async def list_device_manuals(device_id: int):
    """List all manuals for a specific device."""
    async with get_db_context() as db:
        # Check if device exists
        cursor = await db.execute(
            "SELECT id FROM devices WHERE id = ?", (device_id,)
        )
        if not await cursor.fetchone():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device {device_id} not found"
            )
        
        # Get manuals
        cursor = await db.execute(
            """
            SELECT id, device_id, filename, filepath, page_count, indexed_at
            FROM manuals
            WHERE device_id = ?
            ORDER BY indexed_at DESC
            """,
            (device_id,)
        )
        
        rows = await cursor.fetchall()
        
        return [
            Manual(
                id=row['id'],
                device_id=row['device_id'],
                filename=row['filename'],
                filepath=row['filepath'],
                page_count=row['page_count'],
                indexed_at=row['indexed_at']
            )
            for row in rows
        ]


@router.get("/manuals/{manual_id}/file")
async def get_manual_file(manual_id: int):
    """Serve a stored manual PDF inline (for search-result links).

    The browser's built-in PDF viewer honours the ``#page=N`` fragment, so
    search results link to ``/api/downloads/manuals/{id}/file#page=N``.
    """
    from fastapi.responses import FileResponse

    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT filename, filepath FROM manuals WHERE id = ?",
            (manual_id,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Manual {manual_id} not found"
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
            detail="Manual file not found on disk"
        )

    return FileResponse(
        str(resolved),
        media_type="application/pdf",
        content_disposition_type="inline",
    )
