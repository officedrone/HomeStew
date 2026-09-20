"""Offline tests for downloader: PDF validation, size caps, candidate metadata."""
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
    from homestew.services.downloader import download_pdf_detail

    save = tmp_path / "manual.pdf"
    ok, reason = download_pdf_detail("https://x.com/m.pdf", save)
    assert ok and reason is None
    assert save.read_bytes().startswith(b"%PDF")
    assert not list(tmp_path.glob("*.part"))  # temp file renamed, never left behind


def test_html_served_as_pdf_is_rejected(tmp_path, monkeypatch):
    html = b"<html><body>Login required</body></html>" + b" " * 2000
    _patch_get(monkeypatch, FakeResponse([html], {"content-type": "text/html"}))
    from homestew.services.downloader import download_pdf_detail

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
    from homestew.services.downloader import download_pdf_detail

    ok, reason = download_pdf_detail("https://x.com/m.pdf", tmp_path / "m.pdf", max_mb=25)
    assert not ok and "too large" in reason


def test_streamed_size_over_cap_cleans_partial(tmp_path, monkeypatch):
    _patch_get(
        monkeypatch,
        FakeResponse([b"%PDF-1.4\n" + b"x" * (30 * 1024 * 1024)], {"content-type": "application/pdf"}),
    )
    from homestew.services.downloader import download_pdf_detail

    save = tmp_path / "m.pdf"
    ok, reason = download_pdf_detail("https://x.com/m.pdf", save, max_mb=5)
    assert not ok and "size cap" in reason
    assert not save.exists() and not list(tmp_path.glob("*.part"))


def test_network_error_reports_reason(tmp_path, monkeypatch):
    _patch_get(monkeypatch, requests.exceptions.ConnectionError("dns failure"))
    from homestew.services.downloader import download_pdf_detail

    ok, reason = download_pdf_detail("https://x.com/m.pdf", tmp_path / "m.pdf")
    assert not ok and "network error" in reason


class TestDomainOf:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.nespresso.com/doc/c62.pdf", "nespresso.com"),
            ("https://manualslib.com/m/1234", "manualslib.com"),
            ("https://downloads.support.bosch-home.com/a.pdf", "bosch-home.com"),
            ("http://cdn.example.co.uk/manual.pdf", "example.co.uk"),
            ("https://sub.domain.co.uk/x.pdf", "domain.co.uk"),
            ("not a url", ""),
        ],
    )
    def test_top_level_domain(self, url, expected):
        from homestew.services.downloader import domain_of

        assert domain_of(url) == expected


class TestDisplayName:
    @pytest.mark.parametrize(
        "url,title,expected",
        [
            ("https://x.com/files/PIXIE_C62.pdf", "", "PIXIE_C62.pdf"),
            ("https://x.com/d/UM%20C62%20EN.pdf?v=2", "", "UM C62 EN.pdf"),
            # Extension-less download endpoint falls back to the title; a
            # title that already says PDF is kept as-is.
            ("https://x.com/download?id=9", "User manual (PDF)", "User manual (PDF)"),
            ("https://x.com/download?id=9", "Product page", "Product page.pdf"),
        ],
    )
    def test_pdf_name_for_table(self, url, title, expected):
        from homestew.services.downloader import display_name_for

        assert display_name_for(url, title) == expected


def test_enrich_candidates_flags_existing_urls():
    from homestew.services.downloader import enrich_candidates

    results = [
        {"title": "Official", "url": "https://nespresso.com/c62.pdf", "source": "bing"},
        {"title": "Other", "url": "https://manualslib.com/x/1", "source": "brave"},
    ]
    enriched = enrich_candidates(results, existing_urls={"https://nespresso.com/c62.pdf"})

    assert [c["name"] for c in enriched] == ["c62.pdf", "1.pdf"]
    assert [c["domain"] for c in enriched] == ["nespresso.com", "manualslib.com"]
    assert [c["already_downloaded"] for c in enriched] == [True, False]


def test_stored_filename_prefers_pdf_basename():
    from homestew.services.downloader import _stored_filename

    assert _stored_filename("https://x.com/a/UM-C62.pdf", "Nespresso", "PIXIE C62") == "UM-C62.pdf"
    # No usable basename -> deterministic brand/model name.
    assert _stored_filename("https://x.com/d?id=9", "Sony", "ULT Field 300") == "Sony_ULT_Field_300_manual.pdf"


def test_unique_filename_avoids_overwrite(tmp_path):
    from homestew.services.downloader import _unique_filename

    (tmp_path / "manual.pdf").write_bytes(b"x")
    assert _unique_filename(tmp_path, "manual.pdf") == "manual_2.pdf"
    assert _unique_filename(tmp_path, "other.pdf") == "other.pdf"


def test_manual_exists_error_carries_filename():
    from homestew.services.downloader import ManualExistsError

    exc = ManualExistsError("c62.pdf")
    assert exc.filename == "c62.pdf"
    assert "already downloaded" in str(exc)
