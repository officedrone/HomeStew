"""Offline tests for the engine-chain search layer (web_search). No network."""
import base64
import logging
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
def recorded():
    return {}


@pytest.fixture
def fake_ddgs(monkeypatch, recorded):
    """Install a fake ddgs module; record constructor + text() call args.

    ``fake_ddgs.result`` is what the single configured backend returns; an
    Exception instance is raised instead of returned.
    """

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
            recorded.setdefault("backends", []).append(kwargs.get("backend"))
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


def _settings(monkeypatch, **overrides):
    from homestew.config import settings

    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


def _engines(*names_enabled):
    return [{"name": n, "enabled": e} for n, e in names_enabled]


# ---------------------------------------------------------------------------
# Bing scraper (built-in engine)
# ---------------------------------------------------------------------------

def _bing_ck(target: str) -> str:
    """Build a Bing /ck/a redirect URL the way Bing does (u=a1<b64>)."""
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc&u=a1{encoded}"


BING_HTML = f"""
<html><body>
<ol id="b_results">
  <li class="b_algo"><h2><a href="{_bing_ck('https://www.bowflex.com/ic4.pdf')}">Schwinn IC4 Manual</a></h2></li>
  <li class="b_algo"><h2><a href="{_bing_ck('https://example.com/guide.pdf')}">IC4 Guide</a></h2></li>
  <li class="b_algo"><h2><a href="/local/not-http">Relative</a></h2></li>
  <li class="b_no"><h2><a href="https://ignored.example/x.pdf">Not organic</a></h2></li>
</ol>
</body></html>
"""


def test_bing_parse_unwraps_and_filters():
    from homestew.services.search.web_search import _bing_parse

    results = _bing_parse(BING_HTML)
    assert [(r.title, r.url, r.engine) for r in results] == [
        ("Schwinn IC4 Manual", "https://www.bowflex.com/ic4.pdf", "bing"),
        ("IC4 Guide", "https://example.com/guide.pdf", "bing"),
    ]


def test_bing_parse_empty_on_garbage():
    from homestew.services.search.web_search import _bing_parse

    assert _bing_parse("") == []
    assert _bing_parse("<html><body>nothing here</body></html>") == []


def test_search_bing_uses_injected_fetcher_and_caps():
    from homestew.services.search.web_search import search_bing

    calls = {}

    def fetcher(query, timeout):
        calls["query"] = query
        calls["timeout"] = timeout
        return BING_HTML

    results = search_bing("Schwinn IC4 manual filetype:pdf", max_results=1, fetcher=fetcher)
    assert calls == {"query": "Schwinn IC4 manual filetype:pdf", "timeout": 10}
    assert len(results) == 1
    assert results[0].url == "https://www.bowflex.com/ic4.pdf"


# ---------------------------------------------------------------------------
# ddgs single-engine forwarding
# ---------------------------------------------------------------------------

def test_ddgs_engine_forwards_settings(fake_ddgs, recorded, monkeypatch):
    fake_ddgs.result = [
        {"title": "C62 Manual", "href": "https://www.nespresso.com/c62.pdf"},
        {"title": "Via url key", "url": "http://example.com/m.pdf"},
        {"title": "Relative junk", "href": "/local/thing.pdf"},
    ]
    _settings(
        monkeypatch,
        MANUAL_SEARCH_REGION="de-de",
        MANUAL_SEARCH_TIMEOUT=7,
        MANUAL_PROXY="http://proxy:3128",
    )
    from homestew.services.search.web_search import search_ddgs_engine

    results = search_ddgs_engine("q", engine="brave", max_results=5)
    assert [(r.title, r.url) for r in results] == [
        ("C62 Manual", "https://www.nespresso.com/c62.pdf"),
        ("Via url key", "http://example.com/m.pdf"),
    ]
    assert recorded["proxy"] == "http://proxy:3128"
    assert recorded["timeout"] == 7
    assert recorded["kwargs"]["backend"] == "brave"
    assert recorded["kwargs"]["region"] == "de-de"
    assert recorded["kwargs"]["max_results"] == 5


def test_ddgs_region_omitted_when_blank(fake_ddgs, recorded, monkeypatch):
    _settings(monkeypatch, MANUAL_SEARCH_REGION="")
    from homestew.services.search.web_search import search_ddgs_engine

    search_ddgs_engine("q", engine="yahoo")
    assert "region" not in recorded["kwargs"]


def test_ddgs_no_results_found_is_empty_not_error(fake_ddgs, monkeypatch):
    fake_ddgs.result = FakeDDGSException("No results found.")
    from homestew.services.search.web_search import search_ddgs_engine

    assert search_ddgs_engine("q", engine="yahoo") == []


# ---------------------------------------------------------------------------
# configured_engines / enabled_engines
# ---------------------------------------------------------------------------

def test_configured_engines_falls_back_to_defaults(monkeypatch):
    _settings(monkeypatch, MANUAL_SEARCH_ENGINES=[])
    from homestew.config import DEFAULT_MANUAL_SEARCH_ENGINES
    from homestew.services.search.web_search import configured_engines

    engines = configured_engines()
    assert [e["name"] for e in engines] == [e["name"] for e in DEFAULT_MANUAL_SEARCH_ENGINES]


def test_enabled_engines_respects_order_and_flags():
    from homestew.services.search.web_search import enabled_engines

    engines = _engines(("bing", True), ("brave", False), ("yahoo", True))
    assert enabled_engines(engines) == ["bing", "yahoo"]


# ---------------------------------------------------------------------------
# search_web engine chain (first hit wins, errors skipped)
# ---------------------------------------------------------------------------

def test_search_web_first_engine_hit_wins(monkeypatch):
    from homestew.services.search import web_search

    tried = []

    def fake_run(name, query, **kwargs):
        tried.append(name)
        if name == "bing":
            return []  # answered empty -> move on
        if name == "yahoo":
            return [web_search.WebResult(title="T", url="https://x/y.pdf", engine="yahoo")]
        raise AssertionError("should not reach later engines")

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    results = web_search.search_web("q", engines=_engines(("bing", True), ("yahoo", True), ("brave", True)))
    assert tried == ["bing", "yahoo"]
    assert results[0].engine == "yahoo"


def test_search_web_skips_erroring_engine_and_continues(monkeypatch):
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        if name == "bing":
            raise web_search.SearchUnavailableError("blocked")
        return [web_search.WebResult(title="T", url="https://x/y.pdf", engine=name)]

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    results = web_search.search_web("q", engines=_engines(("bing", True), ("brave", True)))
    assert results[0].engine == "brave"


def test_search_web_accept_predicate_skips_unusable_answer(monkeypatch):
    """An engine that answers with results the caller rejects must not win.

    Regression: Bing replies to `filetype:pdf` queries with HTTP 200 and a
    page of brand landing pages. Those results are non-empty but useless for
    manual lookup, so the chain has to continue to an engine that honors the
    operator instead of returning zero usable candidates.
    """
    from homestew.services.search import web_search

    tried = []

    def fake_run(name, query, **kwargs):
        tried.append(name)
        if name == "bing":
            return [web_search.WebResult(title="Buy it", url="https://brand.com/products", engine="bing")]
        return [web_search.WebResult(title="Manual", url="https://brand.com/manual.pdf", engine=name)]

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    results = web_search.search_web(
        "q",
        engines=_engines(("bing", True), ("yahoo", True), ("brave", True)),
        accept=lambda rs: any(r.url.endswith(".pdf") for r in rs),
    )
    assert tried == ["bing", "yahoo"]
    assert results[0].engine == "yahoo"


def test_search_web_accept_predicate_falls_back_to_first_non_empty(monkeypatch):
    """When no engine's answer is accepted, return something over nothing."""
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        return [web_search.WebResult(title="Landing", url=f"https://{name}.com/products", engine=name)]

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    results = web_search.search_web(
        "q",
        engines=_engines(("bing", True), ("yahoo", True)),
        accept=lambda rs: any(r.url.endswith(".pdf") for r in rs),
    )
    assert [r.engine for r in results] == ["bing"]


def test_search_web_all_empty_returns_empty_not_error(monkeypatch):
    from homestew.services.search import web_search

    monkeypatch.setattr(web_search, "_run_engine", lambda name, query, **kw: [])
    assert web_search.search_web("q", engines=_engines(("bing", True), ("brave", True))) == []


def test_search_web_all_error_raises(monkeypatch):
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        raise web_search.SearchUnavailableError(f"{name} down")

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    with pytest.raises(web_search.SearchUnavailableError) as exc:
        web_search.search_web("q", engines=_engines(("bing", True), ("brave", True)))
    message = str(exc.value)
    assert "bing down" in message and "brave down" in message


def test_search_web_no_enabled_engines_raises(monkeypatch):
    from homestew.services.search import web_search

    with pytest.raises(web_search.SearchUnavailableError) as exc:
        web_search.search_web("q", engines=_engines(("bing", False), ("brave", False)))
    assert "No search engines are enabled" in str(exc.value)


# ---------------------------------------------------------------------------
# search_web outcomes (per-engine walk report for the Fetch Manuals dialog)
# ---------------------------------------------------------------------------

def test_search_web_records_outcome_per_attempted_engine(monkeypatch):
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        if name == "bing":
            raise web_search.SearchUnavailableError("bing blocked")
        if name == "yahoo":
            return []
        return [web_search.WebResult(title="Manual", url="https://x/y.pdf", engine=name)]

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    outcomes: list = []
    results = web_search.search_web(
        "q",
        engines=_engines(("bing", True), ("yahoo", True), ("brave", True)),
        outcomes=outcomes,
    )
    assert results[0].engine == "brave"
    # Every engine touched by the chain is reported, including the failure.
    assert [(o["engine"], o["status"]) for o in outcomes] == [
        ("bing", web_search.ENGINE_STATUS_ERROR),
        ("yahoo", web_search.ENGINE_STATUS_EMPTY),
        ("brave", web_search.ENGINE_STATUS_ACCEPTED),
    ]
    assert "blocked" in outcomes[0]["error"]
    assert outcomes[2]["results"] == 1


def test_search_web_records_unaccepted_outcome(monkeypatch):
    """An engine whose answer the accept predicate rejects is reported as
    'unaccepted' - it answered, just not with anything usable."""
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        url = "https://brand.com/products" if name == "bing" else "https://brand.com/m.pdf"
        return [web_search.WebResult(title="T", url=url, engine=name)]

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    outcomes: list = []
    web_search.search_web(
        "q filetype:pdf",
        engines=_engines(("bing", True), ("yahoo", True)),
        accept=lambda rs: any(r.url.endswith(".pdf") for r in rs),
        outcomes=outcomes,
    )
    assert [(o["engine"], o["status"]) for o in outcomes] == [
        ("bing", web_search.ENGINE_STATUS_UNACCEPTED),
        ("yahoo", web_search.ENGINE_STATUS_ACCEPTED),
    ]


def test_search_web_emits_live_progress_without_polluting_outcomes(monkeypatch):
    """on_progress sees each engine start and finish; the transient 'searching'
    status never leaks into the caller's outcomes list (which is merged and
    persisted, where a phantom in-flight engine would be nonsense)."""
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        if name == "bing":
            raise web_search.SearchUnavailableError("bing blocked")
        return [web_search.WebResult(title="Manual", url="https://x/y.pdf", engine=name)]

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    events: list = []
    outcomes: list = []
    results = web_search.search_web(
        "q",
        engines=_engines(("bing", True), ("yahoo", True)),
        outcomes=outcomes,
        on_progress=events.append,
    )
    assert results[0].engine == "yahoo"
    assert [(e["phase"], e.get("engine"), e.get("status")) for e in events] == [
        ("start", "bing", None),
        ("end", "bing", web_search.ENGINE_STATUS_ERROR),
        ("start", "yahoo", None),
        ("end", "yahoo", web_search.ENGINE_STATUS_ACCEPTED),
    ]
    # Position within the chain travels with every event for "engine 2 of 6".
    assert (events[0]["index"], events[0]["total"]) == (1, 2)
    assert (events[3]["index"], events[3]["total"]) == (2, 2)
    assert all(o["status"] != web_search.ENGINE_STATUS_SEARCHING for o in outcomes)


def test_search_web_progress_callback_failure_does_not_break_search(monkeypatch):
    from homestew.services.search import web_search

    monkeypatch.setattr(
        web_search,
        "_run_engine",
        lambda name, query, **kwargs: [
            web_search.WebResult(title="Manual", url="https://x/y.pdf", engine=name)
        ],
    )

    def explode(record):
        raise RuntimeError("UI is gone")

    results = web_search.search_web(
        "q", engines=_engines(("bing", True)), on_progress=explode
    )
    assert len(results) == 1


def test_merge_engine_outcomes_collapses_per_engine_by_usefulness():
    from homestew.services.search import web_search as ws

    first = [
        ws.engine_outcome("bing", ws.ENGINE_STATUS_UNACCEPTED, results=5),
        ws.engine_outcome("yahoo", ws.ENGINE_STATUS_EMPTY),
    ]
    second = [
        ws.engine_outcome("bing", ws.ENGINE_STATUS_ERROR, error="timeout"),
        # Yahoo was empty on the strict pass but answered on the relaxed one:
        # the user must see its results, not a stale "empty".
        ws.engine_outcome("yahoo", ws.ENGINE_STATUS_ACCEPTED, results=3),
        ws.engine_outcome("brave", ws.ENGINE_STATUS_ACCEPTED, results=1),
    ]
    merged = ws.merge_engine_outcomes(first, second)
    # First-seen order is kept; accepted beats error, error beats unaccepted.
    assert [(o["engine"], o["status"]) for o in merged] == [
        ("bing", ws.ENGINE_STATUS_ERROR),
        ("yahoo", ws.ENGINE_STATUS_ACCEPTED),
        ("brave", ws.ENGINE_STATUS_ACCEPTED),
    ]
    assert merged[0]["error"] == "timeout"
    assert merged[1]["results"] == 3


def test_merge_engine_outcomes_keeps_highest_result_count():
    from homestew.services.search import web_search as ws

    merged = ws.merge_engine_outcomes(
        [ws.engine_outcome("yahoo", ws.ENGINE_STATUS_ACCEPTED, results=4)],
        [ws.engine_outcome("yahoo", ws.ENGINE_STATUS_ACCEPTED, results=2)],
    )
    assert len(merged) == 1 and merged[0]["results"] == 4


# ---------------------------------------------------------------------------
# search_engine_report (test UI): every engine, never raises
# ---------------------------------------------------------------------------

def test_report_tests_every_engine_independently(monkeypatch):
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        if name == "bing":
            return [web_search.WebResult(title="T", url="https://x/y.pdf", engine="bing")]
        raise web_search.SearchUnavailableError(f"{name} blocked")

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    monkeypatch.setattr(
        web_search, "engine_catalog", lambda: _engines(("bing", True), ("brave", True))
    )
    report = web_search.search_engine_report("q")
    assert [(r.engine, r.ok, len(r.results)) for r in report] == [
        ("bing", True, 1),
        ("brave", False, 0),
    ]
    assert "blocked" in report[1].error


def test_report_default_tests_all_supported_catalog_engines(monkeypatch):
    """No engine filter (the UI's "Test All Engines") must cover every
    supported catalog engine - including newly exposed ones not yet saved in
    settings - and must skip the ddgs-disabled engines (google, yandex)."""
    from homestew.services.search import web_search

    monkeypatch.setattr(web_search, "_run_engine", lambda name, query, **kw: [])
    tested = [r.engine for r in web_search.search_engine_report("q")]
    assert "grokipedia" in tested and "wikipedia" in tested
    assert "google" not in tested and "yandex" not in tested


def test_report_filters_to_requested_subset(monkeypatch):
    from homestew.services.search import web_search

    monkeypatch.setattr(web_search, "_run_engine", lambda name, query, **kw: [])
    monkeypatch.setattr(
        web_search, "engine_catalog", lambda: _engines(("bing", True), ("brave", True))
    )
    report = web_search.search_engine_report("q", engines=["brave"])
    assert [r.engine for r in report] == ["brave"]


def test_report_includes_requested_but_unconfigured(monkeypatch):
    from homestew.services.search import web_search

    monkeypatch.setattr(web_search, "_run_engine", lambda name, query, **kw: [])
    monkeypatch.setattr(web_search, "engine_catalog", lambda: _engines(("bing", True)))
    report = web_search.search_engine_report("q", engines=["mojeek"])
    assert [r.engine for r in report] == ["mojeek"]


# ---------------------------------------------------------------------------
# friendly error mapping (kept from the outage-hardening)
# ---------------------------------------------------------------------------

def test_invalid_impersonate_maps_to_rebuild_message(fake_ddgs, monkeypatch):
    fake_ddgs.result = FakeDDGSException('Invalid impersonate: "edge_131"')
    from homestew.services.search.web_search import SearchUnavailableError, search_ddgs_engine

    with pytest.raises(SearchUnavailableError) as exc:
        search_ddgs_engine("q", engine="brave")
    message = str(exc.value).lower()
    assert "not a network problem" in message
    assert "docker-compose build" in message


def test_ratelimit_maps_to_friendly_message(fake_ddgs, monkeypatch):
    fake_ddgs.result = FakeRatelimit("rate limit exceeded")
    from homestew.services.search.web_search import SearchUnavailableError, search_ddgs_engine

    with pytest.raises(SearchUnavailableError) as exc:
        search_ddgs_engine("q", engine="brave")
    assert "throttling" in str(exc.value).lower()


def test_capture_http_events_collects_primp_log_lines(monkeypatch):
    from homestew.services.search import web_search

    # ddgs turns a real 429 into a generic DDGSException("No results found.");
    # only primp's log line carries the true status. Reproduce that exactly.
    class FakeDDGS:
        def __init__(self, proxy=None, timeout=5, *, verify=True):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, **kwargs):
            # ddgs fires an auxiliary request first; the /search one is what
            # the user should be able to click.
            logging.getLogger("primp").info(
                "response: https://grokipedia.com/api/typeahead?query=x&limit=1 200"
            )
            logging.getLogger("primp").info(
                "response: https://search.brave.com/search?q=x&source=web 429"
            )
            raise FakeDDGSException("No results found.")

    module = types.ModuleType("ddgs")
    module.DDGS = FakeDDGS
    exceptions = types.ModuleType("ddgs.exceptions")
    exceptions.DDGSException = FakeDDGSException
    exceptions.RatelimitException = FakeRatelimit
    exceptions.TimeoutException = FakeTimeout
    module.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "ddgs", module)
    monkeypatch.setitem(sys.modules, "ddgs.exceptions", exceptions)

    with pytest.raises(web_search.SearchUnavailableError) as exc:
        web_search.search_ddgs_engine("q", engine="brave")
    assert exc.value.http_status == 429
    assert "throttling" in str(exc.value).lower()
    # The clickable URL is the engine's search page, not an auxiliary request.
    assert exc.value.url == "https://search.brave.com/search?q=x&source=web"


def test_capture_http_events_ignores_non_response_lines():
    from homestew.services.search.web_search import capture_http_events, worst_http_event

    with capture_http_events([]) as seen:
        logging.getLogger("primp").info("some unrelated message 500")
        logging.getLogger("primp").info("response: https://x/y 200")
        logging.getLogger("homestew.test").info("response: https://x/y 403")
    assert seen == [("https://x/y", 200)]
    assert worst_http_event(seen) == (200, "https://x/y")


def test_worst_http_event_prefers_failures():
    from homestew.services.search.web_search import worst_http_event

    assert worst_http_event([("a", 200), ("b", 429)]) == (429, "b")
    assert worst_http_event([("a", 200), ("b", 202)]) == (202, "b")
    assert worst_http_event([("a", 403), ("b", 500)]) == (500, "b")
    assert worst_http_event([("a", 200), ("b", 200)]) == (200, "b")
    assert worst_http_event([]) == (0, "")


def test_worst_http_event_prefers_search_page_url():
    from homestew.services.search.web_search import worst_http_event

    # Same status on several URLs -> pick the one that looks like a results
    # page the user can actually open and re-run.
    events = [
        ("https://grokipedia.com/api/typeahead?query=x&limit=1", 200),
        ("https://en.wikipedia.org/w/api.php?action=opensearch&q=x", 200),
        ("https://search.brave.com/search?q=x&source=web", 200),
    ]
    assert worst_http_event(events) == (200, "https://search.brave.com/search?q=x&source=web")


def test_report_uses_captured_status_for_unexpected_errors(monkeypatch):
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        logging.getLogger("primp").info("response: https://www.mojeek.com/search?q=x 403")
        raise RuntimeError("boom")

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    monkeypatch.setattr(
        web_search, "engine_catalog", lambda: _engines(("mojeek", True))
    )
    row = web_search.search_engine_report("q")[0]
    assert not row.ok
    assert row.http_status == 403
    assert "blocked" in row.error.lower()
    assert row.detail == "boom"
    assert row.url == "https://www.mojeek.com/search?q=x"


def test_report_success_path_reports_captured_url(monkeypatch):
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        logging.getLogger("primp").info(
            "response: https://www.bing.com/search?q=Shark+HZ+700C&count=20 200"
        )
        return [web_search.WebResult(title="T", url="https://x/y", engine=name)]

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    monkeypatch.setattr(
        web_search, "engine_catalog", lambda: _engines(("bing", True))
    )
    row = web_search.search_engine_report("q")[0]
    assert row.ok
    assert row.http_status == 200
    assert row.url == "https://www.bing.com/search?q=Shark+HZ+700C&count=20"
    assert row.note == ""


def test_report_empty_success_notes_actual_status(monkeypatch):
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        logging.getLogger("primp").info("response: https://x.example/search?q=y 200")
        return []

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    monkeypatch.setattr(
        web_search, "engine_catalog", lambda: _engines(("mojeek", True))
    )
    row = web_search.search_engine_report("q")[0]
    assert row.ok
    assert row.http_status == 200
    assert "HTTP 200" in row.note
    assert row.url == "https://x.example/search?q=y"


def test_extract_http_status_from_library_text():
    from homestew.services.search.web_search import extract_http_status

    assert extract_http_status("Bing returned HTTP 403") == 403
    assert extract_http_status("Error code: 429 - Too Many Requests") == 429
    assert extract_http_status("403 Forbidden") == 403
    assert extract_http_status("connection reset by peer") == 0
    assert extract_http_status("") == 0


def test_status_specific_guidance_messages():
    from homestew.services.search.web_search import status_guidance

    assert "API key" in status_guidance(401)
    assert "blocked" in status_guidance(403)
    assert "throttling" in status_guidance(429)
    assert "own server" in status_guidance(503)
    assert status_guidance(0) == ""


def test_ddgs_error_carries_http_status_and_detail(fake_ddgs, monkeypatch):
    fake_ddgs.result = FakeDDGSException("Error code: 429 - ratelimited")
    from homestew.services.search.web_search import SearchUnavailableError, search_ddgs_engine

    with pytest.raises(SearchUnavailableError) as exc:
        search_ddgs_engine("q", engine="brave")
    assert exc.value.http_status == 429
    assert "ratelimited" in exc.value.detail
    assert "throttling" in str(exc.value).lower()


def test_report_exposes_http_status_and_notes(monkeypatch):
    from homestew.services.search import web_search

    def fake_run(name, query, **kwargs):
        if name == "bing":
            return []  # answered, found nothing
        if name == "yahoo":
            raise web_search.SearchUnavailableError(
                web_search.status_guidance(429), http_status=429, detail="HTTP 429"
            )
        raise RuntimeError("connection reset")

    monkeypatch.setattr(web_search, "_run_engine", fake_run)
    monkeypatch.setattr(
        web_search,
        "engine_catalog",
        lambda: _engines(("bing", True), ("yahoo", True), ("mojeek", True)),
    )
    report = {r.engine: r for r in web_search.search_engine_report("q")}

    # Successful-but-empty: ok with a note, HTTP 200 chip.
    assert report["bing"].ok and report["bing"].http_status == 200
    assert "no results" in report["bing"].note

    # Typed error keeps its status + raw detail.
    assert not report["yahoo"].ok
    assert report["yahoo"].http_status == 429
    assert report["yahoo"].detail == "HTTP 429"
    assert "throttling" in report["yahoo"].error.lower()

    # Unexpected exception: status extracted from its text, raw kept as detail.
    assert not report["mojeek"].ok
    assert report["mojeek"].http_status == 0
    assert "connection reset" in report["mojeek"].detail


def test_bing_http_error_carries_status(monkeypatch):
    from homestew.services.search import web_search

    class FakeResp:
        status_code = 403
        text = ""

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def get(self, url):
            return FakeResp()

    primp = types.ModuleType("primp")
    primp.Client = FakeClient
    monkeypatch.setitem(sys.modules, "primp", primp)

    with pytest.raises(web_search.SearchUnavailableError) as exc:
        web_search._bing_fetch_html("q", 10)
    assert exc.value.http_status == 403
    assert exc.value.detail == "Bing returned HTTP 403"


def test_bing_fetch_passes_timeout_in_seconds(monkeypatch):
    # primp 2.x Client.timeout is in seconds; multiplying by 1000 (an old
    # bug) silently disabled the Bing timeout (~2.8 hours).
    from homestew.services.search import web_search

    class FakeResp:
        status_code = 200
        text = "<html></html>"

    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def get(self, url):
            return FakeResp()

    primp = types.ModuleType("primp")
    primp.Client = FakeClient
    monkeypatch.setitem(sys.modules, "primp", primp)

    assert web_search._bing_fetch_html("q", 10) == "<html></html>"
    assert seen["timeout"] == 10


def test_missing_library_reports_actionably(monkeypatch):
    # A None entry in sys.modules makes `from ddgs import DDGS` raise ImportError.
    monkeypatch.setitem(sys.modules, "ddgs", None)
    from homestew.services.search.web_search import SearchUnavailableError, search_ddgs_engine

    with pytest.raises(SearchUnavailableError) as exc:
        search_ddgs_engine("q", engine="brave")
    assert "not installed" in str(exc.value)
