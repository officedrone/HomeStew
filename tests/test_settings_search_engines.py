"""Tests for the online-search-engine settings (Settings > Search).

Covers the engine list that replaced the old MANUAL_SEARCH_BACKENDS string:

* ``GET /api/settings`` returns the effective engine order (built-in defaults
  when nothing usable is stored) as ``manual_search_engines``.
* ``PUT /api/settings`` round-trips a custom order/enablement, sanitizes it
  (lowercase, dedupe first-wins, junk names dropped), and persists it to
  settings.json in DATA_DIR so it survives a restart.
* ``POST /api/settings/search-test`` reports per-engine outcomes without ever
  raising for engine failures; the blocking search runs are monkeypatched -
  no network access here.

Fixture pattern copied from test_search_index_jobs.py: signed-in TestClient
on a throwaway DATA_DIR (lifespan runs init_db).

Run from the repo root:  pytest tests/test_settings_search_engines.py -q
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from homestew import config as cfg
from homestew.db import init_db
from homestew.services import auth, web_search


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated data dir + engine list restored after each test."""
    monkeypatch.setattr(cfg.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg.settings, "DEVICES_DIR", tmp_path / "devices")
    monkeypatch.setattr(cfg.settings, "PASSWORD_HASH", "")
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})
    orig_engines = [dict(e) for e in cfg.settings.MANUAL_SEARCH_ENGINES]
    yield tmp_path
    cfg.settings.MANUAL_SEARCH_ENGINES = orig_engines
    auth._failures.clear()
    auth._pw_cache.update({"mtime": None, "hash": ""})


@pytest.fixture()
def db_env(env):
    asyncio.run(init_db())
    return env


@pytest.fixture()
def client(db_env):
    """Signed-in TestClient (forced account creation unlocks the API)."""
    from homestew.main import app

    with TestClient(app) as c:
        r = c.post("/api/auth/setup", json={
            "password": "engine-test-password",
            "confirm_password": "engine-test-password",
        })
        assert r.status_code == 201
        yield c


def test_get_settings_returns_default_engine_order(client):
    engines = client.get("/api/settings").json()["manual_search_engines"]
    names = [e["name"] for e in engines]
    # Bing first (built-in scraper is the most reliable engine), then the
    # default set, then catalog-only engines ddgs ships disabled.
    assert names[0] == "bing"
    assert names[: len(cfg.DEFAULT_MANUAL_SEARCH_ENGINES)] == [
        e["name"] for e in cfg.DEFAULT_MANUAL_SEARCH_ENGINES
    ]
    # Newly exposed working engines are present but off by default; the
    # ddgs-disabled engines appear last, unsupported and disabled.
    by_name = {e["name"]: e for e in engines}
    assert by_name["grokipedia"]["enabled"] is False
    assert by_name["wikipedia"]["enabled"] is False
    for name in ("google", "yandex"):
        assert by_name[name]["supported"] is False
        assert by_name[name]["enabled"] is False
        assert "ddgs" in by_name[name]["note"].lower()
    # The curated default engines are all enabled.
    assert all(by_name[e["name"]]["enabled"] for e in cfg.DEFAULT_MANUAL_SEARCH_ENGINES
               if e["enabled"])


def test_get_settings_catalog_includes_all_ddgs_text_engines(client):
    names = {e["name"] for e in client.get("/api/settings").json()["manual_search_engines"]}
    assert set(cfg.SEARCH_ENGINE_CATALOG) <= names


def test_put_cannot_enable_unsupported_engine(client):
    # google is ddgs-disabled; enabling it would make ddgs silently run every
    # engine, so the server forces it off even though the name is well-formed.
    r = client.put("/api/settings", json={"manual_search_engines": [
        {"name": "google", "enabled": True},
        {"name": "brave", "enabled": True},
    ]})
    assert r.status_code == 200
    by_name = {e["name"]: e for e in r.json()["manual_search_engines"]}
    assert by_name["google"]["enabled"] is False
    assert by_name["google"]["supported"] is False
    assert web_search.enabled_engines() == ["brave"]


def test_put_engines_roundtrips_and_persists(client, env):
    payload = {
        "manual_search_engines": [
            {"name": "yahoo", "enabled": True},
            {"name": "bing", "enabled": False},
            {"name": "brave", "enabled": True},
        ]
    }
    r = client.put("/api/settings", json=payload)
    assert r.status_code == 200
    # The configured engines lead the response in order; catalog-only engines
    # (google, mojeek, ...) are appended after them as disabled rows.
    engines = r.json()["manual_search_engines"]
    assert [(e["name"], e["enabled"]) for e in engines[:3]] == [
        ("yahoo", True), ("bing", False), ("brave", True),
    ]
    # Live singleton updated immediately (searches use it without restart).
    assert web_search.enabled_engines() == ["yahoo", "brave"]
    # Persisted to settings.json for the next container start.
    saved = json.loads((env / "settings.json").read_text(encoding="utf-8"))
    assert [e["name"] for e in saved["MANUAL_SEARCH_ENGINES"]] == ["yahoo", "bing", "brave"]


def test_put_engines_sanitizes_junk(client):
    payload = {
        "manual_search_engines": [
            {"name": " Bing "},                  # case + whitespace normalized
            {"name": "bing", "enabled": False},  # duplicate -> first wins
            {"name": "not a name!"},             # invalid pattern -> dropped
            {"name": "mojeek", "enabled": False},
        ]
    }
    r = client.put("/api/settings", json=payload)
    assert r.status_code == 200
    engines = r.json()["manual_search_engines"]
    # Configured rows lead (catalog-only engines are appended after them).
    assert [(e["name"], e["enabled"]) for e in engines[:2]] == [
        ("bing", True),      # first entry wins; `enabled` defaulted to True
        ("mojeek", False),
    ]
    assert web_search.enabled_engines() == ["bing"]


# ---------------------------------------------------------------------------
# GET /api/downloads/{id}/search - per-engine walk report (`engines`)
# ---------------------------------------------------------------------------

def test_manual_search_response_reports_engines(client, monkeypatch):
    """The search endpoint returns one merged row per engine the chain
    walked (including errored/empty ones) and tags each candidate with the
    engine it came from."""
    from homestew.services import web_search as ws

    def fake_run(name, query, **kwargs):
        if name == "bing":
            raise ws.SearchUnavailableError("bing blocked")
        if name == "yahoo":
            # Answers, but with a landing page the accept predicate rejects.
            return [ws.WebResult(title="Buy it", url="https://brand.com/products", engine="yahoo")]
        return [ws.WebResult(title="C62 Manual PDF", url="https://x.com/c62.pdf", engine="brave")]

    monkeypatch.setattr(ws, "_run_engine", fake_run)
    monkeypatch.setattr(ws, "configured_engines", lambda: _engines(("bing", True), ("yahoo", True), ("brave", True)))

    device = client.post("/api/devices", json={"name": "Coffee", "brand": "Nespresso", "model": "PIXIE C62"}).json()
    r = client.get(f"/api/downloads/{device['id']}/search")
    assert r.status_code == 200
    data = r.json()

    assert [c["engine"] for c in data["candidates"]] == ["brave"]
    # One row per attempted engine, merged across both search passes; the
    # blocked engine is visible instead of silently skipped.
    assert {e["engine"]: e["status"] for e in data["engines"]} == {
        "bing": "error",
        "yahoo": "unaccepted",
        "brave": "accepted",
    }
    bing = next(e for e in data["engines"] if e["engine"] == "bing")
    assert "blocked" in bing["error"]


def test_manual_search_stream_reports_engines_live(client, monkeypatch):
    """The SSE variant emits an event per engine as the chain walks it (start
    then end) and finishes with one `result` frame matching what the plain
    endpoint would have returned."""
    from homestew.services import web_search as ws

    def fake_run(name, query, **kwargs):
        if name == "bing":
            raise ws.SearchUnavailableError("bing blocked")
        return [ws.WebResult(title="C62 Manual PDF", url="https://x.com/c62.pdf", engine=name)]

    monkeypatch.setattr(ws, "_run_engine", fake_run)
    monkeypatch.setattr(ws, "configured_engines", lambda: _engines(("bing", True), ("yahoo", True)))

    device = client.post("/api/devices", json={"name": "Coffee", "brand": "Nespresso", "model": "PIXIE C62"}).json()
    r = client.get(f"/api/downloads/{device['id']}/search-stream")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    frames = [
        json.loads(line[len("data: "):])
        for line in r.text.split("\n")
        if line.startswith("data: ")
    ]
    engines = [f for f in frames if f["type"] == "engine"]
    result = next(f for f in frames if f["type"] == "result")

    # Live walk: bing starts, fails, then yahoo starts and wins the chain.
    assert [(e["phase"], e["engine"], e.get("status")) for e in engines] == [
        ("start", "bing", None),
        ("end", "bing", "error"),
        ("start", "yahoo", None),
        ("end", "yahoo", "accepted"),
    ]
    assert engines[0]["total"] == 2
    # The terminal frame carries the same payload as GET .../search.
    assert [c["engine"] for c in result["candidates"]] == ["yahoo"]
    assert {e["engine"]: e["status"] for e in result["engines"]} == {"bing": "error", "yahoo": "accepted"}


def test_manual_search_stream_404s_before_streaming(client):
    r = client.get("/api/downloads/999999/search-stream")
    assert r.status_code == 404


def _engines(*pairs):
    return [{"name": name, "enabled": enabled} for name, enabled in pairs]


def test_put_engines_rejects_overlong_name(client):
    r = client.put(
        "/api/settings", json={"manual_search_engines": [{"name": "m" * 40}]}
    )
    assert r.status_code == 422


def test_put_empty_engine_list_falls_back_to_defaults(client):
    r = client.put("/api/settings", json={"manual_search_engines": []})
    assert r.status_code == 200
    # GET shows the effective list: defaults (since an empty stored list is
    # unusable, web_search.configured_engines() substitutes them), followed by
    # any catalog-only engines ddgs ships disabled.
    names = [e["name"] for e in r.json()["manual_search_engines"]]
    assert names[: len(cfg.DEFAULT_MANUAL_SEARCH_ENGINES)] == [
        e["name"] for e in cfg.DEFAULT_MANUAL_SEARCH_ENGINES
    ]
    assert set(names) >= set(cfg.SEARCH_ENGINE_CATALOG)


def test_search_test_endpoint_reports_every_engine(client, monkeypatch):
    calls = {}

    def fake_report(query, *, engines=None, **kwargs):
        calls["query"] = query
        calls["engines"] = engines
        return [
            web_search.EngineOutcome(
                engine="bing", ok=True,
                results=[
                    web_search.WebResult(title=f"R{i}", url=f"https://x/{i}.pdf", engine="bing")
                    for i in range(7)
                ],
                error="",
            ),
            web_search.EngineOutcome(
                engine="brave",
                ok=False,
                results=[],
                error="HTTP 429 - throttled",
                http_status=429,
                detail="Error code: 429 - Too Many Requests",
                url="https://search.brave.com/search?q=q&source=web",
            ),
        ]

    monkeypatch.setattr(web_search, "search_engine_report", fake_report)
    r = client.post("/api/settings/search-test", json={"query": "Shark HZ 700C"})
    assert r.status_code == 200
    data = r.json()
    assert data["query"] == "Shark HZ 700C"
    assert calls["query"] == "Shark HZ 700C"
    results = data["results"]
    assert [x["engine"] for x in results] == ["bing", "brave"]
    assert results[0]["ok"] is True
    assert results[0]["result_count"] == 7
    # Sample links are capped at 5 to keep the response small.
    assert len(results[0]["results"]) == 5
    assert results[0]["results"][0] == {"title": "R0", "url": "https://x/0.pdf"}
    assert results[1]["ok"] is False and results[1]["error"] == "HTTP 429 - throttled"
    # HTTP status + raw response text reach the UI verbatim.
    assert results[0]["http_status"] == 0 and results[0]["note"] == ""
    assert results[1]["http_status"] == 429
    assert results[1]["detail"] == "Error code: 429 - Too Many Requests"
    # The exact URL we queried reaches the UI so the user can click it.
    assert results[1]["url"] == "https://search.brave.com/search?q=q&source=web"


def test_search_test_endpoint_passes_through_note_and_detail(client, monkeypatch):
    monkeypatch.setattr(
        web_search, "search_engine_report",
        lambda query, *, engines=None, **kw: [
            web_search.EngineOutcome(
                engine="bing", ok=True, results=[], error="", http_status=200,
                note="Engine responded normally (HTTP 200) but returned no results for this query.",
                url="https://www.bing.com/search?q=q&count=20",
            )
        ],
    )
    r = client.post("/api/settings/search-test", json={"query": "q"})
    assert r.status_code == 200
    row = r.json()["results"][0]
    assert row["http_status"] == 200 and row["result_count"] == 0
    assert "no results" in row["note"]
    assert row["url"] == "https://www.bing.com/search?q=q&count=20"


def test_search_test_endpoint_passes_engine_subset(client, monkeypatch):
    monkeypatch.setattr(
        web_search, "search_engine_report",
        lambda query, *, engines=None, **kw: [
            web_search.EngineOutcome(engine=engines[0], ok=True, results=[], error="")
        ],
    )
    r = client.post(
        "/api/settings/search-test",
        json={"query": "Schwinn IC4 manual filetype:pdf", "engines": ["bing"]},
    )
    assert r.status_code == 200
    assert [x["engine"] for x in r.json()["results"]] == ["bing"]


def test_search_test_rejects_blank_query(client, monkeypatch):
    def never(*a, **kw):  # pragma: no cover - must not run
        raise AssertionError("report should not be called")

    monkeypatch.setattr(web_search, "search_engine_report", never)
    r = client.post("/api/settings/search-test", json={"query": "   "})
    assert r.status_code == 422
