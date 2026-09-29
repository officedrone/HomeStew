"""User-approved manual fetching: search candidates, then store one PDF at a time.

Successor to ``manual_downloader`` after manuals moved from "search and
auto-download everything" to "search, present the list, approve each
download". Candidate discovery and engine quirks stay in
:mod:`homestew.services.manual_finder` / :mod:`web_search`; this module adds
what the approval UI needs on top:

- :func:`find_manual_candidates` - search plus per-candidate display metadata
  (PDF name, source domain) and an ``already_downloaded`` flag derived from
  the stored ``manuals.source_url`` column.
- :func:`store_manual_for_device` - download ONE approved candidate, register
  it in the ``manuals`` table with its source URL and index it for search.
  Re-downloading a known URL is refused unless ``replace=True``, so existing
  manuals are never silently overwritten or duplicated.

Blocking work (web search, ``requests.get``) runs through
``run_in_threadpool`` so the FastAPI event loop stays responsive.
"""
import logging
import re
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

import requests

logger = logging.getLogger(__name__)


class ManualExistsError(Exception):
    """The URL is already stored for this device and replace was not asked."""

    def __init__(self, filename: str):
        super().__init__(f"Manual '{filename}' is already downloaded")
        self.filename = filename


class ManualDownloadError(Exception):
    """An approved candidate could not be fetched (network, cap, not a PDF)."""


# Second-level suffixes so "manuals.co.uk" displays as the registrable domain
# rather than just "co.uk". Not exhaustive - display-only heuristic.
_CC_TLD_SECOND_LEVELS = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk",
    "com.au", "net.au", "org.au", "co.nz", "co.jp", "or.jp",
    "co.in", "com.br", "com.mx", "co.za", "com.sg", "com.hk",
    "com.tr", "co.kr", "com.ar", "com.pl",
}


def domain_of(url: str) -> str:
    """Top-level (registrable-ish) domain of a URL, for the Source column."""
    host = (urlparse(url).hostname or "").lower()
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host
    if f"{labels[-2]}.{labels[-1]}" in _CC_TLD_SECOND_LEVELS and len(labels) >= 3:
        return ".".join(labels[-3:])
    return f"{labels[-2]}.{labels[-1]}"


# URL basenames that carry no information about the document itself; when a
# candidate's path ends in one of these, the search title is the better name.
_GENERIC_BASENAMES = {"download", "downloads", "file", "files", "get", "doc", "docs", "index"}


def display_name_for(url: str, title: str = "") -> str:
    """PDF file name to show for a candidate: URL basename when usable."""
    base = unquote(Path(urlparse(url).path).name)
    base = re.sub(r"[^\w.\- ()]+", "_", base).strip(" ._")
    if base.lower().removesuffix(".pdf") in _GENERIC_BASENAMES:
        base = ""
    if base.lower().endswith(".pdf"):
        return _clip(base)
    if base:
        return _clip(f"{base}.pdf")
    # Extension-less download endpoints (/download?id=9): fall back to title.
    clean = re.sub(r"\s+", " ", (title or "")).strip()
    if clean:
        if "pdf" not in clean.lower():
            clean += ".pdf"
        return _clip(clean)
    return _clip(url)


def _clip(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def search_for_manuals(brand: str, model: str, max_results: Optional[int] = None) -> Tuple[List[dict], Optional[str]]:
    """Search the web for device manual PDFs.

    Returns (list of {"title", "url", "source"} dicts, error or None). Empty
    list without an error means the search worked but nothing plausible was
    found; empty list WITH an error explains why.
    """
    from homestew.config import settings  # noqa: PLC0415 (settings singleton cycle)
    from homestew.services.manual_finder import find_manual_links

    if max_results is None:
        max_results = settings.MANUAL_MAX_RESULTS
    logger.info(f"Searching for manuals: {brand} {model}")

    candidates, error = find_manual_links(brand, model, max_results=max_results)
    results = [{"title": c.title, "url": c.url, "source": c.engine or "web"} for c in candidates]

    if not results and error:
        # One retry: ddgs rotates engines per call, so a single blocked or
        # rate-limited engine may succeed on the second pass.
        logger.info("Retrying search once after error: %s", error)
        time.sleep(1)
        candidates, error = find_manual_links(brand, model, max_results=max_results)
        results = [{"title": c.title, "url": c.url, "source": c.engine or "web"} for c in candidates]

    logger.info(f"Found {len(results)} potential manuals")
    if not results:
        return [], error or "No manuals found. Try a different brand/model or search terms."
    return results, None


def enrich_candidates(results: List[dict], existing_urls: Set[str]) -> List[dict]:
    """Add UI metadata (name/domain) and the already-downloaded flag.

    ``existing_urls`` is the set of source URLs stored for the device; a
    candidate matching one is highlighted in the table instead of being
    offered as a fresh download.
    """
    enriched = []
    for item in results:
        url = item.get("url", "")
        title = item.get("title", "") or ""
        enriched.append(
            {
                "name": display_name_for(url, title),
                "title": title[:200],
                "url": url,
                "domain": domain_of(url),
                "already_downloaded": bool(url) and url in existing_urls,
            }
        )
    return enriched


def download_pdf_detail(
    url: str,
    save_path: Path,
    timeout: Optional[int] = None,
    max_mb: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """Download a PDF file from URL with content validation and size cap.

    Returns (ok, reason). The reason is human-readable and surfaced in the UI
    on failure ("not a PDF", "too large", network errors). The file is
    streamed to a .part sibling and only renamed once it is proven to be a
    real PDF, so a failed download never leaves a corrupt manual behind - and
    replacing an existing manual only touches the old file on success.
    """
    from homestew.config import settings  # noqa: PLC0415

    if timeout is None:
        timeout = settings.MANUAL_DOWNLOAD_TIMEOUT
    if max_mb is None:
        max_mb = settings.MANUAL_MAX_PDF_MB
    max_bytes = max(1, int(max_mb)) * 1024 * 1024

    logger.info(f"Downloading PDF from {url}")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
    }

    tmp_path = save_path.with_suffix(save_path.suffix + ".part")

    class _TooLarge(Exception):
        def __init__(self, cap_mb: int):
            super().__init__(f"exceeds {cap_mb} MB")
            self.max_mb = cap_mb

    def _cleanup() -> None:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    written = 0
    try:
        with requests.get(url, headers=headers, stream=True, timeout=timeout) as response:
            response.raise_for_status()

            # Cheap pre-checks before touching the disk. Some servers label
            # PDFs text/html or octet-stream, so a content-type mismatch only
            # demotes us to the magic-byte check below - not a rejection.
            content_type = response.headers.get("content-type", "").lower()

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return False, f"file is too large ({int(declared) // (1024 * 1024)} MB, cap {max_mb} MB)"

            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise _TooLarge(max_mb)
                    f.write(chunk)

        if written < 1024:
            _cleanup()
            return False, "file is empty or too small to be a manual"

        # Authoritative check: real PDFs start with %PDF. Catches HTML error/
        # login pages served under a .pdf URL just as well as a wrong header.
        with open(tmp_path, "rb") as f:
            is_pdf = f.read(8).startswith(b"%PDF")
        if not is_pdf:
            _cleanup()
            detail = f" (content-type: {content_type})" if content_type else ""
            return False, f"not a valid PDF{detail} - the link may point to a web page"

        tmp_path.replace(save_path)
        logger.info(f"Downloaded {written} bytes to {save_path}")
        return True, None

    except _TooLarge as exc:
        _cleanup()
        return False, f"file exceeds the size cap ({exc.max_mb} MB)"
    except requests.exceptions.Timeout:
        _cleanup()
        return False, "download timed out"
    except requests.exceptions.RequestException as exc:
        _cleanup()
        logger.error(f"Download failed for {url}: {exc}")
        return False, f"network error: {exc.__class__.__name__}"
    except OSError as exc:
        _cleanup()
        logger.error(f"Failed writing {save_path}: {exc}")
        return False, "could not write the file to disk"


def _sanitize_filename(filename: str) -> str:
    """Keep only a safe basename; ensure it ends in .pdf."""
    name = Path(filename).name
    name = re.sub(r"[^\w.\- ]+", "_", name).strip() or "manual.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def _unique_filename(device_dir: Path, filename: str) -> str:
    """Avoid overwriting an unrelated manual that happens to share the name."""
    candidate = device_dir / filename
    if not candidate.exists():
        return filename

    stem = Path(filename).stem
    counter = 2
    while (device_dir / f"{stem}_{counter}.pdf").exists():
        counter += 1
    return f"{stem}_{counter}.pdf"


def _stored_filename(url: str, brand: str, model: str) -> str:
    """File name for a stored manual: URL basename when it looks like one."""
    base = unquote(Path(urlparse(url).path).name)
    if base.lower().endswith(".pdf"):
        return _sanitize_filename(base)
    safe_brand = re.sub(r"[^\w.\-]+", "_", brand or "device").strip("_") or "device"
    safe_model = re.sub(r"[^\w.\-]+", "_", model or "manual").strip("_") or "manual"
    return f"{safe_brand}_{safe_model}_manual.pdf"


async def find_manual_candidates(
    device_id: int, brand: str, model: str
) -> Tuple[List[dict], Optional[str]]:
    """Search for candidates and flag the ones already stored for the device."""
    from fastapi.concurrency import run_in_threadpool

    from homestew.db import get_db_context

    results, error = await run_in_threadpool(search_for_manuals, brand, model)
    if not results:
        return [], error or f"No manuals found for {brand} {model}"

    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT source_url FROM manuals WHERE device_id = ? AND source_url IS NOT NULL",
            (device_id,),
        )
        existing_urls = {row["source_url"] for row in await cursor.fetchall()}

    return enrich_candidates(results, existing_urls), None


async def store_manual_for_device(
    device_id: int,
    brand: str,
    model: str,
    url: str,
    title: Optional[str] = None,
    replace: bool = False,
) -> dict:
    """Download one approved candidate and register + index it.

    Raises :class:`ManualExistsError` when the URL is already stored for this
    device and ``replace`` was not requested (the UI confirms with the user
    first, then retries with replace=True). A replace reuses the existing row
    and file path; a fresh download gets a unique name so unrelated manuals
    are never clobbered.

    Returns {"manual_id", "filename", "replaced", "indexed"}.
    """
    from fastapi.concurrency import run_in_threadpool

    from homestew.config import settings
    from homestew.db import get_db_context
    from homestew.services.indexer import index_manual

    async with get_db_context() as db:
        cursor = await db.execute(
            "SELECT id, filename, filepath FROM manuals WHERE device_id = ? AND source_url = ? LIMIT 1",
            (device_id, url),
        )
        existing = await cursor.fetchone()

    if existing and not replace:
        raise ManualExistsError(existing["filename"])

    device_dir = settings.DEVICES_DIR / str(device_id) / "manuals"
    device_dir.mkdir(parents=True, exist_ok=True)

    if existing and replace:
        # Refresh in place: same row, same path (download_pdf_detail swaps the
        # file atomically only after validating it is a real PDF).
        filename = existing["filename"]
        save_path = Path(existing["filepath"])
    else:
        filename = _unique_filename(device_dir, _stored_filename(url, brand, model))
        save_path = device_dir / filename

    ok, reason = await run_in_threadpool(download_pdf_detail, url, save_path)
    if not ok:
        raise ManualDownloadError(reason or "download failed")

    # Register first and COMMIT before indexing: index_manual opens its own
    # connection and SQLite rejects a second writer while this transaction is
    # still open ("database is locked").
    async with get_db_context() as db:
        if existing and replace:
            manual_id = existing["id"]
            await db.execute(
                "UPDATE manuals SET filename = ?, filepath = ?, source_url = ? WHERE id = ?",
                (filename, str(save_path), url, manual_id),
            )
        else:
            cursor = await db.execute(
                "INSERT INTO manuals (device_id, filename, filepath, source_url) VALUES (?, ?, ?, ?) RETURNING id",
                (device_id, filename, str(save_path), url),
            )
            row = await cursor.fetchone()
            manual_id = row["id"]
        await db.commit()

    indexed = False
    if settings.INDEX_AUTO_ON_UPLOAD:
        try:
            indexed = await index_manual(
                manual_id=manual_id, device_id=device_id, pdf_path=str(save_path), filename=filename
            )
        except Exception as exc:  # indexing failure must not lose the download
            logger.error(f"Failed to index stored manual {filename}: {exc}")

    return {"manual_id": manual_id, "filename": filename, "replaced": bool(existing), "indexed": indexed}
