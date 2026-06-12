"""
tests/test_semantic_search_vec.py — vec0 KNN queries over documents/memories.

Regression coverage for a latent bug the perf harness surfaced: the document
and memory vector searches used a bare ``LIMIT ?`` on a vec0 KNN query, and
with the ``INNER JOIN`` onto the map table SQLite does not push the LIMIT
into the virtual-table scan — sqlite-vec then rejects the query ("A LIMIT or
'k = ?' constraint is required on vec0 knn queries") and ``search_documents``
silently returned []. The fix mirrors trajectory_store/guidance, which always
used the explicit ``AND k = ?`` constraint. These tests run the real
production SQL against real sqlite-vec, like test_trajectory_store.py.
"""

from __future__ import annotations

import pytest

from tests.test_trajectory_store import EMBED_DIM, _deterministic_embed


@pytest.fixture
def vector_env(in_memory_db, monkeypatch):
    from services import semantic_search
    monkeypatch.setattr(semantic_search, "_embed_fn", _deterministic_embed)
    monkeypatch.setattr(semantic_search, "_embed_dim", EMBED_DIM)
    monkeypatch.setattr(semantic_search, "_initialized", True)
    # BM25 state is module-global; isolate from other tests in the session.
    monkeypatch.setattr(semantic_search, "_bm25_index", None)
    monkeypatch.setattr(semantic_search, "_bm25_doc_ids", [])
    monkeypatch.setattr(semantic_search, "_bm25_corpus", [])
    monkeypatch.setattr(semantic_search, "_bm25_contents", {})
    return in_memory_db


def _ingest_and_index(docs: list[tuple[str, str]]) -> None:
    from services import semantic_search
    for content, source in docs:
        semantic_search.ingest_document(content, source)
    while semantic_search.run_indexer_cycle():
        pass


DOCS = [
    ("the refund policy allows returns within thirty days", "policies.md"),
    ("our shipping rates depend on package weight and zone", "shipping.md"),
    ("employees accrue vacation days each calendar month", "handbook.md"),
]


def test_search_documents_returns_vector_hits(vector_env):
    from services import semantic_search
    _ingest_and_index(DOCS)

    hits = semantic_search.search_documents("refund returns policy", top_k=2)
    assert hits, "vector search returned nothing — vec0 KNN query failed"
    assert hits[0]["doc_id"]
    assert "refund" in hits[0]["content"]


def test_search_documents_hybrid_fuses_both_legs(vector_env):
    from services import semantic_search
    _ingest_and_index(DOCS)

    hits = semantic_search.search_documents_hybrid("refund returns policy", top_k=3)
    assert hits
    # At least the top hit must carry a vector rank — before the k=? fix the
    # vector leg errored out and every result was BM25-only.
    assert any(h.get("vector_rank") is not None for h in hits)


def test_search_memories_vec_query_executes(vector_env):
    """search_memories shares the fixed JOIN+KNN shape over vec_memories."""
    import uuid
    from datetime import datetime, timezone

    import db
    from services import semantic_search

    now = datetime.now(timezone.utc).isoformat()
    mem_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO memory_entries (id, content, category, embedding_status, created_at) "
        "VALUES (?, ?, ?, 'dirty', ?)",
        (mem_id, "the user prefers tabs over spaces", "preference", now),
    )
    db.commit()
    while semantic_search.run_indexer_cycle():
        pass

    hits = semantic_search.search_memories("tabs spaces preference", top_k=3)
    assert hits, "memory vector search returned nothing — vec0 KNN query failed"
    assert hits[0]["entry_id"] == mem_id
