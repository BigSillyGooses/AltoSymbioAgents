"""
tests/test_embedding_cache.py — Perf Phase 2 embedding cache + retrieval upgrades.

Exercises the two-tier (memory LRU + SQLite) content-hash cache in
services/embedding_cache.py against the real schema (``in_memory_db``) with
the deterministic bag-of-words embedder from test_trajectory_store wired in
where fastembed would sit — wrapped in a call counter so the tests can
assert exactly when the real embedder runs.

Also covers the retrieval upgrades that landed with the same phase:
``rag_top_k`` / ``memory_similarity_threshold`` plumbing in memory/rag.py
and the flag-gated MMR diversity re-rank + post-RRF cutoff in
``search_documents_hybrid`` (flag-off must be byte-identical).
"""

from __future__ import annotations

import pytest

from tests.test_trajectory_store import EMBED_DIM, _deterministic_embed


class _CountingEmbedder:
    """Wraps the deterministic embedder; records every real-embed call."""

    def __init__(self):
        self.calls = 0
        self.texts: list[str] = []

    def __call__(self, texts):
        self.calls += 1
        self.texts.extend(texts)
        return _deterministic_embed(texts)


@pytest.fixture
def vector_env(in_memory_db, monkeypatch):
    """Vector store up with the counting embedder; cache flag still OFF."""
    from services import embedding_cache, semantic_search

    embedder = _CountingEmbedder()
    monkeypatch.setattr(semantic_search, "_embed_fn", embedder)
    monkeypatch.setattr(semantic_search, "_embed_dim", EMBED_DIM)
    monkeypatch.setattr(semantic_search, "_initialized", True)
    # BM25 + settings state is module-global; isolate from the session.
    monkeypatch.setattr(semantic_search, "_bm25_index", None)
    monkeypatch.setattr(semantic_search, "_bm25_doc_ids", [])
    monkeypatch.setattr(semantic_search, "_bm25_corpus", [])
    monkeypatch.setattr(semantic_search, "_bm25_contents", {})
    monkeypatch.setattr(semantic_search, "_settings_obj", None)
    monkeypatch.setattr(embedding_cache, "_settings_obj", None)
    embedding_cache.clear_memory_tier()
    yield embedder
    embedding_cache.clear_memory_tier()


@pytest.fixture
def cache_env(vector_env, monkeypatch):
    """vector_env plus the embedding cache switched ON via a dict settings."""
    from services import embedding_cache, semantic_search

    settings = {"embedding_cache_enabled": True}
    monkeypatch.setattr(embedding_cache, "_settings_obj", settings)
    monkeypatch.setattr(semantic_search, "_settings_obj", settings)
    return vector_env, settings


# ── get_or_embed: tiers, ordering, isolation ──────────────────────────────────

def test_miss_then_hit_embeds_once(cache_env):
    from services import embedding_cache
    embedder, _ = cache_env

    first = embedding_cache.get_or_embed(["the quick brown fox"])
    second = embedding_cache.get_or_embed(["the quick brown fox"])

    assert embedder.calls == 1, "repeat text must be served from cache"
    assert first == second
    assert len(first[0]) == EMBED_DIM


def test_within_batch_duplicates_embed_once(cache_env):
    from services import embedding_cache
    embedder, _ = cache_env

    vecs = embedding_cache.get_or_embed(["same text", "same text"])

    assert embedder.texts == ["same text"]
    assert vecs[0] == vecs[1]


def test_order_preserved_with_mixed_hit_miss_batch(cache_env):
    from services import embedding_cache
    embedder, _ = cache_env

    embedding_cache.get_or_embed(["alpha doc", "gamma doc"])  # warm two
    embedder.texts.clear()

    texts = ["alpha doc", "beta doc", "gamma doc", "delta doc"]
    vecs = embedding_cache.get_or_embed(texts)

    # Only the two misses reached the real embedder, in one batch call.
    assert embedder.texts == ["beta doc", "delta doc"]
    # Output order matches input order regardless of hit/miss interleaving.
    expected = _deterministic_embed(texts)
    for got, want in zip(vecs, expected):
        assert got == pytest.approx(want, abs=1e-6)


def test_memory_lru_evicts_oldest(cache_env, monkeypatch):
    from services import embedding_cache
    _, settings = cache_env
    settings["embedding_cache_memory_items"] = 2

    embedding_cache.get_or_embed(["one"])
    embedding_cache.get_or_embed(["two"])
    embedding_cache.get_or_embed(["three"])  # evicts "one"

    with embedding_cache._mem_lock:
        keys = set(embedding_cache._mem_cache)
    assert len(keys) == 2
    assert embedding_cache._content_hash("one") not in keys
    assert embedding_cache._content_hash("three") in keys


def test_db_tier_persists_across_cleared_memory_tier(cache_env):
    from services import embedding_cache
    embedder, _ = cache_env

    original = embedding_cache.get_or_embed(["persist me please"])[0]
    embedding_cache.clear_memory_tier()
    revived = embedding_cache.get_or_embed(["persist me please"])[0]

    assert embedder.calls == 1, "DB tier should have served the cleared-RAM hit"
    # The SQLite tier stores float32 (same precision as the vec0 tables),
    # so equality is approximate against the float64 test embedder.
    assert revived == pytest.approx(original, abs=1e-6)


def test_model_key_isolation(cache_env, monkeypatch):
    import db
    from services import embedding_cache
    embedder, _ = cache_env

    embedding_cache.get_or_embed(["shared text"])
    monkeypatch.setattr(embedding_cache, "_MODEL_KEY", "other-model-v2")
    embedding_cache.get_or_embed(["shared text"])

    assert embedder.calls == 2, "a different model key must not hit the old entry"
    row = db.fetchone("SELECT COUNT(*) AS cnt FROM embedding_cache")
    assert row["cnt"] == 2
    models = {r["model"] for r in db.fetchall("SELECT model FROM embedding_cache")}
    assert models == {"BAAI/bge-small-en-v1.5", "other-model-v2"}


def test_db_failure_falls_through_to_real_embedder(cache_env, monkeypatch):
    from services import embedding_cache
    embedder, _ = cache_env

    class _BrokenDB:
        def fetchone(self, *a, **k):
            raise RuntimeError("disk on fire")

        fetchall = execute = executemany = commit = fetchone

    monkeypatch.setattr(embedding_cache, "db", _BrokenDB())

    vecs = embedding_cache.get_or_embed(["resilient text"])
    assert vecs[0] == pytest.approx(
        _deterministic_embed(["resilient text"])[0], abs=1e-6
    )

    # Memory tier still works; only the DB tier degraded.
    embedding_cache.clear_memory_tier()
    embedding_cache.get_or_embed(["resilient text"])
    assert embedder.calls == 2, "with the DB tier broken, a cold lookup re-embeds"


def test_prune_caps_row_count(cache_env):
    import db
    from services import embedding_cache
    _, settings = cache_env
    settings["embedding_cache_max_rows"] = 5

    for i in range(8):
        embedding_cache.get_or_embed([f"chunk number {i}"])

    row = db.fetchone("SELECT COUNT(*) AS cnt FROM embedding_cache")
    assert row["cnt"] <= 5
    # The most recent entry survived the prune.
    assert db.fetchone(
        "SELECT 1 FROM embedding_cache WHERE content_hash = ?",
        (embedding_cache._content_hash("chunk number 7"),),
    ) is not None


# ── Flag-off behavior: the legacy path must be untouched ─────────────────────

def _ingest_and_index(docs):
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


def test_flag_off_semantic_search_never_touches_cache(vector_env, monkeypatch):
    import db
    from services import embedding_cache, semantic_search

    def _sentinel(texts):
        raise AssertionError("embedding cache used while flag is off")

    monkeypatch.setattr(embedding_cache, "get_or_embed", _sentinel)

    _ingest_and_index(DOCS)
    hits = semantic_search.search_documents("refund returns policy", top_k=2)
    assert hits and "refund" in hits[0]["content"]

    row = db.fetchone("SELECT COUNT(*) AS cnt FROM embedding_cache")
    assert row["cnt"] == 0


def test_flag_off_trajectory_store_never_touches_cache(vector_env, monkeypatch):
    from services import embedding_cache, trajectory_store

    def _sentinel(texts):
        raise AssertionError("embedding cache used while flag is off")

    monkeypatch.setattr(embedding_cache, "get_or_embed", _sentinel)

    tid = trajectory_store.record(
        conversation_id="c1", turn_id="t1", task_text="summarize the report",
        agent_id="agent-a", skill_matched="research", backend="claude",
        model_name="claude-sonnet-4-6", routing_score=0.7,
        route_reasoning="test", quality_verdict="success",
        had_error=False, response_empty=False, tokens_in=10, tokens_out=20,
    )
    assert tid is not None
    assert trajectory_store.find_similar("summarize the report", min_sim=0.0)


def test_cache_on_returns_same_search_results_as_off(vector_env, monkeypatch):
    from services import embedding_cache, semantic_search

    _ingest_and_index(DOCS)
    baseline = semantic_search.search_documents_hybrid("refund returns policy", top_k=3)

    settings = {"embedding_cache_enabled": True}
    monkeypatch.setattr(embedding_cache, "_settings_obj", settings)
    cached_run = semantic_search.search_documents_hybrid("refund returns policy", top_k=3)
    repeat_run = semantic_search.search_documents_hybrid("refund returns policy", top_k=3)

    assert cached_run == baseline
    assert repeat_run == baseline


def test_cache_hits_skip_real_embedder_on_repeat_queries(vector_env, monkeypatch):
    from services import embedding_cache, semantic_search

    _ingest_and_index(DOCS)
    settings = {"embedding_cache_enabled": True}
    monkeypatch.setattr(embedding_cache, "_settings_obj", settings)

    semantic_search.search_documents("refund returns policy", top_k=2)
    calls_after_first = vector_env.calls
    semantic_search.search_documents("refund returns policy", top_k=2)
    assert vector_env.calls == calls_after_first, "repeat query must be a cache hit"


# ── memory/rag.py: rag_top_k + memory_similarity_threshold plumbing ──────────

class _FakeRag:
    def __init__(self):
        self.top_k_seen = None

    def search(self, query, top_k):
        self.top_k_seen = top_k
        return [(f"chunk-{i}", 0.9) for i in range(top_k)]


class _FakeSemantic:
    def __init__(self):
        self.top_k_seen = None

    def search_memories(self, query, top_k):
        self.top_k_seen = top_k
        return [
            {"content": "high", "score": 0.9},
            {"content": "low", "score": 0.3},
        ]


def _get_context(settings):
    from models import SessionHistory
    from services.memory.rag import _RagAssembler

    rag, semantic = _FakeRag(), _FakeSemantic()
    assembler = _RagAssembler(rag, semantic, settings)
    ctx = assembler.get_context("conv-1", "what is the policy", [], SessionHistory())
    return rag, semantic, ctx


def test_rag_defaults_match_legacy_hardcoded_values(in_memory_db):
    rag, semantic, ctx = _get_context(settings=None)
    assert rag.top_k_seen == 3
    assert semantic.top_k_seen == 3
    assert ctx.memories == ["high"]  # 0.3 < legacy SIMILARITY_THRESHOLD 0.5


def test_rag_top_k_and_threshold_settings_flow_through(in_memory_db):
    rag, semantic, ctx = _get_context(
        settings={"rag_top_k": 5, "memory_similarity_threshold": 0.2},
    )
    assert rag.top_k_seen == 5
    assert semantic.top_k_seen == 5
    assert ctx.memories == ["high", "low"]  # 0.3 clears the lowered threshold


# ── MMR diversity re-rank + post-RRF cutoff ──────────────────────────────────

def test_mmr_rerank_promotes_diverse_candidate():
    from services import semantic_search

    # q halfway between d1 and d3; d2 is an exact duplicate of d1.
    q = [1.0, 1.0, 0.0]
    d1 = [1.0, 0.0, 0.0]
    d2 = [1.0, 0.0, 0.0]
    d3 = [0.0, 1.0, 0.0]
    candidates = [("d1", 0.03, d1), ("d2", 0.029, d2), ("d3", 0.028, d3)]

    picked = semantic_search.mmr_rerank(candidates, q, lambda_=0.7, top_k=3)
    # The duplicate is demoted below the orthogonal-but-relevant candidate.
    assert picked == ["d1", "d3", "d2"]


def test_mmr_rerank_handles_vectorless_candidates():
    from services import semantic_search

    q = [1.0, 0.0]
    candidates = [("a", 0.03, [1.0, 0.0]), ("b", 0.02, None), ("c", 0.01, None)]
    picked = semantic_search.mmr_rerank(candidates, q, lambda_=0.7, top_k=2)
    assert len(picked) == 2
    assert picked[0] == "a"


DUPLICATE_DOCS = [
    ("solar panel cleaning guide wipe the panel surface gently every week", "a.md"),
    ("solar panel cleaning guide wipe the panel surface gently every month", "b.md"),
    ("solar panel cleaning guide wipe the panel surface gently every season", "c.md"),
    ("solar panel cleaning guide wipe the panel surface gently every morning", "d.md"),
    ("solar panel cleaning costs depend on roof height and ladder access fees", "e.md"),
]


def test_mmr_flag_off_results_identical(vector_env, monkeypatch):
    from services import semantic_search

    _ingest_and_index(DUPLICATE_DOCS)
    baseline = semantic_search.search_documents_hybrid("solar panel cleaning", top_k=3)
    assert baseline

    # Settings attached but every Phase-2 flag at its default → identical.
    monkeypatch.setattr(semantic_search, "_settings_obj", {
        "rag_mmr_enabled": False,
        "rag_mmr_lambda": 0.7,
        "rag_rrf_min_score": 0.0,
    })
    assert semantic_search.search_documents_hybrid("solar panel cleaning", top_k=3) == baseline


def test_mmr_diversity_breaks_duplicate_crowding(vector_env, monkeypatch):
    from services import semantic_search

    _ingest_and_index(DUPLICATE_DOCS)

    def _sources(hits):
        return {h["file_source"] for h in hits}

    baseline = semantic_search.search_documents_hybrid("solar panel cleaning", top_k=3)
    assert len(baseline) == 3

    monkeypatch.setattr(semantic_search, "_settings_obj", {"rag_mmr_enabled": True})
    diverse = semantic_search.search_documents_hybrid("solar panel cleaning", top_k=3)
    assert len(diverse) == 3
    # With MMR on, the near-duplicate guides cannot fill the whole top-3:
    # the distinct costs document must surface.
    assert "e.md" in _sources(diverse)


def test_rrf_min_score_cutoff_filters_results(vector_env, monkeypatch):
    from services import semantic_search

    _ingest_and_index(DOCS)
    baseline = semantic_search.search_documents_hybrid("refund returns policy", top_k=3)
    assert baseline

    # Impossibly high cutoff (single-list RRF max is 1/(60+1) ≈ 0.0164 per
    # leg, ~0.033 fused) removes everything; 0.0 keeps the list unchanged.
    monkeypatch.setattr(semantic_search, "_settings_obj", {"rag_rrf_min_score": 0.9})
    assert semantic_search.search_documents_hybrid("refund returns policy", top_k=3) == []

    monkeypatch.setattr(semantic_search, "_settings_obj", {"rag_rrf_min_score": 0.0})
    assert semantic_search.search_documents_hybrid("refund returns policy", top_k=3) == baseline
