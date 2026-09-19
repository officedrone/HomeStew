"""Offline tests for the ddgs wrapper (web_search). No network access."""
import sys
import types

import pytest


class FakeDDGSException(Exception):
    pass


class FakeRatelimit(FakeDDGSException):
    pass


class FakeTimeout(FakeDDGSException):
    pass


@pytest.fixture
def fake_ddgs(monkeypatch, recorded):
    """Install a fake ddgs module; record constructor + text() call args."""

    class FakeDDGS:
        def __init__(self, proxy=None, timeout=5, *, verify=True):
            recorded["proxy"] = proxy
            recorded["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, **kwargs):
            recorded["query"] = query
            recorded["kwargs"] = kwargs
            result = fake_ddgs.result
            if isinstance(result, Exception):
                raise result
            return result

    module = types.ModuleType("ddgs")
    module.DDGS = FakeDDGS
    exceptions = types.ModuleType("ddgs.exceptions")
    exceptions.DDGSException = FakeDDGSException
    exceptions.RatelimitException = FakeRatelimit
    exceptions.TimeoutException = FakeTimeout
    module.exceptions = exceptions

    monkeypatch.setitem(sys.modules, "ddgs", module)
    monkeypatch.setitem(sys.modules, "ddgs.exceptions", exceptions)
    fake_ddgs.result = []
    return fake_ddgs


@pytest.fixture
def recorded():
    return {}


def _settings(monkeypatch, **overrides):
    from homestew.config import settings

    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


def test_normalizes_results_and_drops_non_http(fake_ddgs, recorded, monkeypatch):
    fake_ddgs.result = [
        {"title": "C62 Manual", "href": "https://www.nespresso.com/c62.pdf"},
        {"title": "Via url key", "url": "http://example.com/m.pdf"},
        {"title": "Relative junk", "href": "/local/thing.pdf"},
    ]
    from homestew.services.web_search import search_web

    results = search_web("q", max_results=5)
    assert [(r.title, r.url) for r in results] == [
        ("C62 Manual", "https://www.nespresso.com/c62.pdf"),
        ("Via url key", "http://example.com/m.pdf"),
    ]
    assert recorded["kwargs"]["max_results"] == 5


def test_settings_are_forwarded(fake_ddgs, recorded, monkeypatch):
    _settings(
        monkeypatch,
        MANUAL_SEARCH_BACKENDS="bing,brave",
        MANUAL_SEARCH_REGION="de-de",
        MANUAL_SEARCH_TIMEOUT=7,
        MANUAL_PROXY="http://proxy:3128",
    )
    from homestew.services.web_search import search_web

    search_web("q")
    assert recorded["proxy"] == "http://proxy:3128"
    assert recorded["timeout"] == 7
    assert recorded["kwargs"]["backend"] == "bing,brave"
    assert recorded["kwargs"]["region"] == "de-de"


def test_empty_backends_config_means_auto(fake_ddgs, recorded, monkeypatch):
    _settings(monkeypatch, MANUAL_SEARCH_BACKENDS="", MANUAL_SEARCH_REGION="")
    from homestew.services.web_search import search_web

    search_web("q")
    assert recorded["kwargs"]["backend"] == "auto"
    assert "region" not in recorded["kwargs"]  # library default when unset


def test_no_results_found_is_empty_not_error(fake_ddgs, monkeypatch):
    fake_ddgs.result = FakeDDGSException("No results found.")
    from homestew.services.web_search import search_web

    assert search_web("q") == []


def test_ratelimit_maps_to_friendly_message(fake_ddgs, monkeypatch):
    fake_ddgs.result = FakeRatelimit("rate limit exceeded")
    from homestew.services.web_search import SearchUnavailableError, search_web

    with pytest.raises(SearchUnavailableError) as exc:
        search_web("q")
    assert "rate-limiting" in str(exc.value).lower()


def test_invalid_impersonate_maps_to_rebuild_message(fake_ddgs, monkeypatch):
    # The Sep 2026 outage signature: primp dropped a browser profile the
    # pinned library hardcoded. Must tell users to rebuild, not "no results".
    fake_ddgs.result = FakeDDGSException('Invalid impersonate: "edge_131"')
    from homestew.services.web_search import SearchUnavailableError, search_web

    with pytest.raises(SearchUnavailableError) as exc:
        search_web("q")
    message = str(exc.value).lower()
    assert "not a network problem" in message
    assert "docker-compose build" in message


def test_missing_library_reports_actionably(monkeypatch):
    # A None entry in sys.modules makes `from ddgs import DDGS` raise ImportError.
    monkeypatch.setitem(sys.modules, "ddgs", None)
    from homestew.services.web_search import SearchUnavailableError, search_web

    with pytest.raises(SearchUnavailableError) as exc:
        search_web("q")
    assert "not installed" in str(exc.value)
