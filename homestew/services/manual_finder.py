"""Turn a brand/model into ranked PDF manual candidates.

Pure domain logic on top of :mod:`homestew.services.web_search`: builds the
query, unwraps search-engine redirect links, filters to plausible PDFs and
ranks what remains. No I/O beyond the injected ``search`` callable (defaults
to :func:`web_search.search_web`), which keeps this module unit-testable with
a fake and reusable by a future LLM tool without touching downloads or disk.

Why each step exists:
- ``filetype:pdf`` in the query lets engines do the PDF filtering server-side;
  post-filtering on URL/title alone missed manuals hosted behind extension-less
  URLs while accepting HTML pages that merely mention "PDF".
- Search engines wrap outbound links in redirects (DuckDuckGo
  ``/l/?uddg=<urlencoded>``, Bing ``/ck/a`` with a base64-encoded ``u=``). The
  old code tested ``'.pdf' in href`` on the *redirect* URL, so real PDFs were
  invisible to the filter.
- Ranking prefers manufacturer domains: for "Nespresso PIXIE C62" the official
  nespresso.com manual beats a random forum attachment every time.
"""
import logging
import re
from base64 import b64decode
from typing import Callable, List, NamedTuple, Optional, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

logger = logging.getLogger(__name__)


class ManualCandidate(NamedTuple):
    """A plausible manual PDF, ready to download."""

    title: str
    url: str
    engine: str


# Words that indicate a result is NOT the document we want.
_NEGATIVE_TITLE_RE = re.compile(
    r"\b(video|youtube|amazon\.com|reddit|forum|review|coupon|deal)\b", re.I
)

# Aggregator domains known for hosting device documentation; matched as
# substring of the lowercased host. Mid-tier: below the manufacturer's own
# domain, above random sites.
_OFFICIAL_HINTS = ("manualslib", "manualzz", "manualsonline")


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def build_query(brand: str, model: str, search_terms: Optional[str] = None) -> str:
    """Search query for a device manual PDF.

    ``search_terms`` (user-edited terms in the UI) replaces the brand + model
    part; the manual/filetype hints are still appended.
    """
    terms = (search_terms or "").strip()
    base = terms if terms else f"{brand} {model}"
    return f"{base} manual filetype:pdf"


def relaxed_query(brand: str, model: str, search_terms: Optional[str] = None) -> str:
    """Fallback query without the ``filetype:`` operator.

    Engines treat ``filetype:pdf`` inconsistently - Bing ignores it outright
    and Yahoo often finds nothing with it while returning PDFs for the same
    words without it. Dropping the operator (and using the broader "user
    guide pdf" phrasing) recovers those documents; :func:`looks_like_pdf`
    still filters the results down to actual PDFs afterwards.
    """
    terms = (search_terms or "").strip()
    base = terms if terms else f"{brand} {model}"
    return f"{base} user guide pdf"


def _decode_base64_url(raw: str) -> str:
    """Decode a padding-stripped, url-safe base64 URL (Bing's encoding).

    The decoded text may itself still be percent-encoded, so it is unquoted
    before being validated. Returns "" for anything that isn't an http(s) URL.
    """
    if not raw:
        return ""
    try:
        padded = unquote(raw)
        padded += "=" * (-len(padded) % 4)
        decoded = unquote(b64decode(padded, altchars="-_", validate=False).decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001 - any decoding weirdness -> no target
        return ""
    return decoded if decoded.lower().startswith(("http://", "https://")) else ""


def unwrap_redirect(url: str) -> str:
    """Resolve search-engine redirect links to their target URL.

    Handles DuckDuckGo's ``/l/?uddg=...``, Google's ``/url?q=...`` and Bing's
    ``/ck/a?...&u=a1<base64>`` formats, plus a generic ``?u=/url=/target=``
    parameter. Anything unrecognized is returned unchanged; empty input maps
    to an empty string so callers can pre-filter later.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) if target else url

    if "google." in host and parsed.path in ("/url", "/link"):
        target = parse_qs(parsed.query).get("q", [""])[0]
        return unquote(target) if target else url

    if host.endswith("bing.com") and parsed.path.startswith("/ck/"):
        # Bing base64-encodes the target with a 2-char prefix ("a1"), url-safe,
        # stripped of padding. Malformed values fall through to `url`.
        raw = parse_qs(parsed.query).get("u", [""])[0]
        if raw:
            try:
                padded = unquote(raw)[2:]
                padded += "=" * (-len(padded) % 4)
                decoded = b64decode(padded, altchars="-_", validate=False).decode("utf-8", "ignore")
                if decoded.lower().startswith(("http://", "https://")):
                    return decoded
            except Exception:  # noqa: BLE001 - any decoding weirdness -> keep original
                pass
        return url

    if host.endswith("bing.com") and parsed.path.startswith("/aclick"):
        # Sponsored-result wrapper: `u=` holds a padding-stripped base64 of the
        # (still percent-encoded) destination. Ad landing pages are usually not
        # the document we want, but unwrapping lets the filters judge the real
        # URL instead of Bing's tracking link.
        raw = parse_qs(parsed.query).get("u", [""])[0]
        target = _decode_base64_url(raw)
        return target or url

    params = parse_qs(parsed.query)
    for key in ("u", "url", "target", "dest"):
        target = params.get(key, [""])[0]
        if target and target.lower().startswith(("http://", "https://")):
            return unquote(target)

    # Protocol-relative links from scraped pages (//example.com/a.pdf).
    if url.startswith("//"):
        return "https:" + url
    return url


def looks_like_pdf(url: str, title: str = "") -> bool:
    """Heuristic: is this result plausibly a PDF document?"""
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return True
    # Extension-less but explicit paths like /downloads/manual-c62.pdf?v=2 are
    # caught above via .pdf; also accept query strings carrying the filetype.
    if ".pdf" in url.lower():
        return True
    return bool(re.search(r"\bpdf\b", title or "", re.I))


def _is_engine_tracking_url(url: str) -> bool:
    """True for search-engine redirect/ad wrappers we could not resolve.

    An unresolved wrapper tells us nothing about the document behind it (and
    often leads to a sponsored landing page), so such results are dropped
    rather than offered as manuals.
    """
    host = _host_of(url)
    path = urlparse(url).path.lower()
    if host.endswith("bing.com") and path.startswith(("/ck/", "/aclick")):
        return True
    if host.endswith("duckduckgo.com") and path.startswith("/l/"):
        return True
    return False


def _normalize_result(item) -> Optional[Tuple[str, str, str]]:
    """Normalize one raw search result to ``(url, title, engine)``.

    Accepts normalized :class:`web_search.WebResult` objects and raw ddgs
    dicts (which use ``href``). Returns ``None`` when no URL survives the
    redirect unwrap; otherwise the URL is absolute (https-resolved).
    """
    url = getattr(item, "url", None)
    if url is None and isinstance(item, dict):
        # Raw ddgs result dicts use "href"; normalized WebResult has .url.
        url = item.get("url") or item.get("href")
    url = unwrap_redirect(url or "")
    if not url:
        return None
    # Resolve protocol-relative leftovers against https.
    url = urljoin("https://", url)
    if _is_engine_tracking_url(url):
        return None
    title = getattr(item, "title", None) or (item.get("title", "") if isinstance(item, dict) else "")
    engine = getattr(item, "engine", None) or (item.get("engine", "") if isinstance(item, dict) else "")
    return url, title, engine


def _result_is_manual(item) -> bool:
    """True when one raw search result is a usable manual PDF candidate.

    Applies the same redirect-unwrap + negative/PDF filters that
    :func:`find_manual_links` applies downstream, so it can be handed to
    :func:`web_search.search_web` as its ``accept`` predicate: an engine whose
    results all fail this check (e.g. Bing answering a ``filetype:pdf`` query
    with brand landing pages) is skipped instead of ending the search.
    """
    normalized = _normalize_result(item)
    if normalized is None:
        return False
    url, title, _engine = normalized
    if _NEGATIVE_TITLE_RE.search(f"{title} {url}"):
        return False
    return looks_like_pdf(url, title)


def has_manual_results(results) -> bool:
    """``accept`` predicate for search_web: does any result look like a PDF?"""
    return any(_result_is_manual(item) for item in (results or []))


def _rank(candidate: ManualCandidate, brand: str, model: str) -> int:
    """Ascending sort key (negated score): manufacturer > aggregator > rest."""
    host = _host_of(candidate.url)
    brand_tokens = [t for t in re.split(r"[^a-z0-9]+", (brand or "").lower()) if len(t) >= 3]

    score = 0
    # Brand domain (nespresso.com, samsung.com...) hosting the file: top tier.
    if any(token in host for token in brand_tokens):
        score += 100
    elif any(hint in host for hint in _OFFICIAL_HINTS):
        score += 50
    if urlparse(candidate.url).path.lower().endswith(".pdf"):
        score += 20
    model_norm = (model or "").lower().replace(" ", "")
    hay = f"{candidate.title} {candidate.url}".lower().replace(" ", "")
    if model_norm and model_norm in hay:
        score += 10
    return -score  # ascending sort with negated score


def find_manual_links(
    brand: str,
    model: str,
    *,
    max_results: Optional[int] = None,
    search: Optional[Callable[..., List]] = None,
    search_terms: Optional[str] = None,
    use_filetype_operator: bool = True,
    outcomes: Optional[List[dict]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> Tuple[List[ManualCandidate], Optional[str]]:
    """Find ranked PDF manual candidates for a device.

    Args:
        brand/model: Device identifiers used to build the query (and to rank
            results even when custom terms replaced them).
        max_results: Candidate cap (settings default when None).
        search: Injectable search callable matching
            :func:`web_search.search_web`'s signature (tests pass a fake).
        search_terms: Optional user-supplied terms replacing brand + model in
            the query; the manual hints are still appended so results stay
            manuals. Blank/None keeps the default brand/model query.
        use_filetype_operator: Append ``filetype:pdf`` to the query (default).
            Pass ``False`` on a retry when the operator found nothing - some
            engines return zero results with it and good PDFs without it.
        outcomes: Optional list receiving the per-engine walk records from the
            search layer (see :func:`web_search.search_web`), so the UI can
            show which engines answered and which are broken.
        on_progress: Optional live per-engine progress callback forwarded to
            the search layer; reporting cannot change the results.

    Returns:
        ``(candidates, error_message_or_None)``. An empty candidate list with
        no error means the search worked but nothing plausible was found.
    """
    from homestew.config import settings  # noqa: PLC0415 (import cycle at module load)

    if max_results is None:
        max_results = settings.MANUAL_MAX_RESULTS

    from homestew.services.web_search import SearchUnavailableError, friendly_search_error  # noqa: PLC0415

    if search is None:
        from homestew.services.web_search import search_web  # noqa: PLC0415

        search = search_web

    query = (
        build_query(brand, model, search_terms)
        if use_filetype_operator
        else relaxed_query(brand, model, search_terms)
    )
    try:
        raw_results = search(
            query,
            max_results=max(10, max_results * 2),
            # Engines disagree on `filetype:pdf`. Bing frequently answers with
            # HTTP 200 and a page of brand landing pages (no PDFs); without
            # this predicate the chain would stop there and every real manual
            # offered by a later engine would go unqueried.
            accept=has_manual_results,
            # Only real search_web supports these; injectable test fakes may
            # not, so pass them only when the caller actually wants them.
            **({"outcomes": outcomes} if outcomes is not None else {}),
            **({"on_progress": on_progress} if on_progress is not None else {}),
        )
    except SearchUnavailableError as exc:
        # Already a human-facing message from the search layer.
        return [], str(exc)
    except Exception as exc:  # unexpected failure - normalize like web_search does
        logger.exception("Unexpected manual search failure")
        return [], friendly_search_error(exc) or f"Search failed: {exc}"

    seen: set = set()
    candidates: List[ManualCandidate] = []
    for item in raw_results or []:
        normalized = _normalize_result(item)
        if normalized is None:
            continue
        url, title, engine = normalized
        if _NEGATIVE_TITLE_RE.search(f"{title} {url}"):
            continue
        if not looks_like_pdf(url, title):
            continue
        key = (_host_of(url), urlparse(url).path)
        if key in seen:  # same document offered by several engines
            continue
        seen.add(key)
        candidates.append(ManualCandidate(title=title[:200] or url[:200], url=url, engine=engine))

    candidates.sort(key=lambda c: _rank(c, brand, model))
    ranked = [c for c in candidates][: max_results]
    logger.info("Found %d PDF candidate(s) for %r (%d raw result(s))", len(ranked), query, len(raw_results or []))
    return ranked, None
