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


def build_query(brand: str, model: str) -> str:
    """Search query for a device manual PDF."""
    return f"{brand} {model} manual filetype:pdf"


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
) -> Tuple[List[ManualCandidate], Optional[str]]:
    """Find ranked PDF manual candidates for a device.

    Args:
        brand/model: Device identifiers used to build the query.
        max_results: Candidate cap (settings default when None).
        search: Injectable search callable matching
            :func:`web_search.search_web`'s signature (tests pass a fake).

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

    query = build_query(brand, model)
    try:
        raw_results = search(query, max_results=max(10, max_results * 2))
    except SearchUnavailableError as exc:
        # Already a human-facing message from the search layer.
        return [], str(exc)
    except Exception as exc:  # unexpected failure - normalize like web_search does
        logger.exception("Unexpected manual search failure")
        return [], friendly_search_error(exc) or f"Search failed: {exc}"

    seen: set = set()
    candidates: List[ManualCandidate] = []
    for item in raw_results or []:
        url = getattr(item, "url", None)
        if url is None and isinstance(item, dict):
            # Raw ddgs result dicts use "href"; normalized WebResult has .url.
            url = item.get("url") or item.get("href")
        url = unwrap_redirect(url or "")
        title = getattr(item, "title", None) or (item.get("title", "") if isinstance(item, dict) else "")
        engine = getattr(item, "engine", None) or (item.get("engine", "") if isinstance(item, dict) else "")
        if not url:
            continue
        # Resolve protocol-relative leftovers against https.
        url = urljoin("https://", url)
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
