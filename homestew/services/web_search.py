"""Engine-agnostic web search layer (the ONLY module that imports ddgs).

Kept deliberately pure - no database, no filesystem, no progress callbacks -
so it can back both the manual downloader and any future LLM tool that needs
to reach the open web. Callers get normalized :class:`WebResult` rows or a
:class:`SearchUnavailableError` carrying a human-readable explanation; raw
library exceptions never leak upward.

ddgs specifics this layer hides (verified against ddgs 9.x):
- ``DDGS(proxy, timeout).text(query, region=..., safesearch=..., backend=...)``
  returns dicts with ``title`` / ``href`` keys (``url`` on some engines);
- an empty result set is signalled by raising ``DDGSException("No results
  found.")``, not by returning [] - we normalize that to an empty list;
- unknown engine names in ``backend`` are skipped by ddgs with a warning, so
  a configured-but-invalid engine degrades instead of failing.

Error mapping matters for ops: the Sep 2026 outage (primp 2.x dropping the
browser profiles an older search library hardcoded) surfaced as
``Invalid impersonate: "edge_131"`` on every query in ~0 ms. That is a broken
image, not a network problem, and users must be told so explicitly.
"""
import logging
from typing import List, NamedTuple, Optional

logger = logging.getLogger(__name__)


class WebResult(NamedTuple):
    """One normalized web search hit."""

    title: str
    url: str
    engine: str  # originating engine/provider when the library reports one


class SearchUnavailableError(Exception):
    """Search cannot run or failed; ``message`` is safe to show in the UI."""


def _import_ddgs():
    """Import ddgs lazily with an actionable error if it is missing/broken.

    A broken install must not crash app import - only manual fetching fails,
    with a message that says what to do about it.
    """
    try:
        from ddgs import DDGS  # noqa: PLC0415
        from ddgs.exceptions import (  # noqa: PLC0415
            DDGSException,
            RatelimitException,
            TimeoutException,
        )

        return DDGS, DDGSException, RatelimitException, TimeoutException
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise SearchUnavailableError(
            "The search library (ddgs) is not installed in this container. "
            "Rebuild the image: docker-compose build --no-cache && docker-compose up -d"
        ) from exc


def friendly_search_error(exc: Exception) -> str:
    """Turn a raw search-library exception into a human-readable message."""
    text = str(exc)
    lowered = text.lower()

    # The dependency-drift signature (see module docstring). Distinct from a
    # network problem so users rebuild instead of waiting for rate limits.
    if "invalid impersonate" in lowered:
        return (
            "The search library is incompatible with the installed HTTP client "
            f"(browser profile error: {text}). This is not a network problem - "
            "rebuild the container image: docker-compose build --no-cache && docker-compose up -d"
        )
    if "no results found" in lowered:
        # ddgs raises instead of returning [] when engines agree on nothing.
        return ""  # caller treats empty results as "nothing found", not an error
    try:
        from ddgs.exceptions import RatelimitException

        if isinstance(exc, RatelimitException):
            return "The search engines are rate-limiting requests. Wait a minute or two and try again."
    except ImportError:  # library absent - fall through to text matching
        pass
    if "ratelimit" in lowered or ("202" in text and "limit" in lowered):
        return (
            "DuckDuckGo is rate-limiting requests (HTTP 202). "
            "Wait a minute or two and try again."
        )
    if "timeout" in lowered or "timed out" in lowered:
        return "Search request timed out. Check your internet connection and try again."
    if "connection" in lowered or "network" in lowered or "resolve" in lowered:
        return "Network error while searching. Check the container's internet connection and try again."
    return f"Search backend error: {text}"


def search_web(
    query: str,
    *,
    max_results: Optional[int] = None,
    backends: Optional[str] = None,
    region: Optional[str] = None,
    safesearch: str = "moderate",
) -> List[WebResult]:
    """Search the web via ddgs and return normalized results.

    Args:
        query: The search query.
        max_results: Cap on returned results (library default when None).
        backends: Comma-separated engine names ("bing,brave,...") or None/""
            for "auto" - ddgs then rotates over all engines, which is the
            resilient default.
        region: ddgs region token ("us-en", "de-de", ...); library default
            when None/"".
        safesearch: "on"/"moderate"/"off".

    Returns:
        List of :class:`WebResult` (possibly empty - empty means "nothing
        found", which is NOT an error).

    Raises:
        SearchUnavailableError: The library is missing, or every engine
            failed. The message explains what to do.
    """
    from homestew.config import settings  # noqa: PLC0415 (avoid import cycle at module load)

    DDGS, DDGSException, _Ratelimit, _Timeout = _import_ddgs()

    backend = (backends if backends is not None else settings.MANUAL_SEARCH_BACKENDS).strip() or "auto"
    resolved_region = (region if region is not None else settings.MANUAL_SEARCH_REGION).strip()
    timeout = max(1, int(settings.MANUAL_SEARCH_TIMEOUT))

    kwargs = {"backend": backend, "safesearch": safesearch}
    if resolved_region:
        kwargs["region"] = resolved_region
    if max_results is not None:
        kwargs["max_results"] = max_results

    logger.info("Web search via ddgs (backends=%s): %s", backend, query)
    try:
        with DDGS(proxy=settings.MANUAL_PROXY or None, timeout=timeout) as ddgs:
            raw = ddgs.text(query, **kwargs)
    except DDGSException as exc:
        message = friendly_search_error(exc)
        if not message:  # "No results found." -> empty list, not a failure
            logger.info("Web search returned no results for: %s", query)
            return []
        logger.warning("ddgs search failed: %s", exc)
        raise SearchUnavailableError(message) from exc
    except Exception as exc:  # unexpected library/HTTP-client error
        logger.exception("Unexpected web search failure")
        raise SearchUnavailableError(friendly_search_error(exc)) from exc

    results: List[WebResult] = []
    for item in raw or []:
        url = (item.get("href") or item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        results.append(
            WebResult(
                title=(item.get("title") or "").strip(),
                url=url,
                engine=str(item.get("engine") or item.get("source") or ""),
            )
        )
    logger.info("Web search returned %d normalized result(s)", len(results))
    return results
