"""SQLite FTS5 indexer for PDF content."""
import logging
from pathlib import Path
from typing import Optional

from homebrain.db import get_db_context
from homebrain.services.pdf_extractor import extract_text_from_pdf, split_text_into_chunks

logger = logging.getLogger(__name__)


async def index_manual(
    manual_id: int,
    device_id: int,
    pdf_path: str,
    filename: str
) -> bool:
    """
    Index a PDF manual into the FTS5 search engine.
    
    Args:
        manual_id: ID of the manual in database
        device_id: Device ID this manual belongs to
        pdf_path: Path to the PDF file
        filename: Original filename
        
    Returns:
        True if indexing successful, False otherwise
    """
    try:
        # Extract text from PDF
        full_text, page_count = extract_text_from_pdf(pdf_path)
        
        if not full_text.strip():
            logger.warning(f"No text extracted from {pdf_path}")
            return False
        
        # Split into chunks (one per page or paragraph groups)
        pages = full_text.split('\n\n')
        
        async with get_db_context() as db:
            # Insert each page/chunk into FTS5 index
            for page_num, page_text in enumerate(pages, start=1):
                if not page_text.strip():
                    continue
                
                # Truncate very long pages
                if len(page_text) > 5000:
                    page_text = page_text[:5000] + "..."
                
                await db.execute(
                    """
                    INSERT INTO pdf_index (device_id, filename, page_number, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (device_id, filename, page_num, page_text)
                )
            
            await db.commit()
        
        logger.info(f"Indexed {len(pages)} pages from {filename}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to index {pdf_path}: {e}")
        return False


async def reindex_all_manuals():
    """Re-index all manuals in the database."""
    async with get_db_context() as db:
        # Get all manuals
        cursor = await db.execute(
            "SELECT id, device_id, filepath, filename FROM manuals"
        )
        manuals = await cursor.fetchall()
    
    success_count = 0
    for manual in manuals:
        if await index_manual(
            manual['id'],
            manual['device_id'],
            manual['filepath'],
            manual['filename']
        ):
            success_count += 1
    
    logger.info(f"Re-indexed {success_count}/{len(manuals)} manuals")
    return success_count


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
