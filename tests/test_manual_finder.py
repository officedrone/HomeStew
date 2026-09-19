"""Offline tests for manual_finder: query, redirect unwrapping, filtering, ranking."""
from base64 import urlsafe_b64encode
from urllib.parse import quote

import pytest


def test_build_query_targets_pdf_filetype():
    from homestew.services.manual_finder import build_query

    assert build_query("Nespresso", "PIXIE C62") == "Nespresso PIXIE C62 manual filetype:pdf"


class TestUnwrapRedirect:
    def test_duckduckgo_l_redirect(self):
        from homestew.services.manual_finder import unwrap_redirect

        target = "https://www.nespresso.com/c62-manual.pdf"
        url = f"https://duckduckgo.com/l/?uddg={quote(target, safe='')}&rut=abc"
        assert unwrap_redirect(url) == target

    def test_google_url_redirect(self):
        from homestew.services.manual_finder import unwrap_redirect

        target = "https://example.com/manual.pdf"
        url = f"https://www.google.com/url?q={quote(target, safe='')}&sa=D"
        assert unwrap_redirect(url) == target

    def test_bing_ck_redirect_base64(self):
        from homestew.services.manual_finder import unwrap_redirect

        target = "https://example.com/manual.pdf"
        encoded = urlsafe_b64encode(target.encode()).decode().rstrip("=")
        url = f"https://www.bing.com/ck/a?!&&p=xyz&u=a1{encoded}"
        assert unwrap_redirect(url) == target

    def test_generic_url_param(self):
        from homestew.services.manual_finder import unwrap_redirect

        target = "https://example.com/m.pdf"
        url = f"https://tracker.example.org/redirect?url={quote(target, safe='')}"
        assert unwrap_redirect(url) == target

    def test_plain_url_unchanged_and_empty_safe(self):
        from homestew.services.manual_finder import unwrap_redirect

        plain = "https://www.nespresso.com/c62.pdf"
        assert unwrap_redirect(plain) == plain
        assert unwrap_redirect("") == ""


class TestLooksLikePdf:
    @pytest.mark.parametrize(
        "url,title,expected",
        [
            ("https://x.com/a.pdf", "", True),
            ("https://x.com/a.PDF?v=2", "", True),
            ("https://x.com/download?id=9", "User manual (PDF)", True),
            ("https://x.com/page", "Product page", False),
        ],
    )
    def test_cases(self, url, title, expected):
        from homestew.services.manual_finder import looks_like_pdf

        assert looks_like_pdf(url, title) is expected


def _fake_search(results):
    def search(query, **kwargs):
        search.calls = getattr(search, "calls", 0) + 1
        return list(results)

    return search


def test_filters_ranks_and_dedupes(monkeypatch):
    from homestew.services.manual_finder import find_manual_links

    raw = [
        # Manufacturer domain wins even without .pdf in path.
        {"title": "PIXIE C62 - official", "url": "https://www.nespresso.com/docs/c62-manual.pdf"},
        # Same document offered by a second engine -> deduped.
        {"title": "PIXIE C62 - official", "url": "https://duckduckgo.com/l/?uddg=" + quote("https://www.nespresso.com/docs/c62-manual.pdf", safe="")},
        # Aggregator: mid tier.
        {"title": "Nespresso manual", "url": "https://manualslib.com/doc/1234"},
        # Random site with .pdf: lower than manufacturer.
        {"title": "Uploaded manual", "url": "https://some-blog.example.org/uploads/c62.pdf"},
        # HTML page, no PDF signal -> filtered out.
        {"title": "Buy a Nespresso", "url": "https://shop.example.com/nespresso"},
        # Video result -> filtered by negative terms even if it says pdf.
        {"title": "Video review pdf", "url": "https://youtube.com/watch?v=1.pdf"},
    ]

    candidates, error = find_manual_links("Nespresso", "PIXIE C62", search=_fake_search(raw))
    assert error is None
    urls = [c.url for c in candidates]
    # Manufacturer first; duplicate collapsed; non-PDF and video gone.
    assert urls[0] == "https://www.nespresso.com/docs/c62-manual.pdf"
    assert len(urls) == len(set(urls))
    assert all("youtube" not in u and "shop.example" not in u for u in urls)


def test_search_error_is_reported_not_raised(monkeypatch):
    from homestew.services.web_search import SearchUnavailableError

    def boom(query, **kwargs):
        raise SearchUnavailableError("Search engines are rate-limiting requests.")

    from homestew.services.manual_finder import find_manual_links

    candidates, error = find_manual_links("Nespresso", "C62", search=boom)
    assert candidates == []
    assert "rate-limiting" in error


def test_unexpected_search_error_is_normalized():
    def boom(query, **kwargs):
        raise RuntimeError('Invalid impersonate: "edge_131"')

    from homestew.services.manual_finder import find_manual_links

    candidates, error = find_manual_links("Nespresso", "C62", search=boom)
    assert candidates == []
    # The dependency-drift signature must map to the rebuild advice.
    assert "not a network problem" in error


def test_empty_results_is_not_an_error():
    from homestew.services.manual_finder import find_manual_links

    candidates, error = find_manual_links("Nespresso", "C62", search=_fake_search([]))
    assert candidates == []
    assert error is None
