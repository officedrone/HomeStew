"""Search API endpoints."""
from fastapi import APIRouter, HTTPException, status
from typing import List

from homestew.models.schemas import (
    IndexStatusResponse,
    ReindexQueuedResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
)
from homestew.services.search_engine import search_manuals, count_indexed_documents
from homestew.services.indexer import (
    count_manuals as indexer_count_manuals,
    get_index_status,
    list_manual_items,
    start_background_reindex,
)

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
    """Get search engine statistics (indexed page rows + registered manuals)."""
    count = await count_indexed_documents()
    manuals = await indexer_count_manuals()

    return {
        "indexed_documents": count,
        "manuals": manuals,
    }


@router.post("/reindex", response_model=ReindexQueuedResponse, status_code=status.HTTP_202_ACCEPTED)
async def reindex_manuals():
    """Queue a background re-index of every stored manual.

    Useful when a manual failed to index earlier (e.g. an extractor bug that
    has since been fixed) and its content is missing from search results.
    Returns immediately; poll GET /search/index-status for progress.
    """
    items = await list_manual_items()
    queued = start_background_reindex(items)
    if queued < 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An indexing job is already running",
        )
    return ReindexQueuedResponse(queued=queued, rebuild=False)


@router.post("/rebuild", response_model=ReindexQueuedResponse, status_code=status.HTTP_202_ACCEPTED)
async def rebuild_index():
    """Drop the search index entirely and re-index every manual from scratch.

    Unlike /reindex (which upserts per manual), this also purges stale rows
    left behind by deleted manuals. Returns immediately; progress is polled
    from GET /search/index-status like any other indexing job.
    """
    items = await list_manual_items()
    queued = start_background_reindex(items, rebuild=True)
    if queued < 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An indexing job is already running",
        )
    return ReindexQueuedResponse(queued=queued, rebuild=True)


@router.get("/index-status", response_model=IndexStatusResponse)
async def index_status():
    """Progress of the current (or most recent) background indexing job."""
    return IndexStatusResponse(**get_index_status())
