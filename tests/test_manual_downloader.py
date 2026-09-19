"""Offline tests for manual_downloader: validation, size caps, progress contract."""
from pathlib import Path

import pytest
import requests

VALID_PDF = b"%PDF-1.4\n" + b"x" * 2000


class FakeResponse:
    """Minimal stand-in for a streaming requests.Response."""

    def __init__(self, chunks, headers=None, status_code=200):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)

    def iter_content(self, chunk_size=8192):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_get(monkeypatch, response_or_exc):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if isinstance(response_or_exc, Exception):
            raise response_or_exc
        return response_or_exc

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def test_valid_pdf_is_saved_atomically(tmp_path, monkeypatch):
    _patch_get(monkeypatch, FakeResponse([VALID_PDF], {"content-type": "application/pdf"}))
    from homestew.services.manual_downloader import download_pdf_detail

    save = tmp_path / "manual.pdf"
    ok, reason = download_pdf_detail("https://x.com/m.pdf", save)
    assert ok and reason is None
    assert save.read_bytes().startswith(b"%PDF")
    assert not list(tmp_path.glob("*.part"))  # temp file renamed, never left behind


def test_html_served_as_pdf_is_rejected(tmp_path, monkeypatch):
    html = b"<html><body>Login required</body></html>" + b" " * 2000
    _patch_get(monkeypatch, FakeResponse([html], {"content-type": "text/html"}))
    from homestew.services.manual_downloader import download_pdf_detail

    save = tmp_path / "manual.pdf"
    ok, reason = download_pdf_detail("https://x.com/manual.pdf", save)
    assert not ok
    assert "not a valid PDF" in reason
    assert not save.exists() and not list(tmp_path.glob("*.part"))


def test_declared_size_over_cap_rejected_before_writing(tmp_path, monkeypatch):
    _patch_get(
        monkeypatch,
        FakeResponse([], {"content-type": "application/pdf", "content-length": str(50 * 1024 * 1024)}),
    )
    from homestew.services.manual_downloader import download_pdf_detail

    ok, reason = download_pdf_detail("https://x.com/m.pdf", tmp_path / "m.pdf", max_mb=25)
    assert not ok and "too large" in reason


def test_streamed_size_over_cap_cleans_partial(tmp_path, monkeypatch):
    _patch_get(
        monkeypatch,
        FakeResponse([b"%PDF-1.4\n" + b"x" * (30 * 1024 * 1024)], {"content-type": "application/pdf"}),
    )
    from homestew.services.manual_downloader import download_pdf_detail

    save = tmp_path / "m.pdf"
    ok, reason = download_pdf_detail("https://x.com/m.pdf", save, max_mb=5)
    assert not ok and "size cap" in reason
    assert not save.exists() and not list(tmp_path.glob("*.part"))


def test_network_error_reports_reason(tmp_path, monkeypatch):
    _patch_get(monkeypatch, requests.exceptions.ConnectionError("dns failure"))
    from homestew.services.manual_downloader import download_pdf_detail

    ok, reason = download_pdf_detail("https://x.com/m.pdf", tmp_path / "m.pdf")
    assert not ok and "network error" in reason


def _fake_finder(results, error=None):
    def finder(brand, model, **kwargs):
        return list(results), error

    return finder


def test_device_flow_progress_and_contract(tmp_path, monkeypatch):
    from homestew.services import manual_finder, manual_downloader

    candidates = [
        manual_finder.ManualCandidate("Official", "https://nespresso.com/c62.pdf", "bing"),
        manual_finder.ManualCandidate("Broken", "https://dead.example/x.pdf", "brave"),
    ]
    monkeypatch.setattr(manual_finder, "find_manual_links", _fake_finder(candidates))

    responses = {
        "https://nespresso.com/c62.pdf": FakeResponse([VALID_PDF], {"content-type": "application/pdf"}),
        "https://dead.example/x.pdf": requests.exceptions.ConnectionError("refused"),
    }

    def fake_get(url, **kwargs):
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(requests, "get", fake_get)

    events = []
    count, filenames, error = manual_downloader.download_manuals_for_device(
        "Nespresso", "C62", tmp_path / "manuals", max_downloads=5, progress_callback=events.append
    )

    assert (count, filenames, error) == (1, ["Nespresso_C62_manual_1.pdf"], None)
    statuses = [e["status"] for e in events]
    # searching -> found -> per-candidate downloading/success|error; payloads are dicts.
    assert statuses[0] == "searching" and statuses[1] == "found"
    assert "success" in statuses and "error" in statuses
    assert all(isinstance(e, dict) and e["type"] == "step" for e in events)


def test_device_flow_search_error_propagates(tmp_path, monkeypatch):
    from homestew.services import manual_finder, manual_downloader

    monkeypatch.setattr(
        manual_finder,
        "find_manual_links",
        _fake_finder([], error="Search engines are rate-limiting requests. Wait a minute or two and try again."),
    )
    # The retry inside search_for_manuals must not actually sleep in tests.
    monkeypatch.setattr(manual_downloader.time, "sleep", lambda s: None)

    events = []
    count, filenames, error = manual_downloader.download_manuals_for_device(
        "Nespresso", "C62", tmp_path / "manuals", progress_callback=events.append
    )
    assert count == 0 and filenames == []
    assert "rate-limiting" in error


def test_device_flow_all_downloads_failed_explains_first_reason(tmp_path, monkeypatch):
    from homestew.services import manual_finder, manual_downloader

    candidates = [manual_finder.ManualCandidate("M", "https://x.com/m.pdf", "bing")]
    monkeypatch.setattr(manual_finder, "find_manual_links", _fake_finder(candidates))
    _patch_get(monkeypatch, FakeResponse([b"<html>nope</html>" + b" " * 2000], {"content-type": "text/html"}))

    count, filenames, error = manual_downloader.download_manuals_for_device(
        "Nespresso", "C62", tmp_path / "manuals"
    )
    assert count == 0 and filenames == []
    # Distinguishes "links found but downloads failed" from "nothing found".
    assert "none could be downloaded" in error and "not a valid PDF" in error
