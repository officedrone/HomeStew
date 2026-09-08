"""Search API endpoints."""
from fastapi import APIRouter, HTTPException, status
from typing import List

from homebrain.models.schemas import SearchRequest, SearchResponse, SearchResult
from homebrain.services.search_engine import search_manuals, count_indexed_documents

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
