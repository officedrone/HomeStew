"""Full-text search engine using SQLite FTS5."""
import logging
from typing import Optional, List

from homebrain.db import get_db_context
from homebrain.models.schemas import SearchResult

logger = logging.getLogger(__name__)


async def search_manuals(
    query: str,
    device_id: Optional[int] = None,
    limit: int = 10
) -> List[SearchResult]:
    """
    Search across indexed PDF content.
    
    Args:
        query: Search query (supports FTS5 syntax)
        device_id: Optional filter by device ID
        limit: Maximum results to return
        
    Returns:
        List of SearchResult objects with snippets
    """
    try:
        async with get_db_context() as db:
            # Build search query with optional device filter
            if device_id:
                sql = """
                    SELECT 
                        rowid,
                        device_id,
                        filename,
                        page_number,
                        highlight(pdf_index, 3, '<mark>', '</mark>') as snippet,
                        bm25(pdf_index) as score
                    FROM pdf_index
                    WHERE pdf_index MATCH ?
                      AND device_id = ?
                    ORDER BY bm25(pdf_index)
                    LIMIT ?
                """
                params = (query, device_id, limit)
            else:
                sql = """
                    SELECT 
                        rowid,
                        device_id,
                        filename,
                        page_number,
                        highlight(pdf_index, 3, '<mark>', '</mark>') as snippet,
                        bm25(pdf_index) as score
                    FROM pdf_index
                    WHERE pdf_index MATCH ?
                    ORDER BY bm25(pdf_index)
                    LIMIT ?
                """
                params = (query, limit)
            
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
            
            results = []
            for row in rows:
                # Clean up snippet HTML
                snippet = row['snippet'] or ''
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."
                
                results.append(SearchResult(
                    manual_id=row['rowid'],
                    device_id=row['device_id'],
                    filename=row['filename'],
                    page_number=row['page_number'],
                    snippet=snippet,
                    score=float(row['score'])
                ))
            
            logger.info(f"Search found {len(results)} results for: {query}")
            return results
            
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return []


async def count_indexed_documents() -> int:
    """Count total indexed documents."""
    async with get_db_context() as db:
        cursor = await db.execute("SELECT COUNT(*) as count FROM pdf_index")
        row = await cursor.fetchone()
        return row['count'] if row else 0


async def search_count(query: str, device_id: Optional[int] = None) -> int:
    """Get total count of matches for a query."""
    try:
        async with get_db_context() as db:
            if device_id:
                sql = """
                    SELECT COUNT(*) as count FROM pdf_index
                    WHERE pdf_index MATCH ? AND device_id = ?
                """
                cursor = await db.execute(sql, (query, device_id))
            else:
                sql = """
                    SELECT COUNT(*) as count FROM pdf_index
                    WHERE pdf_index MATCH ?
                """
                cursor = await db.execute(sql, (query,))
            
            row = await cursor.fetchone()
            return row['count'] if row else 0
            
    except Exception as e:
        logger.error(f"Search count failed: {e}")
        return 0
