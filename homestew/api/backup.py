"""Backup / restore API endpoints (Settings > General).

Two-step download keeps the UI honest about what was captured without buffering
a large archive in the browser: ``POST /create`` builds the file on disk and
returns its stats + a one-shot URL, then ``GET /file/{name}`` streams it as an
attachment and deletes it once sent. Restore accepts an uploaded archive plus a
mode (merge/replace) and re-indexes restored manuals in the background.
"""
import logging
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from homestew.config import settings
from homestew.models.schemas import (
    BackupCreateResponse,
    RestoreIndexStatus,
    RestoreProgressResponse,
    RestoreResponse,
)
from homestew.services import backup as backup_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backup", tags=["backup"])

# Abandoned downloads (browser never fetched the file) would otherwise pile up;
# any archive older than this is swept on the next create call.
_STALE_AFTER_SECONDS = 30 * 60


def _safe_backup_path(filename: str) -> Path:
    """Resolve a backup filename strictly inside the backups dir (no traversal)."""
    root = backup_service.backup_dir().resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root or not candidate.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup file not found",
        )
    return candidate


def _sweep_stale(out_dir: Path) -> None:
    """Delete leftover archives from abandoned downloads (best effort)."""
    cutoff = time.time() - _STALE_AFTER_SECONDS
    try:
        for path in out_dir.glob("homestew-backup-*.zip"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except OSError as exc:  # never let cleanup break a create
        logger.warning("Backup sweep failed: %s", exc)


@router.post("/create", response_model=BackupCreateResponse)
async def create_backup(include_manuals: bool = Form(True)):
    """Build a backup archive and return its stats + download URL.

    ``include_manuals`` False produces a lightweight metadata-only archive
    (devices, attributes, calendar events) without any manual rows or PDFs.
    """
    out_dir = backup_service.backup_dir()
    await run_in_threadpool(_sweep_stale, out_dir)

    try:
        result = await backup_service.create_backup(include_manuals=include_manuals)
    except OSError as exc:
        logger.error("Backup creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create the backup file.",
        )

    return BackupCreateResponse(
        filename=result["filename"],
        url=f"/api/backup/file/{result['filename']}",
        devices=result["devices"],
        manuals=result["manuals"],
        calendar_events=result["calendar_events"],
        missing_files=result["missing_files"],
    )


@router.get("/file/{filename}")
async def download_backup(filename: str):
    """Stream a freshly created backup as an attachment, then delete it."""
    path = _safe_backup_path(filename)
    return FileResponse(
        str(path),
        media_type="application/zip",
        filename=filename,  # sets Content-Disposition: attachment
        content_disposition_type="attachment",
    )


@router.post("/restore", response_model=RestoreResponse)
async def restore_backup(
    file: UploadFile = File(...),
    mode: str = Form("merge"),
):
    """Restore devices, manuals and calendar events from an uploaded archive.

    ``mode`` is 'merge' (add what's missing, idempotent) or 'replace' (wipe the
    four tables first). Search re-indexing of restored PDFs runs in the
    background; poll /restore-progress for live phase/percent/elapsed while any
    of that is happening.
    """
    # get_restore_progress() folds the index-phase transition in, so a finished
    # earlier restore can't leave a stale flag blocking this one.
    if backup_service.get_restore_progress()["running"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A restore is already running.",
        )

    if mode not in ("merge", "replace"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be 'merge' or 'replace'",
        )

    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please choose a backup .zip file.",
        )

    out_dir = backup_service.backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = out_dir / f".restore-{int(time.time() * 1000)}.zip"

    # Claim the progress slot before the upload so the status bar covers even
    # the transfer itself.
    backup_service.begin_restore(mode)

    # Stream the upload to disk (never fully buffered), then restore.
    try:
        total_bytes = file.size or 0
        backup_service.set_restore_phase("upload", 0, total_bytes)

        def _save():
            written = 0
            with open(tmp_path, "wb") as out:
                while chunk := file.file.read(1024 * 1024):
                    out.write(chunk)
                    written += len(chunk)
                    backup_service.bump_restore(len(chunk))

        await run_in_threadpool(_save)
        if tmp_path.stat().st_size == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The uploaded file is empty.",
            )

        result = await backup_service.restore_backup(tmp_path, mode)
    except ValueError as exc:  # malformed / unsafe archive
        # restore_backup already recorded the error; re-map to a 400.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except BaseException as exc:  # upload failure / client disconnect
        backup_service.finish_restore(error=str(exc) or "Restore failed.")
        raise
    finally:
        tmp_path.unlink(missing_ok=True)

    # Kick off background re-indexing of restored manuals (fire-and-forget).
    await backup_service.start_reindex(result.get("_pending_index", []))

    return RestoreResponse(
        mode=result["mode"],
        devices_created=result["devices_created"],
        devices_merged=result["devices_merged"],
        manuals_restored=result["manuals_restored"],
        events_restored=result["events_restored"],
        skipped=result["skipped"],
        index_total=result["index_total"],
    )


@router.get("/restore-status", response_model=RestoreIndexStatus)
async def restore_status():
    """Progress of the background search re-index that follows a restore."""
    state = backup_service.get_restore_index_status()
    return RestoreIndexStatus(
        running=state["running"], done=state["done"], total=state["total"]
    )


@router.get("/restore-progress", response_model=RestoreProgressResponse)
async def restore_progress():
    """Live progress of the current/last restore for the status bar.

    Reports the phase, per-phase counters, weighted percentage, steps
    remaining and elapsed seconds. The index phase's counters are folded in
    from the shared indexer state, so one poll covers the whole pipeline.
    """
    return RestoreProgressResponse(**backup_service.get_restore_progress())
