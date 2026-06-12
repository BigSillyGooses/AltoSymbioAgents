"""
benchmarks/perf/scenarios.py — The deterministic benchmark scenarios.

Each scenario is a zero-argument callable that assumes ``runner.isolated_env``
has already prepared the process (temp sqlite DB with the full schema, the
deterministic bag-of-words embedder wired into ``services.semantic_search``,
``services.perf_metrics`` enabled) and returns a metrics dict. The runner
attaches the perf_metrics span snapshot and wall clock afterwards.

Three scenarios ship with Phase 1b (``chat_long`` and ``team_pipeline``
arrive with Phases 3 and 5 respectively):

  - ``chat_short``  5-turn single-agent conversation through the REAL
                    ChatOrchestrator with fake clients injected.
  - ``rag_heavy``   ~2,000 fixture chunks through the production ingest +
                    indexer path, then 50 hybrid queries.
  - ``routing``     200 seeded trajectories, then 100 routing-recall
                    decisions (find_similar + bias_table) with latency spans.

Determinism contract: everything except span timings / wall clock must be
identical across two consecutive runs (asserted by tests/test_perf_harness.py).
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Read a checked-in fixture JSON. No generation at run time."""
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _safe_rate(num: float, denom: float) -> float:
    """Guarded ratio (0.0 when the denominator is zero)."""
    return 0.0 if denom <= 0 else round(num / denom, 6)


# ── chat_short ────────────────────────────────────────────────────────────────

def run_chat_short() -> dict:
    """5 scripted turns through a REAL ChatOrchestrator.

    ChatOrchestrator construction turned out NOT to be too entangled for the
    harness: the same wiring the backend test suite uses (fake model clients,
    a fixed-RouteDecision task router, a MemoryManager with no RAG index, and
    a plain settings dict — every collaborator reads settings via ``.get``)
    drives the full production ``send()`` path: TurnLifecycle open/close,
    MemoryRecall, TurnRouter, SecurityGate, Governance, HubRouter.invoke,
    EscalationLadder, and the cached-aware cost estimate.

    The system prompt is fixture-supplied and >2048 fixture tokens so the
    simulated system-prompt cache writes on turn 1 and reads on turns 2-5
    (the FakeLocalClient returns no extractable facts, keeping the assembled
    system prompt byte-stable across turns — see fake_clients.FakeLocalClient.chat).
    """
    import db
    from services.chat_orchestrator import ChatOrchestrator
    from services.memory import MemoryManager

    from benchmarks.perf.fake_clients import (
        FakeClaudeClient, FakeLocalClient, FakeTaskRouter,
    )

    fixture = load_fixture("chat_short")
    system_prompt = fixture["system_prompt"]

    # Plain dict settings: every collaborator reads configuration through
    # ``settings.get(key, default)``, so a dict pins every knob the scenario
    # cares about and leaves the rest at production defaults. Voting,
    # extended thinking, and the escalation channel are switched off because
    # they would add model calls whose count depends on heuristics over
    # fixture text rather than on the code paths being measured.
    settings = {
        "system_prompt": system_prompt,
        "high_stakes_voting_enabled": False,
        "interleaved_reasoning_enabled": False,
        "escalation_channel_enabled": False,
        "max_conversation_budget_usd": 5.0,
        "budget_warning_threshold_pct": 80.0,
    }

    claude = FakeClaudeClient(replies=[t["reply"] for t in fixture["turns"]])
    local = FakeLocalClient()
    memory = MemoryManager(None, None, local, settings)
    orchestrator = ChatOrchestrator(
        claude, local, FakeTaskRouter(model="claude", complexity="simple"),
        memory, settings,
    )

    conversation_id = orchestrator.create_conversation()
    turns: list[dict] = []
    for i, turn in enumerate(fixture["turns"], start=1):
        result = orchestrator.send(conversation_id, turn["user"])
        # Cache accounting comes from token_usage (ChatResult doesn't carry
        # the cache columns) — TurnLifecycle.close just wrote this turn's row.
        usage_row = db.fetchone(
            "SELECT cache_read_tokens, cache_creation_tokens FROM token_usage "
            "WHERE conversation_id = ? ORDER BY rowid DESC LIMIT 1",
            (conversation_id,),
        )
        turns.append({
            "turn": i,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cache_read_tokens": usage_row["cache_read_tokens"] if usage_row else 0,
            "cache_creation_tokens": usage_row["cache_creation_tokens"] if usage_row else 0,
            "cost_usd": round(result.cost_usd, 8),
            "model": result.model,
            "route_reason": result.route_reason,
        })

    totals = {
        "tokens_in": sum(t["tokens_in"] for t in turns),
        "tokens_out": sum(t["tokens_out"] for t in turns),
        "cache_read_tokens": sum(t["cache_read_tokens"] for t in turns),
        "cache_creation_tokens": sum(t["cache_creation_tokens"] for t in turns),
        "cost_usd": round(sum(t["cost_usd"] for t in turns), 8),
    }
    n = len(turns)
    return {
        "turn_count": n,
        "turns": turns,
        "totals": totals,
        "tokens_in_per_turn": _safe_rate(totals["tokens_in"], n),
        "tokens_out_per_turn": _safe_rate(totals["tokens_out"], n),
        "cost_per_turn_usd": _safe_rate(totals["cost_usd"], n),
        # Hit rate = read / (read + uncached input). The API's input_tokens
        # already excludes cached tokens, so this is "share of resent prompt
        # that was served from cache". Guarded against /0.
        "cache_hit_rate": _safe_rate(
            totals["cache_read_tokens"],
            totals["cache_read_tokens"] + totals["tokens_in"],
        ),
    }


# ── rag_heavy ─────────────────────────────────────────────────────────────────

def run_rag_heavy() -> dict:
    """Ingest ~2,000 fixture chunks via the production path, then 50 queries.

    Ingest goes through ``semantic_search.ingest_document`` (documents table +
    immediate BM25 sync) followed by ``run_indexer_cycle`` until the dirty
    backlog drains — the exact write path production uses, including its
    per-document BM25 rebuild cost (which the ``ingest_total`` /
    ``index_total`` spans make visible). Queries go through
    ``search_documents_hybrid`` so the embed/bm25/vec_search/rrf spans from
    Phase 1a all light up.
    """
    from services import perf_metrics, semantic_search

    fixture = load_fixture("rag_heavy")

    with perf_metrics.span("ingest_total"):
        for chunk in fixture["chunks"]:
            semantic_search.ingest_document(
                chunk["text"], source=chunk["source"], doc_type="text",
            )
    with perf_metrics.span("index_total"):
        while semantic_search.run_indexer_cycle():
            pass

    queries_with_results = 0
    results_returned = 0
    fused_from_both = 0
    for query in fixture["queries"]:
        hits = semantic_search.search_documents_hybrid(query, top_k=5)
        if hits:
            queries_with_results += 1
        results_returned += len(hits)
        fused_from_both += sum(1 for h in hits if h.get("result_source") == "both")

    n_queries = len(fixture["queries"])
    return {
        "chunks_ingested": len(fixture["chunks"]),
        "documents_indexed": semantic_search.document_count(),
        "query_count": n_queries,
        "queries_with_results": queries_with_results,
        "results_returned": results_returned,
        "results_per_query": _safe_rate(results_returned, n_queries),
        "fused_from_both": fused_from_both,
    }


# ── routing ───────────────────────────────────────────────────────────────────

def run_routing() -> dict:
    """Seed 200 trajectories, then make 100 routing-recall decisions.

    Each decision performs the two reads the router makes in production:
    ``find_similar`` (what the explain path uses) and ``bias_table`` (what
    ``HubRouter._trajectory_rates`` consumes). Latency rides in the
    ``traj_find_similar`` / ``traj_bias_table`` spans; the recall counts are
    the deterministic part.
    """
    from services import perf_metrics, trajectory_store

    fixture = load_fixture("routing")

    seeded = 0
    for t in fixture["trajectories"]:
        traj_id = trajectory_store.record(
            conversation_id=t["conversation_id"],
            turn_id=t["turn_id"],
            task_text=t["task_text"],
            agent_id=t["agent_id"],
            skill_matched=t["skill_matched"],
            backend=t["backend"],
            model_name=t["model_name"],
            routing_score=0.7,
            route_reasoning="perf seed",
            quality_verdict=t["quality_verdict"],
            had_error=bool(t["had_error"]),
            response_empty=False,
            tokens_in=10,
            tokens_out=20,
        )
        if traj_id is not None:
            seeded += 1

    decisions = 0
    similar_hits = 0
    bias_tables_nonempty = 0
    agents_biased = 0
    # min_sim is relaxed vs the production default (0.6) because the
    # bag-of-words embedder produces lower cosine similarities than the
    # real model for paraphrases; the SQL path being timed is identical.
    for query in fixture["queries"]:
        with perf_metrics.span("traj_find_similar"):
            similar = trajectory_store.find_similar(query, top_k=3, min_sim=0.3)
        with perf_metrics.span("traj_bias_table"):
            table = trajectory_store.bias_table(query, top_k=5, min_sim=0.3)
        decisions += 1
        similar_hits += len(similar)
        if table:
            bias_tables_nonempty += 1
            agents_biased += len(table)

    return {
        "trajectories_seeded": seeded,
        "decision_count": decisions,
        "similar_hits": similar_hits,
        "bias_tables_nonempty": bias_tables_nonempty,
        "agents_biased": agents_biased,
    }


# ── Registry ──────────────────────────────────────────────────────────────────

SCENARIOS = {
    "chat_short": run_chat_short,
    "rag_heavy": run_rag_heavy,
    "routing": run_routing,
}
