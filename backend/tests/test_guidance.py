"""
tests/test_guidance.py — Guidance / Constitution compiler.

Covers shard splitting, persistence, always-on priority rules, vector recall
(via the deterministic test embedder), and the no-vector fallback.
"""

from __future__ import annotations

import hashlib
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


@pytest.fixture
def vector_env(in_memory_db, monkeypatch):
    from services import semantic_search
    monkeypatch.setattr(semantic_search, "_embed_fn", _deterministic_embed)
    monkeypatch.setattr(semantic_search, "_embed_dim", EMBED_DIM)
    monkeypatch.setattr(semantic_search, "_initialized", True)
    return in_memory_db


def test_split_into_shards():
    from services import guidance
    doc = """
    # Code Style
    - Always use type hints in Python.
    - Prefer composition over inheritance.

    # Security
    - Never log secrets or API keys.
    """
    shards = guidance.split_into_shards(doc)
    assert any("type hints" in s for s in shards)
    assert any(s.startswith("[Code Style]") for s in shards)
    assert any(s.startswith("[Security]") for s in shards)
    assert all(s.strip() for s in shards)


def test_compile_and_list(vector_env):
    from services import guidance
    ids = guidance.compile_from_prompt(
        "- Use snake_case for functions.\n- Write a docstring for every public function.",
        scope="global", source="CLAUDE.md",
    )
    assert len(ids) == 2
    rules = guidance.list_rules("global")
    assert len(rules) == 2
    assert guidance.rule_count() == 2


def test_dedupe_identical_shard(vector_env):
    from services import guidance
    a = guidance.add_rule("Never hardcode credentials.", scope="global")
    b = guidance.add_rule("Never hardcode credentials.", scope="global")
    assert a == b
    assert guidance.rule_count() == 1


def test_retrieve_relevant_rules(vector_env):
    from services import guidance
    guidance.add_rule("Always validate user input before database queries.", scope="global")
    guidance.add_rule("Use a consistent color palette in the UI.", scope="global")
    guidance.add_rule("Cache expensive database lookups where possible.", scope="global")

    hits = guidance.retrieve("how should I handle database queries safely",
                             top_k=2, min_sim=0.0)
    assert hits
    texts = " ".join(h["rule_text"].lower() for h in hits)
    assert "database" in texts


def test_always_on_rules_included(vector_env):
    from services import guidance
    guidance.add_rule("CRITICAL: never exfiltrate secrets.",
                      scope="global", priority=guidance.ALWAYS_ON_PRIORITY)
    guidance.add_rule("Use tabs not spaces.", scope="global", priority=0)

    # Query unrelated to the critical rule — it must still be present.
    hits = guidance.retrieve("what color should the button be", top_k=1, min_sim=0.0)
    assert any("exfiltrate" in h["rule_text"] for h in hits)


def test_agent_scope_and_global_both_apply(vector_env):
    from services import guidance
    guidance.add_rule("Global: be concise.", scope="global")
    guidance.add_rule("Agent: cite sources.", scope="agent:researcher")

    hits = guidance.retrieve("please write something", scope="agent:researcher",
                             top_k=5, min_sim=0.0)
    scopes_text = " ".join(h["rule_text"] for h in hits)
    assert "Global" in scopes_text
    assert "Agent" in scopes_text


def test_retrieve_fallback_without_vector(in_memory_db):
    """Without a vector store, retrieve() surfaces top-priority rules."""
    from services import guidance
    guidance.add_rule("low priority rule", scope="global", priority=1)
    guidance.add_rule("high priority rule", scope="global", priority=50)
    hits = guidance.retrieve("anything", top_k=1)
    assert hits
    assert hits[0]["rule_text"] == "high priority rule"


def test_format_block():
    from services import guidance
    assert guidance.format_block([]) == ""
    block = guidance.format_block([{"rule_text": "Be nice.", "priority": 0, "score": 1.0}])
    assert "## Relevant Rules" in block
    assert "Be nice." in block


def test_set_enabled_and_delete(vector_env):
    from services import guidance
    rid = guidance.add_rule("temporary rule", scope="global")
    guidance.set_enabled(rid, False)
    assert guidance.rule_count() == 0  # disabled rules don't count
    guidance.delete_rule(rid)
    assert guidance.list_rules("global") == []
