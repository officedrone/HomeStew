"""Search API endpoints."""
from fastapi import APIRouter, HTTPException, status
from typing import List

from homestew.models.schemas import SearchRequest, SearchResponse, SearchResult
from homestew.services.search_engine import search_manuals, count_indexed_documents
from homestew.services.indexer import reindex_all_manuals

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(request: SearchRequest):
    """Search across all indexed PDF manuals."""
    if not request.query.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query cannot be empty"
        )
    
    results = await search_manuals(
        query=request.query,
        device_id=request.device_id,
        limit=request.limit
    )
    
    return SearchResponse(
        query=request.query,
        total_results=len(results),
        results=results
    )


@router.get("/stats")
async def get_search_stats():
    """Get search engine statistics."""
    count = await count_indexed_documents()
    
    return {
        "indexed_documents": count,
        "status": "ready" if count > 0 else "no_indexed_content"
    }


@router.post("/reindex")
async def reindex_manuals():
    """Re-index every stored manual.

    Useful when a manual failed to index earlier (e.g. an extractor bug that
    has since been fixed) and its content is missing from search results.
    """
    indexed = await reindex_all_manuals()
    count = await count_indexed_documents()

    return {
        "manuals_indexed": indexed,
        "indexed_documents": count,
    }
