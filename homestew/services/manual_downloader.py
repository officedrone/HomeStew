"""Manual fetching: web search (via manual_finder/web_search) + PDF download.

This module is deliberately I/O-only: candidate discovery lives in
``manual_finder`` and the engine-agnostic search in ``web_search``, so a
future LLM tool can reuse those layers without pulling in downloads or disk.

Public contract kept stable for ``homestew/api/downloads.py`` (both the
legacy /trigger endpoint and the SSE stream):
- ``search_for_manuals(brand, model) -> (list_of_dicts, error_or_None)``
- ``download_pdf(url, save_path) -> bool``  (reason via ``download_pdf_detail``)
- ``download_manuals_for_device(...) -> (count, filenames, error_or_None)``
  with dict progress payloads ``{"type": "step", "message", "status"}``.
"""
import logging
import time
from pathlib import Path
from typing import Callable, List, Optional

import requests

logger = logging.getLogger(__name__)


def _friendly_error(exc: Exception) -> str:
    """Turn a raw search-library exception into a human-readable message."""
    from homestew.services.web_search import friendly_search_error

    return friendly_search_error(exc)


class _TooLarge(Exception):
    """Internal: streamed body exceeded the configured size cap."""

    def __init__(self, max_mb: int):
        super().__init__(f"exceeds {max_mb} MB")
        self.max_mb = max_mb


def search_for_manuals(brand: str, model: str, max_results: Optional[int] = None) -> tuple[List[dict], Optional[str]]:
    """Search the web for device manual PDFs.

    Args:
        brand: Device brand (e.g., "Nespresso")
        model: Device model (e.g., "PIXIE C62")
        max_results: Maximum number of candidates (settings default when None)

    Returns:
        Tuple of (list of {"title", "url", "source"} dicts, error_message or None).
        Empty list without an error means the search worked but nothing
        plausible was found; empty list WITH an error explains why.
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
        # rate-limited engine may succeed on the second pass. (The old
        # lru_cache also cached *errors*, which turned one transient failure
        # into a sticky one until the process restarted.)
        logger.info("Retrying search once after error: %s", error)
        time.sleep(1)
        candidates, error = find_manual_links(brand, model, max_results=max_results)
        results = [{"title": c.title, "url": c.url, "source": c.engine or "web"} for c in candidates]

    logger.info(f"Found {len(results)} potential manuals")
    if not results:
        return [], error or "No manuals found. Try a different brand/model or search terms."
    return results, None


def download_pdf_detail(
    url: str,
    save_path: Path,
    timeout: Optional[int] = None,
    max_mb: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """Download a PDF file from URL with content validation and size cap.

    Returns (ok, reason). The reason is human-readable and surfaced in the UI
    on failure ("not a PDF", "too large", network errors), instead of the old
    bare False that left users guessing. The file is streamed to a .part
    sibling and only renamed once it is proven to be a real PDF, so a failed
    download never leaves a corrupt manual behind.
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

    def _cleanup() -> None:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        with requests.get(url, headers=headers, stream=True, timeout=timeout) as response:
            response.raise_for_status()

            # Cheap pre-checks before touching the disk. Some servers label
            # PDFs text/html or octet-stream, so a mismatch only demotes us to
            # the magic-byte check below - it is not an outright rejection.
            content_type = response.headers.get("content-type", "").lower()

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return False, f"file is too large ({int(declared) // (1024 * 1024)} MB, cap {max_mb} MB)"

            save_path.parent.mkdir(parents=True, exist_ok=True)
            written = 0
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

        # Authoritative check: real PDFs start with %PDF. This catches HTML
        # error/login pages served under a .pdf URL just as well as a wrong
        # Content-Type header. (Windows cannot unlink an open file, so the
        # cleanup must happen after the read handle is closed.)
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


def download_pdf(url: str, save_path: Path, timeout: Optional[int] = None) -> bool:
    """Download a PDF from URL. True on success (reason via download_pdf_detail)."""
    ok, _ = download_pdf_detail(url, save_path, timeout=timeout)
    return ok


def download_manuals_for_device(
    brand: str,
    model: str,
    device_dir: Path,
    max_downloads: Optional[int] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> tuple[int, List[str], Optional[str]]:
    """Search and download manuals for a specific device.

    Args:
        brand: Device brand
        model: Device model
        device_dir: Directory to save PDFs
        max_downloads: Maximum number of PDFs to download (settings default)
        progress_callback: Receives ``{"type": "step", "message", "status"}`` dicts

    Returns:
        Tuple of (downloaded_count, list_of_filenames, error_message or None).
        The error message distinguishes the failure modes - search backend
        broken vs nothing found vs links that would not download - so the UI
        never shows a generic "not found" for an environment problem.

    ``progress_callback`` payloads stay dicts on purpose: the SSE endpoint in
    api/downloads.py normalizes them into frames, and raw pre-formatted SSE
    strings (as an older helper emitted) would be double-wrapped.
    """
    from homestew.config import settings  # noqa: PLC0415

    if max_downloads is None:
        max_downloads = settings.MANUAL_MAX_DOWNLOADS

    def _emit(message: str, status: str) -> None:
        if progress_callback:
            try:
                progress_callback({"type": "step", "message": message, "status": status})
            except Exception as exc:  # never let UI plumbing break the download
                logger.debug(f"progress_callback failed: {exc}")

    device_dir.mkdir(parents=True, exist_ok=True)

    _emit(f"Searching web engines for: {brand} {model} manual filetype:pdf", "searching")

    results, search_error = search_for_manuals(brand, model)

    if not results:
        error_detail = search_error or f"No manuals found for {brand} {model}"
        logger.info(f"No manuals found for {brand} {model}: {error_detail}")
        return 0, [], error_detail

    _emit(f"Found {len(results)} potential manual(s)", "found")

    downloaded: List[str] = []
    failures: List[tuple[str, str]] = []

    candidates = results[:max_downloads]
    for i, result in enumerate(candidates):
        filename = f"{brand}_{model}_manual_{i + 1}.pdf"
        save_path = device_dir / filename

        _emit(f"Downloading: {result.get('title', 'Unknown')} ({i + 1}/{len(candidates)})", "downloading")

        ok, reason = download_pdf_detail(result["url"], save_path)
        if ok:
            downloaded.append(filename)
            _emit(f"Downloaded: {filename}", "success")
        else:
            failures.append((result.get("title", filename), reason or "unknown error"))
            _emit(f"Failed to download: {result.get('title', filename)} ({reason})", "error")

    logger.info(f"Downloaded {len(downloaded)} manuals, failed {len(failures)}")

    # If nothing was downloaded but the search itself succeeded, explain why so
    # the UI can show a real reason instead of a generic "no manuals found".
    error_detail = None
    if not downloaded:
        if failures:
            first_reason = failures[0][1]
            error_detail = (
                f"Found {len(candidates)} manual link(s) but none could be downloaded "
                f"(first failure: {first_reason}). The links may be broken or blocked - "
                "try again or use different search terms."
            )
        else:
            error_detail = "No downloadable manuals were found for this brand/model."

    return len(downloaded), downloaded, error_detail
