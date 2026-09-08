"""Manual download API endpoints."""
import os
import json
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from typing import List, AsyncGenerator
import asyncio

from homebrain.config import settings
from homebrain.db import get_db_context
from homebrain.models.schemas import DownloadStatus, Manual, DownloadTriggerRequest
from homebrain.services.manual_downloader import download_manuals_for_device_with_progress
from homebrain.services.indexer import index_manual

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/downloads", tags=["downloads"])


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
    from homebrain.services.manual_downloader import download_manuals_for_device
    downloaded_count, filenames, error_msg = download_manuals_for_device(
        brand=device['brand'],
        model=device['model'],
        device_dir=device_dir,
        max_downloads=5
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
    
    # Index downloaded PDFs
    indexed_count = 0
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
                (device_id, filename, filepath)
            )
            row = await cursor.fetchone()
            manual_id = row['id'] if row else None
            
            if manual_id:
                # Index the PDF
                success = await index_manual(
                    manual_id=manual_id,
                    device_id=device_id,
                    pdf_path=filepath,
                    filename=filename
                )
                
                if success:
                    indexed_count += 1
        
        await db.commit()
    
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
    
    def progress_callback(message):
        """Callback that puts messages in the queue."""
        try:
            message_queue.put(message)
        except Exception as e:
            logger.error(f"Failed to put message in queue: {e}")
    
    async def generate_progress():
        # Start download in a background thread
        def run_download():
            try:
                from homebrain.services.manual_downloader import download_manuals_for_device
                
                downloaded_count, filenames, error_msg = download_manuals_for_device(
                    brand=device['brand'],
                    model=device['model'],
                    device_dir=device_dir,
                    max_downloads=5,
                    progress_callback=progress_callback
                )
                
                # Send completion message
                if len(filenames) > 0:
                    msg = f"Successfully downloaded {len(filenames)} manual(s)"
                    progress_callback('data: {"type": "step", "message": "' + msg + '", "status": "success"}\n\n')
                    progress_callback('data: {"type": "complete", "downloaded_count": ' + str(downloaded_count) + ', "filenames": ' + json.dumps(filenames) + ', "success": true}\n\n')
                else:
                    error_detail = error_msg or "No manuals found"
                    progress_callback('data: {"type": "step", "message": "Download failed: ' + error_detail + '", "status": "error"}\n\n')
                    progress_callback('data: {"type": "complete", "downloaded_count": 0, "error_detail": "' + error_detail + '", "success": false}\n\n')
                    
            except Exception as e:
                error_msg = str(e)
                progress_callback('data: {"type": "step", "message": "Error: ' + error_msg + '", "status": "error"}\n\n')
                progress_callback('data: {"type": "error", "message": "' + error_msg + '"}\n\n')
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
