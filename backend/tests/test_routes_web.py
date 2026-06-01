"""tests/test_routes_web.py — HTTP-level tests for /api/web routes.

Exercises the real router + BearerAuthMiddleware end to end against the REAL
WebAPI (real curl_cffi fetch + Scrapling parse + sqlite-vec ingest) hitting the
real local_web_server fixture. No mocked API, no canned responses — the route's
container.api is a genuine WebAPI wired to a real RAGIndex.
"""

from __future__ import annotations

import hashlib
import logging
import math
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import web as web_routes
from server import BearerAuthMiddleware

TOKEN = "test-token-web"
EMBED_DIM = 384


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _deterministic_embed(texts):
    out = []
    for t in texts:
        vec = [0.0] * EMBED_DIM
        for tok in (t or "").lower().split():
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % EMBED_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out


class _Facade:
    """Minimal real facade exposing what WebAPI reads via BaseAPI passthrough."""
    def __init__(self, settings, rag):
        self._settings = settings
        self._rag = rag
        self._log = logging.getLogger("test.routes.web")

    def _emit(self, event, payload=None):
        pass


@pytest.fixture
def app(in_memory_db, settings, tmp_path, monkeypatch):
    from services import semantic_search
    from services.rag_index import RAGIndex
    from core.api.web import WebAPI
    from core import paths

    monkeypatch.setattr(semantic_search, "_embed_fn", _deterministic_embed)
    monkeypatch.setattr(semantic_search, "_embed_dim", EMBED_DIM)
    monkeypatch.setattr(semantic_search, "_initialized", True)
    monkeypatch.setattr(paths, "rag_cache_dir", lambda: tmp_path)

    settings.set("web_research_enabled", True)
    settings.set("web_research_allow_private", True)

    rag = RAGIndex(model=None)
    rag._semantic = semantic_search

    a = FastAPI()
    a.add_middleware(BearerAuthMiddleware, expected_token=TOKEN)
    a.include_router(web_routes.router, prefix="/api/web")
    a.state.container = SimpleNamespace(api=WebAPI(_Facade(settings, rag)))
    return a


def test_status_requires_auth(app):
    assert TestClient(app).get("/api/web/status").status_code == 401


def test_status_reports_real_capability(app):
    resp = TestClient(app).get("/api/web/status", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True   # deps installed in CI
    assert body["enabled"] is True
    assert isinstance(body["stealth_available"], bool)


def test_fetch_returns_real_markdown(app, local_web_server):
    resp = TestClient(app).post(
        "/api/web/fetch", json={"url": local_web_server.url("/")}, headers=_auth()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Acme Widgets"
    assert "Widget 3000" in body["markdown"]


def test_fetch_to_rag_indexes_real_page(app, local_web_server):
    import db
    resp = TestClient(app).post(
        "/api/web/fetch_to_rag", json={"url": local_web_server.url("/")}, headers=_auth()
    )
    assert resp.status_code == 200
    assert resp.json()["chunks_added"] >= 1
    row = db.fetchone("SELECT content FROM documents WHERE doc_type = 'web' LIMIT 1")
    assert row is not None and "Widget 3000" in row["content"]


def test_fetch_blocked_url_returns_friendly_envelope(app):
    # A private target is refused by the real SSRF guard (allow_private is on for
    # the fixture, so use an explicitly blocked scheme instead).
    resp = TestClient(app).post(
        "/api/web/fetch", json={"url": "ftp://internal/secret"}, headers=_auth()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body and body["reason"] == "blocked_scheme"
