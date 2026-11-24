"""Offline tests for manual_finder: query, redirect unwrapping, filtering, ranking."""
from base64 import urlsafe_b64encode
from urllib.parse import quote

import pytest


def test_build_query_targets_pdf_filetype():
    from homestew.services.search.manual_finder import build_query

    assert build_query("Nespresso", "PIXIE C62") == "Nespresso PIXIE C62 manual filetype:pdf"


def _capturing_search(record):
    """Fake search callable that records the query it was called with."""

    def search(query, **kwargs):
        record.append(query)
        return []

    return search


def test_custom_search_terms_replace_brand_model():
    from homestew.services.search.manual_finder import find_manual_links

    calls: list = []
    find_manual_links(
        "Nespresso", "PIXIE C62", search=_capturing_search(calls), search_terms="Breville BES870"
    )
    # Custom terms replace brand+model, but the PDF hints stay.
    assert calls == ["Breville BES870 manual filetype:pdf"]


def test_blank_custom_terms_fall_back_to_brand_model():
    from homestew.services.search.manual_finder import find_manual_links

    for blank in (None, "", "   "):
        calls: list = []
        find_manual_links(
            "Nespresso", "PIXIE C62", search=_capturing_search(calls), search_terms=blank
        )
        assert calls == ["Nespresso PIXIE C62 manual filetype:pdf"]


class TestUnwrapRedirect:
    def test_duckduckgo_l_redirect(self):
        from homestew.services.search.manual_finder import unwrap_redirect

        target = "https://www.nespresso.com/c62-manual.pdf"
        url = f"https://duckduckgo.com/l/?uddg={quote(target, safe='')}&rut=abc"
        assert unwrap_redirect(url) == target

    def test_google_url_redirect(self):
        from homestew.services.search.manual_finder import unwrap_redirect

        target = "https://example.com/manual.pdf"
        url = f"https://www.google.com/url?q={quote(target, safe='')}&sa=D"
        assert unwrap_redirect(url) == target

    def test_bing_ck_redirect_base64(self):
        from homestew.services.search.manual_finder import unwrap_redirect

        target = "https://example.com/manual.pdf"
        encoded = urlsafe_b64encode(target.encode()).decode().rstrip("=")
        url = f"https://www.bing.com/ck/a?!&&p=xyz&u=a1{encoded}"
        assert unwrap_redirect(url) == target

    def test_bing_aclick_ad_wrapper_base64(self):
        from homestew.services.search.manual_finder import unwrap_redirect

        target = "https://www.find-my-manual.com/register?utm_campaign=x"
        encoded = urlsafe_b64encode(quote(target, safe="").encode()).decode().rstrip("=")
        url = f"https://www.bing.com/aclick?ld=abc&u={encoded}&rlid=123"
        assert unwrap_redirect(url) == target

    def test_generic_url_param(self):
        from homestew.services.search.manual_finder import unwrap_redirect

        target = "https://example.com/m.pdf"
        url = f"https://tracker.example.org/redirect?url={quote(target, safe='')}"
        assert unwrap_redirect(url) == target

    def test_plain_url_unchanged_and_empty_safe(self):
        from homestew.services.search.manual_finder import unwrap_redirect

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
        from homestew.services.search.manual_finder import looks_like_pdf

        assert looks_like_pdf(url, title) is expected


def _fake_search(results):
    def search(query, **kwargs):
        search.calls = getattr(search, "calls", 0) + 1
        return list(results)

    return search


def test_filters_ranks_and_dedupes(monkeypatch):
    from homestew.services.search.manual_finder import find_manual_links

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


def test_relaxed_query_drops_filetype_operator():
    """Retry query must drop `filetype:pdf`, which some engines can't answer."""
    from homestew.services.search.manual_finder import relaxed_query

    assert relaxed_query("Lenovo", "Legion 5") == "Lenovo Legion 5 user guide pdf"
    assert "filetype" not in relaxed_query("Lenovo", "Legion 5")
    # Custom terms still replace brand+model in the relaxed form.
    assert relaxed_query("Lenovo", "Legion 5", "Ideapad Slim 3") == "Ideapad Slim 3 user guide pdf"


def test_find_manual_links_can_skip_filetype_operator():
    from homestew.services.search.manual_finder import find_manual_links

    calls: list = []
    find_manual_links(
        "Lenovo",
        "Legion 5",
        search=_capturing_search(calls),
        use_filetype_operator=False,
    )
    assert calls == ["Lenovo Legion 5 user guide pdf"]


def test_find_manual_links_threads_outcomes_to_search():
    """The outcomes list must reach the injected search callable so the walk
    report flows up to the API; a search that ignores it still works."""
    from homestew.services.search.manual_finder import find_manual_links

    seen: dict = {}

    def search(query, **kwargs):
        seen.update(kwargs)
        return []

    outcomes: list = [{"engine": "bing", "status": "empty", "results": 0, "error": ""}]
    find_manual_links("Nespresso", "PIXIE C62", search=search, outcomes=outcomes)
    assert seen.get("outcomes") is outcomes


def test_find_manual_links_omits_outcomes_kwarg_when_none():
    """Custom callables without an `outcomes` kwarg keep working (no pass)."""
    from homestew.services.search.manual_finder import find_manual_links

    seen: dict = {}

    def strict_search(query, *, max_results=None, accept=None):
        seen["ok"] = True
        return []

    find_manual_links("Nespresso", "PIXIE C62", search=strict_search)
    assert seen.get("ok") is True


def test_downloader_retries_with_relaxed_query(monkeypatch):
    """Empty first pass must retry without the operator (Yahoo flakiness)."""
    from homestew.services.search import downloader
    from homestew.services.search import manual_finder

    monkeypatch.setattr(downloader.time, "sleep", lambda *_: None)

    queries: list = []

    def fake_find(brand, model, **kwargs):
        queries.append(manual_finder.build_query(brand, model)
                       if kwargs.get("use_filetype_operator", True)
                       else manual_finder.relaxed_query(brand, model))
        if kwargs.get("use_filetype_operator", True):
            return [], None  # operator found nothing
        return [manual_finder.ManualCandidate(title="M", url="https://x.com/m.pdf", engine="yahoo")], None

    monkeypatch.setattr(manual_finder, "find_manual_links", fake_find)

    results, error = downloader.search_for_manuals("Lenovo", "Legion 5")
    assert error is None
    assert [r["url"] for r in results] == ["https://x.com/m.pdf"]
    assert len(queries) == 2 and "filetype" not in queries[1]


def test_find_manual_links_passes_pdf_accept_predicate():
    """The search chain must be told to skip engines with no PDF in the answer.

    Regression: Bing answers `filetype:pdf` queries with brand landing pages
    (HTTP 200, zero PDFs). Passing no predicate let that answer end the search,
    so every candidate was filtered out and the dialog showed nothing.
    """
    from homestew.services.search.manual_finder import find_manual_links

    captured = {}

    def spy_search(query, **kwargs):
        captured.update(kwargs)
        return [{"title": "Manual", "url": "https://brand.com/m.pdf"}]

    find_manual_links("Nespresso", "C62", search=spy_search)

    accept = captured.get("accept")
    assert callable(accept)
    assert accept([{"title": "Buy now", "url": "https://shop.example.com/nespresso"}]) is False
    assert accept([{"title": "Manual", "url": "https://brand.com/m.pdf"}]) is True
    # A redirect-wrapped PDF still counts, and a filtered-out page does not.
    assert accept(
        [{"title": "Manual", "url": "https://duckduckgo.com/l/?uddg=" + quote("https://x.com/m.pdf", safe="")}]
    ) is True
    assert accept([{"title": "Video review pdf", "url": "https://youtube.com/watch?v=1.pdf"}]) is False


def test_unresolved_engine_wrapper_is_dropped():
    """A Bing ad link we cannot decode must not be offered as a manual."""
    from homestew.services.search.manual_finder import find_manual_links

    raw = [
        {"title": "Manual", "url": "https://www.bing.com/aclick?ld=abc&rlid=123"},
        {"title": "Real manual", "url": "https://brand.com/m.pdf"},
    ]
    candidates, error = find_manual_links("Brand", "X1", search=_fake_search(raw))
    assert error is None
    assert [c.url for c in candidates] == ["https://brand.com/m.pdf"]


def test_search_error_is_reported_not_raised(monkeypatch):
    from homestew.services.search.web_search import SearchUnavailableError

    def boom(query, **kwargs):
        raise SearchUnavailableError("Search engines are rate-limiting requests.")

    from homestew.services.search.manual_finder import find_manual_links

    candidates, error = find_manual_links("Nespresso", "C62", search=boom)
    assert candidates == []
    assert "rate-limiting" in error


def test_unexpected_search_error_is_normalized():
    def boom(query, **kwargs):
        raise RuntimeError('Invalid impersonate: "edge_131"')

    from homestew.services.search.manual_finder import find_manual_links

    candidates, error = find_manual_links("Nespresso", "C62", search=boom)
    assert candidates == []
    # The dependency-drift signature must map to the rebuild advice.
    assert "not a network problem" in error


def test_empty_results_is_not_an_error():
    from homestew.services.search.manual_finder import find_manual_links

    candidates, error = find_manual_links("Nespresso", "C62", search=_fake_search([]))
    assert candidates == []
    assert error is None
