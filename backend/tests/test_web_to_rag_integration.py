"""tests/test_web_to_rag_integration.py — real fetch → scan → RAG ingest.

Drives the genuine WebAPI.web_fetch_to_rag against the real local_web_server,
the real input_sanitizer scan, and the real sqlite-vec/BM25 ingest path (with
the repo's deterministic test embedder). Proves a fetched page lands in the
``documents`` table tagged ``doc_type='web'`` and is retrievable.
"""

from __future__ import annotations

import hashlib
import logging
import math

import pytest

EMBED_DIM = 384


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
    """Minimal real facade exposing exactly what WebAPI reads via BaseAPI."""
    def __init__(self, settings, rag):
        self._settings = settings
        self._rag = rag
        self._log = logging.getLogger("test.web")
        self.events = []

    def _emit(self, event, payload=None):
        self.events.append((event, payload))


@pytest.fixture
def web_api(in_memory_db, settings, tmp_path, monkeypatch):
    from services import semantic_search
    from services.rag_index import RAGIndex
    from core.api.web import WebAPI
    from core import paths

    # Real vector store with the deterministic embedder (no 200MB download).
    monkeypatch.setattr(semantic_search, "_embed_fn", _deterministic_embed)
    monkeypatch.setattr(semantic_search, "_embed_dim", EMBED_DIM)
    monkeypatch.setattr(semantic_search, "_initialized", True)
    monkeypatch.setattr(paths, "rag_cache_dir", lambda: tmp_path)

    settings.set("web_research_enabled", True)
    settings.set("web_research_allow_private", True)  # reach the loopback fixture

    rag = RAGIndex(model=None)
    rag._semantic = semantic_search  # wire the (now-available) real backend

    return WebAPI(_Facade(settings, rag))


@pytest.mark.asyncio
async def test_fetch_to_rag_indexes_real_page_as_web(web_api, local_web_server):
    import db

    out = await web_api.web_fetch_to_rag(local_web_server.url("/"))

    assert "error" not in out, out
    assert out["chunks_added"] >= 1
    assert out["url"] == local_web_server.url("/")
    assert out["title"] == "Acme Widgets"

    # Real row in the documents table, tagged as web-sourced.
    row = db.fetchone(
        "SELECT content, doc_type, source FROM documents WHERE doc_type = 'web' LIMIT 1"
    )
    assert row is not None
    assert row["doc_type"] == "web"
    assert row["source"] == local_web_server.url("/")
    assert "Widget 3000" in row["content"]

    # Emitted the live-status + rag_done events the UI timeline listens for.
    kinds = [e for e, _ in web_api._facade.events]
    assert "web_fetch" in kinds
    assert "rag_done" in kinds


@pytest.mark.asyncio
async def test_fetch_to_rag_reaches_the_searchable_index(web_api, local_web_server):
    from services import semantic_search

    before = semantic_search.document_count()
    await web_api.web_fetch_to_rag(local_web_server.url("/"))

    # Embed the freshly-ingested (dirty) web doc with the deterministic embedder
    # and confirm it landed in the vector index the Researcher's search reads.
    indexed = semantic_search._index_dirty_documents()
    assert indexed >= 1
    assert semantic_search.document_count() > before

    # And the BM25 side ingested it immediately (content recoverable by doc).
    import db
    row = db.fetchone("SELECT content FROM documents WHERE doc_type = 'web' LIMIT 1")
    assert row is not None and "Widget 3000" in row["content"]


@pytest.mark.asyncio
async def test_fetch_to_rag_disabled_when_flag_off(in_memory_db, settings, local_web_server):
    from services.rag_index import RAGIndex
    from core.api.web import WebAPI

    # web_research_enabled defaults to False — must refuse without fetching.
    api = WebAPI(_Facade(settings, RAGIndex(model=None)))
    out = await api.web_fetch_to_rag(local_web_server.url("/"))
    assert out["reason"] == "disabled"


def test_fetch_and_index_sync_helper_indexes_real_page(in_memory_db, settings, monkeypatch, local_web_server):
    """The sync orchestrator helper fetches + scans + indexes a real page."""
    from services import semantic_search, web_research
    from services.rag_index import RAGIndex

    monkeypatch.setattr(semantic_search, "_embed_fn", _deterministic_embed)
    monkeypatch.setattr(semantic_search, "_embed_dim", EMBED_DIM)
    monkeypatch.setattr(semantic_search, "_initialized", True)
    settings.set("web_research_allow_private", True)

    rag = RAGIndex(model=None)
    rag._semantic = semantic_search

    out = web_research.fetch_and_index(local_web_server.url("/"), rag=rag, settings=settings)
    assert "error" not in out, out
    assert out["chunks_added"] >= 1
    assert out["title"] == "Acme Widgets"

    import db
    row = db.fetchone("SELECT content FROM documents WHERE doc_type = 'web' LIMIT 1")
    assert row is not None and "Widget 3000" in row["content"]
