"""Engine-agnostic web search layer (the ONLY module that imports ddgs).

Kept deliberately pure - no database, no filesystem, no progress callbacks -
so it can back both the manual downloader and any future LLM tool that needs
to reach the open web. Callers get normalized :class:`WebResult` rows or a
:class:`SearchUnavailableError` carrying a human-readable explanation; raw
library exceptions never leak upward.

Engine model (Settings > Search "Search Online for Manuals"): searches walk an
ordered list of engines and stop at the first one that returns results, so a
blocked engine only costs its own timeout instead of failing the whole search.
Two engine kinds exist:

- ``"bing"`` - HomeStew's own scraper (primp + lxml). Verified to answer 200
  with real results from IPs where every ddgs engine fails: Brave answers 429
  (captcha), Mojeek/Google 403, DuckDuckGo's HTML endpoint 202, Startpage 200
  with a JavaScript anti-bot challenge, Yahoo works ~40% of the time. Bing's
  outbound links are ``/ck/a`` redirects; :func:`manual_finder.unwrap_redirect`
  decodes them into the real target URLs.
- any other name (``"yahoo"``, ``"brave"``, ``"duckduckgo"``, ``"startpage"``,
  ``"mojeek"``, ...) - passed through to ddgs as a single-engine ``backend``.
  ddgs' own bing engine is disabled/stale, hence the custom scraper above.

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
import re
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, List, NamedTuple, Optional

logger = logging.getLogger(__name__)

# ddgs hides engine HTTP statuses (any non-200 becomes "no results"), but the
# primp HTTP client it uses logs every response as "response: <url> <status>".
# Capturing those lines while one engine runs is the only way to tell a
# throttled/blocked engine apart from one that genuinely found nothing.
_PRIMP_RESPONSE_RE = re.compile(r"response:\s*(\S+)\s+(\d{3})\b")


@contextmanager
def capture_http_events(sink: List[tuple]) -> Iterator[List[tuple]]:
    """Collect ``(url, status)`` pairs primp logs inside the block.

    The URL is what the user can open in their own browser to verify the
    engine's behavior by hand; the status distinguishes throttling/blocking
    from a genuine empty answer.
    """
    handler_src = logging.getLogger("primp")
    previous_level = handler_src.level

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                match = _PRIMP_RESPONSE_RE.search(record.getMessage())
            except Exception:  # noqa: BLE001 - logging must never raise
                return
            if match:
                sink.append((match.group(1), int(match.group(2))))

    collector = _Collector(level=logging.INFO)
    handler_src.addHandler(collector)
    if handler_src.level > logging.INFO or handler_src.level == logging.NOTSET:
        handler_src.setLevel(logging.INFO)
    try:
        yield sink
    finally:
        handler_src.removeHandler(collector)
        handler_src.setLevel(previous_level)


def _search_url_score(url: str) -> int:
    """How much a captured URL looks like the engine's results page.

    ddgs also fires auxiliary requests (typeahead, opensearch) that are
    useless for manual testing; the actual ``/search?q=...`` page wins.
    """
    lowered = url.lower()
    score = 0
    if "/search" in lowered:
        score += 4
    if "?q=" in lowered or "&q=" in lowered:
        score += 2
    return score


def worst_http_event(events: Iterable[tuple]) -> tuple:
    """The ``(status, url)`` worth reporting from ones seen during a run.

    Prefers failure codes (4xx/5xx, plus ddgs-hostile 202 challenges);
    otherwise the last event seen; ``(0, "")`` when nothing was captured.
    The URL is the most results-page-like one among the matching events so
    the user can click it and reproduce the engine's answer.
    """
    pairs = [(str(u), int(s)) for u, s in events]
    candidates = [(u, s) for u, s in pairs if s >= 400 or s == 202] or pairs
    if not candidates:
        return 0, ""
    if any(s >= 400 or s == 202 for _, s in candidates):
        worst = max(s for _, s in candidates)
        candidates = [(u, s) for u, s in candidates if s == worst]
        status = worst
    else:
        status = candidates[-1][1]
    best_index = max(
        range(len(candidates)),
        key=lambda i: (_search_url_score(candidates[i][0]), i),
    )
    return status, candidates[best_index][0]

# Engine name handled by the built-in scraper instead of ddgs.
BING_ENGINE = "bing"


class WebResult(NamedTuple):
    """One normalized web search hit."""

    title: str
    url: str
    engine: str  # originating engine/provider when the library reports one


class SearchUnavailableError(Exception):
    """Search cannot run or failed; ``message`` is safe to show in the UI.

    ``http_status`` carries the engine's HTTP status when known (0 otherwise),
    ``detail`` keeps the raw library/HTTP text for troubleshooting, and ``url``
    is the actual search URL that was queried (so users can retry by hand).
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int = 0,
        detail: str = "",
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.http_status = int(http_status or 0)
        self.detail = detail or ""
        self.url = url or ""


class EngineOutcome(NamedTuple):
    """Per-engine result of :func:`search_engine_report` (test-search UI)."""

    engine: str
    ok: bool  # False = the engine errored (blocked, timed out, ...)
    results: List[WebResult]
    error: str  # human-readable guidance for the failure; "" when ok
    http_status: int = 0  # engine's HTTP status; 0 = unknown/not applicable
    detail: str = ""  # raw engine/library response text for troubleshooting
    note: str = ""  # non-error explanation, e.g. "answered but found nothing"
    url: str = ""  # actual search URL the engine was queried (clickable)


def extract_http_status(text: str) -> int:
    """Pull an HTTP error status code out of a library exception message.

    Search libraries embed the status in free-form text ("HTTP 403",
    "status code: 429", "403 Forbidden", ...). Returns 0 when none found.
    """
    match = re.search(r"\b([45]\d{2})\b", text or "")
    return int(match.group(1)) if match else 0


def status_guidance(status: int) -> str:
    """Actionable plain-English explanation for an HTTP status code."""
    if status == 202:
        return (
            "HTTP 202 - the engine is challenging this server (anti-bot / "
            "rate-limit check). Try again later, or disable this engine if it keeps happening."
        )
    if status == 401:
        return (
            "HTTP 401 - this engine appears to require an API key or login. "
            "Disable it here and use one of the free engines."
        )
    if status == 403:
        return (
            "HTTP 403 - the engine blocked this server (anti-bot protection). "
            "Try again later, or disable this engine if it keeps happening."
        )
    if status == 429:
        return (
            "HTTP 429 - the engine is throttling requests from this server. "
            "Try again in a minute or two; disable this engine if it continues."
        )
    if 400 <= status < 500:
        return f"HTTP {status} - the engine rejected the request."
    if 500 <= status < 600:
        return (
            f"HTTP {status} - the engine's own server failed. "
            "Try again later or use another engine."
        )
    return ""


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


def friendly_search_error(exc: Exception, http_status: int = 0) -> str:
    """Turn a raw search-library exception into a human-readable message.

    ``http_status`` (when non-zero) is the engine's HTTP status extracted from
    the exception text; it drives the guidance for 401/403/429/5xx even when
    the library words its message unusually.
    """
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

    # Status-specific guidance first (the user asked for exact per-code
    # explanations: throttled / API key required / blocked / server error).
    if http_status == 429:
        return status_guidance(429)
    try:
        from ddgs.exceptions import RatelimitException

        if isinstance(exc, RatelimitException):
            return status_guidance(429)
    except ImportError:  # library absent - fall through to text matching
        pass
    if "ratelimit" in lowered or "202" in text or "429" in text:
        return (
            "HTTP 202/429 - the engine is throttling requests from this server. "
            "Try again in a minute or two; disable this engine if it continues."
        )
    if http_status == 401:
        return status_guidance(401)
    if http_status == 403 or "forbidden" in lowered or "captcha" in lowered or "anubis" in lowered:
        return status_guidance(403)
    if http_status and 500 <= http_status < 600:
        return status_guidance(http_status)
    if "timeout" in lowered or "timed out" in lowered:
        return "Search request timed out. Check your internet connection and try again."
    if "connection" in lowered or "network" in lowered or "resolve" in lowered:
        return "Network error while searching. Check the container's internet connection and try again."
    return f"Search backend error: {text}"


def describe_engine_failure(exc: Exception) -> tuple:
    """Map any engine exception to ``(guidance, http_status, raw_detail)``."""
    text = str(exc)
    if isinstance(exc, SearchUnavailableError):
        status = exc.http_status or extract_http_status(text)
        return (text or friendly_search_error(exc, status), status, exc.detail or text)
    status = extract_http_status(text)
    return (friendly_search_error(exc, status), status, text)


# ---------------------------------------------------------------------------
# Engine list resolution
# ---------------------------------------------------------------------------

def configured_engines() -> List[dict]:
    """The engine order from settings, sanitized; defaults when unusable.

    Returns ``[{"name": str, "enabled": bool}, ...]`` in priority order. An
    empty or all-junk stored list falls back to the built-in default so a
    mis-edited settings.json can never leave searches with zero engines.
    """
    from homestew.config import (  # noqa: PLC0415 (settings singleton cycle)
        DEFAULT_MANUAL_SEARCH_ENGINES,
        normalize_search_engines,
        settings,
    )

    engines = normalize_search_engines(settings.MANUAL_SEARCH_ENGINES)
    if not engines:
        return [dict(e) for e in DEFAULT_MANUAL_SEARCH_ENGINES]
    return engines


def enabled_engines(engines: Optional[Iterable[dict]] = None) -> List[str]:
    """Names of the enabled engines in priority order."""
    if engines is None:
        engines = configured_engines()
    return [
        str(e.get("name", ""))
        for e in engines
        if isinstance(e, dict) and e.get("enabled", True) and e.get("name")
    ]


def engine_catalog() -> List[dict]:
    """Every engine the Settings > Search UI should show, in display order.

    Shape: ``[{"name", "enabled", "supported", "note"}, ...]``. Merges the
    configured list with SEARCH_ENGINE_CATALOG so engines added in a newer
    release (and ones ddgs ships disabled) appear even when the stored list
    predates them. Configured engines keep their position and enablement;
    catalog-only engines are appended in catalog order, disabled.

    ``supported`` is False for engines ddgs has disabled upstream (google,
    yandex); ``note`` explains why, and such an engine is always reported with
    ``enabled`` False so the UI cannot offer it as a working toggle.
    """
    from homestew.config import (  # noqa: PLC0415 (settings singleton cycle)
        SEARCH_ENGINE_CATALOG,
        UNSUPPORTED_SEARCH_ENGINES,
    )

    configured = {str(e["name"]): e for e in configured_engines()}
    ordered = list(configured) + [n for n in SEARCH_ENGINE_CATALOG if n not in configured]
    return [
        {
            "name": name,
            "enabled": bool(configured.get(name, {}).get("enabled", False)),
            "supported": name not in UNSUPPORTED_SEARCH_ENGINES,
            "note": UNSUPPORTED_SEARCH_ENGINES.get(name, ""),
        }
        for name in ordered
    ]


# ---------------------------------------------------------------------------
# Bing scraper (built-in engine)
# ---------------------------------------------------------------------------

_BING_URL = "https://www.bing.com/search?q={query}&count={count}&setlang=en"


def _bing_fetch_html(query: str, timeout: int) -> str:
    """Fetch the Bing results page HTML with a browser-like TLS client.

    primp (the HTTP client ddgs itself uses) impersonates Chrome, which is
    what keeps Bing from answering 403. ``random`` picks a current profile so
    the fingerprint rotates per request; family names ("chrome") stay valid
    across primp upgrades, unlike pinned versioned profiles.

    ``primp.Client.timeout`` is in *seconds* (primp 2.x), same unit as the
    ddgs constructor, so the value passes through without scaling.
    """
    import primp  # noqa: PLC0415

    client = primp.Client(
        impersonate="random",
        verify=False,
        timeout=max(1, timeout),
        referer=True,
    )
    url = _BING_URL.format(query=query, count=20)
    resp = client.get(url)
    status = int(getattr(resp, "status_code", 200))
    if status != 200:
        raise SearchUnavailableError(
            status_guidance(status) or f"Bing returned HTTP {status}",
            http_status=status,
            detail=f"Bing returned HTTP {status}",
            url=url,
        )
    return resp.text


def _bing_parse(html: str) -> List[WebResult]:
    """Extract organic results from Bing HTML and unwrap its /ck/a links.

    Organic hits live in ``li.b_algo`` blocks with the title anchor in an
    ``h2``; the href is a ``/ck/a?...u=a1<base64>`` redirect which
    manual_finder.unwrap_redirect() decodes to the real target URL.
    """
    from lxml import html as lxml_html  # noqa: PLC0415 (ddgs dependency)
    from homestew.services.manual_finder import unwrap_redirect  # noqa: PLC0415

    try:
        document = lxml_html.fromstring(html or "")
    except Exception:  # noqa: BLE001 - unparseable page == no results
        return []

    anchors = document.xpath("//li[contains(@class,'b_algo')]//h2/a[@href]")
    if not anchors:
        anchors = document.xpath("//li[contains(@class,'b_algo')]//a[@href]")

    results: List[WebResult] = []
    seen: set = set()
    for anchor in anchors:
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        url = unwrap_redirect(href)
        if not url.lower().startswith(("http://", "https://")):
            continue
        title = " ".join((anchor.text_content() or "").split())
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        results.append(WebResult(title=title[:200], url=url, engine=BING_ENGINE))
    return results


def search_bing(
    query: str,
    *,
    max_results: Optional[int] = None,
    timeout: int = 10,
    fetcher: Optional[Callable[[str, int], str]] = None,
) -> List[WebResult]:
    """Search Bing directly and return normalized results.

    ``fetcher`` (query, timeout) -> html is injectable for tests; the default
    hits bing.com through primp. Raises on HTTP/network errors - callers
    treat those as "this engine failed" and move to the next one.
    """
    fetch = fetcher or _bing_fetch_html
    html = fetch(query, timeout)
    results = _bing_parse(html)
    if max_results is not None:
        results = results[:max_results]
    logger.info("Bing search returned %d result(s)", len(results))
    return results


# ---------------------------------------------------------------------------
# ddgs engines
# ---------------------------------------------------------------------------

def search_ddgs_engine(
    query: str,
    *,
    engine: str,
    max_results: Optional[int] = None,
    region: Optional[str] = None,
    safesearch: str = "moderate",
) -> List[WebResult]:
    """Run one ddgs engine by name; empty list means "nothing found".

    Raises SearchUnavailableError with a friendly message when the engine
    itself fails (blocked, rate-limited, library broken).
    """
    from homestew.config import settings  # noqa: PLC0415 (avoid cycle at load)

    DDGS, DDGSException, _Ratelimit, _Timeout = _import_ddgs()

    resolved_region = (region if region is not None else settings.MANUAL_SEARCH_REGION).strip()
    timeout = max(1, int(settings.MANUAL_SEARCH_TIMEOUT))

    kwargs = {"backend": engine, "safesearch": safesearch}
    if resolved_region:
        kwargs["region"] = resolved_region
    if max_results is not None:
        kwargs["max_results"] = max_results

    # ddgs turns ANY non-200 engine response into "No results found.", so the
    # real status (429 throttle, 403 block, ...) is only visible in primp's
    # per-response log lines. Capture them for this engine run.
    with capture_http_events([]) as seen:
        try:
            with DDGS(proxy=settings.MANUAL_PROXY or None, timeout=timeout) as ddgs:
                raw = ddgs.text(query, **kwargs)
        except DDGSException as exc:
            text = str(exc)
            tried_status, tried_url = worst_http_event(seen)
            status = extract_http_status(text) or tried_status
            message = friendly_search_error(exc, status)
            if not message and (status < 400 and status != 202):
                # Genuinely no results (engine answered normally with nothing).
                return []
            if not message:  # engine errored with a status but generic text
                message = status_guidance(status) or f"Engine failed (HTTP {status})."
            logger.warning("ddgs engine %s failed (HTTP %s): %s", engine, status, exc)
            raise SearchUnavailableError(
                message, http_status=status, detail=text, url=tried_url
            ) from exc
        except Exception as exc:  # unexpected library/HTTP-client error
            logger.exception("Unexpected ddgs engine failure (%s)", engine)
            text = str(exc)
            tried_status, tried_url = worst_http_event(seen)
            status = extract_http_status(text) or tried_status
            raise SearchUnavailableError(
                friendly_search_error(exc, status),
                http_status=status,
                detail=text,
                url=tried_url,
            ) from exc

    results: List[WebResult] = []
    for item in raw or []:
        url = (item.get("href") or item.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        results.append(
            WebResult(
                title=(item.get("title") or "").strip(),
                url=url,
                # ddgs text results only carry title/href/body; the engine
                # name is the backend we asked for.
                engine=engine,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Public search entry points
# ---------------------------------------------------------------------------

def _run_engine(
    name: str,
    query: str,
    *,
    max_results: Optional[int],
    region: Optional[str],
    safesearch: str,
) -> List[WebResult]:
    """Dispatch one engine by name to its implementation."""
    if name == BING_ENGINE:
        from homestew.config import settings  # noqa: PLC0415

        return search_bing(
            query,
            max_results=max_results,
            timeout=max(1, int(settings.MANUAL_SEARCH_TIMEOUT)),
        )
    return search_ddgs_engine(
        query,
        engine=name,
        max_results=max_results,
        region=region,
        safesearch=safesearch,
    )


# Per-engine walk outcome recorded by :func:`search_web` (see ``outcomes``).
# Machine-readable so the Fetch Manuals dialog can label each engine's row:
#   accepted  - the engine answered and its results were used
#   unaccepted - the engine answered with results the caller rejected
#                (e.g. Bing replying to `filetype:pdf` with HTML landing pages)
#   empty     - the engine answered normally with zero results
#   error     - the engine failed (blocked, timed out, ...); ``error`` explains
ENGINE_STATUS_ACCEPTED = "accepted"
ENGINE_STATUS_UNACCEPTED = "unaccepted"
ENGINE_STATUS_EMPTY = "empty"
ENGINE_STATUS_ERROR = "error"

# Transient status pushed to live progress listeners only (never stored in the
# caller's ``outcomes`` list): the engine is the current one being queried.
# It is what lets the Fetch Manuals dialog show a search progressing engine by
# engine instead of sitting on one spinner until the whole chain finishes.
ENGINE_STATUS_SEARCHING = "searching"


def engine_outcome(
    name: str,
    status: str,
    *,
    results: int = 0,
    error: str = "",
) -> dict:
    """Build one per-engine walk record (see the ENGINE_STATUS_* constants)."""
    return {
        "engine": name,
        "status": status,
        "results": int(results or 0),
        "error": error or "",
    }


def merge_engine_outcomes(*lists: Optional[List[dict]]) -> List[dict]:
    """Merge per-engine walk records from several searches into one list.

    Keeps the order engines were first seen in and collapses repeats of the
    same engine to a single row, preferring the status that tells the user
    the most: ``accepted`` (the engine actually delivered results here) beats
    ``error``, which beats ``unaccepted``, which beats ``empty``. So an engine
    that was empty on the strict pass but answered on the relaxed one shows
    its real results, and a flaky engine is still reported as errored when it
    never delivered anything. The highest result count wins regardless.
    """
    priority = {
        ENGINE_STATUS_ACCEPTED: 3,
        ENGINE_STATUS_ERROR: 2,
        ENGINE_STATUS_UNACCEPTED: 1,
        ENGINE_STATUS_EMPTY: 0,
    }
    merged: dict = {}
    for outcomes in lists:
        for item in outcomes or []:
            name = item.get("engine") or "unknown"
            current = merged.get(name)
            if current is None:
                merged[name] = dict(item)
                continue
            if priority.get(item.get("status"), 0) > priority.get(current.get("status"), 0):
                merged[name] = {**current, **item}
            elif int(item.get("results") or 0) > int(current.get("results") or 0):
                current["results"] = item.get("results")
    return list(merged.values())


def search_web(
    query: str,
    *,
    max_results: Optional[int] = None,
    engines: Optional[Iterable[dict]] = None,
    region: Optional[str] = None,
    safesearch: str = "moderate",
    accept: Optional[Callable[[List["WebResult"]], bool]] = None,
    outcomes: Optional[List[dict]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> List[WebResult]:
    """Search the web across configured engines and return normalized results.

    Engines are tried in configured order (Settings > Search "Search Online
    for Manuals"); the first engine whose answer is accepted wins, so a blocked
    engine only costs its own timeout. An engine that errors is logged and
    skipped. Only when every attempted engine errored does this raise -
    engines that answered with nothing do not count as failures.

    ``accept`` decides what counts as a usable answer (default: any non-empty
    result). When provided, an engine that returns results which the caller
    considers useless does NOT stop the chain - the next engine is tried. This
    matters because engines disagree on operators like ``filetype:pdf``: Bing
    often answers such a query with HTTP 200 and a page full of brand landing
    pages (no PDFs at all), which would otherwise end the search before an
    engine that honors the operator (Yahoo, Brave) is ever queried. If no
    engine produces an accepted answer, the first non-empty result set is
    returned as a best-effort fallback rather than nothing.

    Args:
        query: The search query.
        max_results: Cap on returned results (library default when None).
        engines: Engine list override (``[{"name","enabled"}, ...]``);
            defaults to the configured list, falling back to the built-in
            default order when nothing usable is configured.
        region: ddgs region token ("us-en", "de-de", ...); library default
            when None/"". Ignored by the built-in Bing engine.
        safesearch: "on"/"moderate"/"off".
        accept: ``(results) -> bool`` predicate; the first engine whose results
            satisfy it wins. ``None`` means "stop at the first non-empty set".
        outcomes: Optional list that receives one record per attempted engine
            (``{"engine", "status", "results", "error"}`` - see
            :func:`engine_outcome`). The Fetch Manuals dialog uses this to show
            which engines worked and which are broken; recording never changes
            the search itself.
        on_progress: Optional callback invoked with one record per engine as
            the walk happens: ``{"phase": "start", "engine", "index", "total"}``
            just before an engine is queried, then ``{"phase": "end", ...}``
            carrying the same fields as :func:`engine_outcome`. Unlike
            ``outcomes`` this also reports ``status="searching"``, which only
            ever exists while a query is in flight. Used by the streaming
            search endpoint to update the dialog live; a callback that raises
            is swallowed so UI problems cannot break a search.

    Returns:
        List of :class:`WebResult` (possibly empty - empty means "nothing
        found", which is NOT an error).

    Raises:
        SearchUnavailableError: No engine is enabled, or every enabled
            engine failed. The message explains what happened.
    """
    names = enabled_engines(engines)
    if not names:
        raise SearchUnavailableError(
            "No search engines are enabled. Enable at least one engine in "
            "Settings > Search (Search Online for Manuals)."
        )

    total = len(names)

    def _emit(record: dict) -> None:
        if on_progress is None:
            return
        try:
            on_progress(record)
        except Exception:  # progress reporting must never break a search
            logger.debug("Search progress callback failed", exc_info=True)

    def _report(position: int, outcome: dict) -> None:
        """Record a finished engine and tell the live listener about it."""
        if outcomes is not None:
            outcomes.append(outcome)
        _emit({**outcome, "phase": "end", "index": position + 1, "total": total})

    errors: List[str] = []
    fallback: Optional[List[WebResult]] = None  # first non-empty, used if none accepted
    for position, name in enumerate(names):
        logger.info("Web search via %s: %s", name, query)
        _emit({"phase": "start", "engine": name, "index": position + 1, "total": total})
        try:
            results = _run_engine(
                name,
                query,
                max_results=max_results,
                region=region,
                safesearch=safesearch,
            )
        except Exception as exc:  # engine-internal bug must not kill the chain
            guidance, status, detail = describe_engine_failure(exc)
            if not isinstance(exc, SearchUnavailableError):
                logger.exception("Unexpected %s search failure", name)
            errors.append(f"{name}: {guidance}" + (f" [{status}]" if status else ""))
            logger.debug("Engine %s failure detail: %s", name, detail)
            _report(position, engine_outcome(name, ENGINE_STATUS_ERROR, error=guidance))
            continue
        if results:
            if fallback is None:
                fallback = results
            # Without a predicate any non-empty answer wins; with one, only an
            # accepted answer does - otherwise keep walking the engine list.
            if accept is None or accept(results):
                logger.info(
                    "Web search returned %d normalized result(s) via %s", len(results), name
                )
                _report(
                    position,
                    engine_outcome(name, ENGINE_STATUS_ACCEPTED, results=len(results)),
                )
                return results
            logger.info(
                "Engine %s returned %d result(s) but none accepted; trying next engine",
                name,
                len(results),
            )
            _report(
                position,
                engine_outcome(name, ENGINE_STATUS_UNACCEPTED, results=len(results)),
            )
        else:
            logger.info("Engine %s returned no results for: %s", name, query)
            _report(position, engine_outcome(name, ENGINE_STATUS_EMPTY))

    if fallback is not None:
        logger.info(
            "No engine produced an accepted answer; returning first non-empty result set"
        )
        return fallback
    if errors:
        raise SearchUnavailableError("; ".join(errors))
    return []


def search_engine_report(
    query: str,
    *,
    engines: Optional[Iterable[str]] = None,
    max_results: int = 5,
    region: Optional[str] = None,
    safesearch: str = "moderate",
) -> List[EngineOutcome]:
    """Test-search each engine independently (Settings > Search test UI).

    Unlike :func:`search_web` this never stops at the first hit - every
    requested engine is queried so the user sees exactly which ones work from
    this network. With no ``engines`` filter it tests every *supported* engine
    in the catalog (not just the enabled/stored ones), so newly exposed
    engines show up in "Test All Engines" before they are saved into settings.
    Unsupported engines (ddgs-disabled google/yandex) are excluded from the
    default set - asking ddgs for one silently runs every engine, which would
    make a meaningless test result. ``engines`` filters to a subset by name;
    names outside the catalog still get tested on an explicit request.
    Never raises: per-engine failures come back as ``EngineOutcome(ok=False,
    error=...)``.
    """
    catalog = engine_catalog()
    supported = [str(e["name"]) for e in catalog if e.get("supported", True)]
    wanted = [str(n).strip().lower() for n in (engines or []) if str(n).strip()]
    if wanted:
        ordered = [n for n in supported if n in wanted]
    else:
        ordered = list(supported)
    # Names requested but not in the supported catalog still get tested (the
    # UI only sends supported names, but an ad-hoc API call should work).
    for name in wanted:
        if name not in ordered:
            ordered.append(name)

    outcomes: List[EngineOutcome] = []
    for name in ordered:
        with capture_http_events([]) as seen:
            try:
                results = _run_engine(
                    name,
                    query,
                    max_results=max_results,
                    region=region,
                    safesearch=safesearch,
                )
            except Exception as exc:  # noqa: BLE001 - report, never propagate
                guidance, status, detail = describe_engine_failure(exc)
                tried_status, tried_url = worst_http_event(seen)
                if isinstance(exc, SearchUnavailableError) and exc.url:
                    tried_url = exc.url
                if not status:
                    status = tried_status
                    if status and guidance == f"Search backend error: {detail}":
                        guidance = status_guidance(status) or guidance
                logger.info("Test search failed on %s: %s", name, guidance)
                outcomes.append(
                    EngineOutcome(
                        engine=name,
                        ok=False,
                        results=[],
                        error=guidance,
                        http_status=status,
                        detail=detail,
                        url=tried_url,
                    )
                )
                continue

        # Success path: report the actual status primp saw (200 unless a
        # challenge/teapot slipped through), not a hardcoded 200.
        status, tried_url = worst_http_event(seen)
        outcomes.append(
            EngineOutcome(
                engine=name,
                ok=True,
                results=results,
                error="",
                http_status=status or 200,
                url=tried_url,
                note=(
                    ""
                    if results
                    else f"Engine responded normally (HTTP {status or 200}) but returned no results for this query."
                ),
            )
        )
    return outcomes
