"""SQLite FTS5 indexer for PDF content."""
import asyncio
import logging
from pathlib import Path
from typing import List, Optional

from homestew.config import settings
from homestew.db import get_db_context
from homestew.services.pdf_extractor import (
    extract_text_from_pdf,
    extract_pages_from_pdf,
    split_text_into_chunks,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared background-index progress state
# ---------------------------------------------------------------------------
#
# Every long-running indexing job (manual re-index from Settings > Search,
# full rebuild, and the re-index that follows a restore) reports progress
# through this single module-global so any part of the UI can poll one
# endpoint. Single-process uvicorn, so a plain dict is enough.
_INDEX_STATE = {
    "running": False,
    "done": 0,
    "total": 0,
    "failed": 0,
    "current_file": "",
}


def get_index_status() -> dict:
    """Snapshot of the current/last background indexing job."""
    return dict(_INDEX_STATE)


async def index_manual(
    manual_id: int,
    device_id: int,
    pdf_path: str,
    filename: str
) -> bool:
    """
    Index a PDF manual into the FTS5 search engine.

    Each real PDF page becomes one indexed row so that search results can
    link directly to the correct page of the source PDF.

    Args:
        manual_id: ID of the manual in database
        device_id: Device ID this manual belongs to
        pdf_path: Path to the PDF file
        filename: Original filename
        
    Returns:
        True if indexing successful, False otherwise
    """
    try:
        # Extract text page-by-page so page numbers match the actual PDF.
        pages = extract_pages_from_pdf(pdf_path)

        if not any(p.strip() for p in pages):
            logger.warning(f"No text extracted from {pdf_path}")
            return False
        
        async with get_db_context() as db:
            # Drop any previous rows for this manual so re-indexing is safe.
            cursor = await db.execute(
                "SELECT rowid FROM pdf_index WHERE manual_id = ?",
                (manual_id,)
            )
            existing = await cursor.fetchall()
            for row in existing:
                await db.execute("DELETE FROM pdf_index WHERE rowid = ?", (row['rowid'],))

            # Insert one row per PDF page.
            indexed_pages = 0
            for page_num, page_text in enumerate(pages, start=1):
                if not page_text.strip():
                    continue

                # Very long pages are split into chunks that keep the same
                # (correct) page number so snippets stay linkable to the page.
                # Chunk size is a Settings > Search option, read live so a
                # save applies to the next indexing run without a restart.
                for chunk in split_text_into_chunks(
                    page_text,
                    max_chunk_size=max(200, int(settings.INDEX_MAX_CHUNK_SIZE)),
                ):
                    await db.execute(
                        """
                        INSERT INTO pdf_index
                            (device_id, manual_id, filename, page_number, content)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (device_id, manual_id, filename, page_num, chunk)
                    )
                    indexed_pages += 1
            
            await db.commit()
        
        logger.info(f"Indexed {indexed_pages} chunks from {filename}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to index {pdf_path}: {e}")
        return False


async def count_manuals() -> int:
    """Number of manuals registered in the database (indexed or not)."""
    async with get_db_context() as db:
        cursor = await db.execute("SELECT COUNT(*) AS count FROM manuals")
        row = await cursor.fetchone()
    return row["count"] if row else 0


async def list_manual_items() -> List[dict]:
    """All stored manuals as index_manual kwargs (used to queue jobs)."""
    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT id, device_id, filepath, filename FROM manuals"
        )
        rows = await cursor.fetchall()
    return [
        {
            "manual_id": row["id"],
            "device_id": row["device_id"],
            "pdf_path": row["filepath"],
            "filename": row["filename"],
        }
        for row in rows
    ]


async def reindex_all_manuals():
    """Re-index all manuals in the database (blocking; startup path only).

    UI-triggered jobs go through start_background_reindex() instead so the
    request returns immediately and progress is polled from _INDEX_STATE.
    """
    items = await list_manual_items()

    success_count = 0
    for item in items:
        if await index_manual(**item):
            success_count += 1

    logger.info(f"Re-indexed {success_count}/{len(items)} manuals")
    return success_count


async def _reset_fts_table() -> None:
    """Drop and recreate the empty pdf_index FTS table (rebuild from scratch).

    Mirrors the DDL in db.init_db(); used by the Settings > Search "Rebuild"
    action when the index itself is stale or corrupt rather than just missing
    rows for some manuals.
    """
    async with get_db_context() as db:
        await db.execute("DROP TABLE IF EXISTS pdf_index")
        await db.execute(
            """
            CREATE VIRTUAL TABLE pdf_index USING fts5(
                device_id UNINDEXED,
                manual_id UNINDEXED,
                filename UNINDEXED,
                page_number UNINDEXED,
                content,
                tokenize = 'trigram'
            )
            """
        )
        await db.commit()


def start_background_reindex(
    items: Optional[List[dict]] = None, rebuild: bool = False
) -> int:
    """Queue a background indexing job over ``items`` (all manuals by default).

    Fire-and-forget: the caller gets back the queued manual count right away
    and polls get_index_status() for progress. When ``rebuild`` is True the
    pdf_index table is dropped and recreated first, so stale rows from
    deleted manuals are purged too.

    Returns the number of queued manuals, or -1 if a job is already running
    (checked and claimed without an await in between, so two concurrent
    requests cannot both start).
    """
    if _INDEX_STATE["running"]:
        return -1
    # Claim the slot synchronously; the task fills in total once it knows.
    _INDEX_STATE.update(
        {"running": True, "done": 0, "total": len(items) if items is not None else 0,
         "failed": 0, "current_file": ""}
    )
    asyncio.create_task(_run_index_job(items, rebuild))
    return len(items) if items is not None else 0


async def _run_index_job(items: Optional[List[dict]], rebuild: bool) -> None:
    """Body of a background indexing job; always clears the running flag."""
    try:
        if rebuild:
            await _reset_fts_table()
        if items is None:
            # Enumerate after the (optional) table reset so a rebuild picks
            # up exactly the manuals still registered.
            items = await list_manual_items()
            _INDEX_STATE["total"] = len(items)

        done = 0
        failed = 0
        for item in items:
            _INDEX_STATE["current_file"] = item.get("filename", "")
            try:
                ok = await index_manual(**item)
            except Exception as exc:  # one bad PDF must not abort the rest
                logger.error(f"Background index failed for {item.get('pdf_path')}: {exc}")
                ok = False
            if not ok:
                failed += 1
            done += 1
            _INDEX_STATE["done"] = done
            _INDEX_STATE["failed"] = failed

        logger.info(f"Background index finished: {done - failed}/{done} ok (rebuild={rebuild})")
    finally:
        _INDEX_STATE["running"] = False
        _INDEX_STATE["current_file"] = ""


async def clear_device_index(device_id: int):
    """Clear FTS5 index entries for a specific device."""
    # FTS5 doesn't support DELETE with WHERE on the virtual table directly
    # We need to recreate the table or use a workaround
    
    async with get_db_context() as db:
        # Get all rowids for this device
        cursor = await db.execute(
            "SELECT rowid FROM pdf_index WHERE device_id = ?",
            (device_id,)
        )
        rows = await cursor.fetchall()
        
        for row in rows:
            await db.execute("DELETE FROM pdf_index WHERE rowid = ?", (row['rowid'],))
        
        await db.commit()
    
    logger.info(f"Cleared index for device {device_id}")
