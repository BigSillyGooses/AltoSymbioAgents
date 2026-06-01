"""
tests/test_trajectory_store.py — ReasoningBank-lite trajectory store.

Exercises the real sqlite-vec path (the extension is installed in CI) using a
deterministic bag-of-words embedder so the tests don't depend on downloading
the fastembed model. The embedder is wired into ``semantic_search`` exactly
where the real fastembed function would sit, so record/index/search run the
production SQL unchanged.
"""

from __future__ import annotations

import hashlib

import pytest

EMBED_DIM = 384


def _deterministic_embed(texts):
    """Hash tokens into a normalized 384-dim bag-of-words vector.

    Texts that share tokens land close together under L2 distance, which is
    all the similarity tests need. This is test scaffolding for the vector
    index — the production embedder is fastembed/BAAI-bge-small.
    """
    import math
    out = []
    for t in texts:
        vec = [0.0] * EMBED_DIM
        for tok in (t or "").lower().split():
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % EMBED_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out


@pytest.fixture
def vector_env(in_memory_db, monkeypatch):
    """Enable the vector store with the deterministic embedder."""
    from services import semantic_search
    monkeypatch.setattr(semantic_search, "_embed_fn", _deterministic_embed)
    monkeypatch.setattr(semantic_search, "_embed_dim", EMBED_DIM)
    monkeypatch.setattr(semantic_search, "_initialized", True)
    return in_memory_db


def _record(conv="c1", task="", agent="agent-a", skill="research", verdict="success",
            had_error=False):
    from services import trajectory_store
    return trajectory_store.record(
        conversation_id=conv, turn_id="t-" + task[:5], task_text=task,
        agent_id=agent, skill_matched=skill, backend="claude",
        model_name="claude-sonnet-4-6", routing_score=0.7,
        route_reasoning="test", quality_verdict=verdict,
        had_error=had_error, response_empty=False, tokens_in=10, tokens_out=20,
    )


def test_record_writes_row(vector_env):
    import db
    from services import trajectory_store
    tid = _record(task="summarize the quarterly financial report")
    assert tid is not None
    row = db.fetchone("SELECT * FROM trajectories WHERE id = ?", (tid,))
    assert row is not None
    assert row["agent_id"] == "agent-a"
    assert row["quality_verdict"] == "success"
    # Inline embed should have marked it clean and populated the vec map.
    assert row["embedding_status"] == "clean"
    assert db.fetchone(
        "SELECT 1 FROM vec_trajectories_map WHERE trajectory_id = ?", (tid,)
    ) is not None


def test_find_similar_ranks_closest_first(vector_env):
    from services import trajectory_store
    _record(task="write a python function to sort a list", skill="code")
    _record(task="summarize the quarterly financial report", skill="research")
    _record(task="translate this paragraph into french", skill="writing")

    hits = trajectory_store.find_similar(
        "summarize the financial report", top_k=3, min_sim=0.0
    )
    assert hits, "expected at least one similar trajectory"
    assert "summarize" in hits[0]["task_text"]


def test_bias_for_reflects_success_rate(vector_env):
    from services import trajectory_store
    # agent-good always succeeds on this task family.
    for _ in range(3):
        _record(task="audit the security of the auth module",
                agent="agent-good", verdict="success")
    # agent-bad always fails on the same family.
    for _ in range(3):
        _record(task="audit the security of the auth module",
                agent="agent-bad", verdict="2.3", had_error=True)

    good = trajectory_store.bias_for(
        "audit the security of the login module", "agent-good", "research",
        min_sim=0.0,
    )
    bad = trajectory_store.bias_for(
        "audit the security of the login module", "agent-bad", "research",
        min_sim=0.0,
    )
    assert good is not None and bad is not None
    assert good > bad
    assert good >= 0.9   # all successes
    assert bad <= 0.1    # all failures


def test_bias_for_returns_none_without_history(vector_env):
    from services import trajectory_store
    assert trajectory_store.bias_for(
        "a task never seen before xyzzy", "unknown-agent", min_sim=0.0
    ) is None


def test_is_success_classifier():
    from services import trajectory_store
    assert trajectory_store.is_success("success") is True
    assert trajectory_store.is_success(None) is True
    assert trajectory_store.is_success("1.1") is False
    assert trajectory_store.is_success("3.3") is False
    assert trajectory_store.is_success("2.6") is False


def test_record_noop_without_vector_store(in_memory_db):
    """record() still writes the row even when the vector store is off."""
    import db
    from services import trajectory_store
    tid = trajectory_store.record(
        conversation_id="c", turn_id="t", task_text="some task",
        agent_id="a", skill_matched="s", backend="local", model_name="m",
        routing_score=0.5, route_reasoning="r", quality_verdict="success",
        had_error=False, response_empty=False, tokens_in=1, tokens_out=1,
    )
    assert tid is not None
    # Row exists but stays 'dirty' (embed skipped, no vector store).
    row = db.fetchone("SELECT embedding_status FROM trajectories WHERE id = ?", (tid,))
    assert row["embedding_status"] == "dirty"
    # find_similar degrades to empty rather than raising.
    assert trajectory_store.find_similar("some task") == []


def test_bias_table_groups_agents_in_one_query(vector_env, monkeypatch):
    """bias_table embeds the task ONCE and returns a per-agent success rate
    consistent with bias_for (which the router used to call once per agent)."""
    from services import trajectory_store, semantic_search
    for _ in range(3):
        _record(task="audit the security of the auth module",
                agent="agent-good", verdict="success")
    for _ in range(3):
        _record(task="audit the security of the auth module",
                agent="agent-bad", verdict="2.3", had_error=True)

    # Count embeddings issued while building the whole table.
    calls = {"n": 0}
    base = semantic_search._embed_fn
    monkeypatch.setattr(
        semantic_search, "_embed_fn",
        lambda texts: (calls.__setitem__("n", calls["n"] + 1) or base(texts)),
    )

    table = trajectory_store.bias_table(
        "audit the security of the login module", top_k=5, min_sim=0.0)

    assert calls["n"] == 1                       # one embed for all candidates
    assert table["agent-good"] > table["agent-bad"]
    assert table["agent-good"] >= 0.9
    assert table["agent-bad"] <= 0.1
