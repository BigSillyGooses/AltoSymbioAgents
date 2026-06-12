"""
benchmarks/perf/scenarios.py — The deterministic benchmark scenarios.

Each scenario is a zero-argument callable that assumes ``runner.isolated_env``
has already prepared the process (temp sqlite DB with the full schema, the
deterministic bag-of-words embedder wired into ``services.semantic_search``,
``services.perf_metrics`` enabled) and returns a metrics dict. The runner
attaches the perf_metrics span snapshot and wall clock afterwards.

Four scenarios (``team_pipeline`` arrives with Phase 5):

  - ``chat_short``  5-turn single-agent conversation through the REAL
                    ChatOrchestrator with fake clients injected.
  - ``chat_long``   Phase 3: 30 turns that overflow an 8,000-char history
                    budget, run in four flag configs (off / history caching /
                    compaction / both) reporting the billed-input proxy.
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


# ── chat_long ─────────────────────────────────────────────────────────────────

def run_chat_long() -> dict:
    """30 scripted turns under FOUR flag configs — the Phase 3 headline.

    Configs: both flags off / history caching only / compaction only / both.
    Per config the scenario reports the billed-input proxy
    (``input + 1.25·creation + 0.1·read`` — Anthropic's write premium and
    read discount), the cache hit rate, and the summary regeneration count.

    The scenario drives the Phase 3 units DIRECTLY — the production history
    window (MAX_HISTORY_MESSAGES mirror), ``history_compactor.compact``, and
    ``ClaudeClient._apply_history_cache`` — against the FakeClaudeClient
    prefix-cache simulation, rather than constructing a full
    ChatOrchestrator: the orchestrator wiring of the same pieces
    (``_compact_or_trim`` flag gate + legacy-trim fallback, the SDK kwargs
    the marker lands in) is covered by tests/test_history_compactor.py and
    tests/test_claude_history_caching.py. Driving the units directly keeps
    the model-call count exactly one per turn per config, which is what
    makes the four billed-token columns comparable.

    The history budget (8,000 chars) is scenario config from the fixture —
    NOT a patched MAX_CONTEXT_CHARS — sized so the 30-turn history overflows
    around turn 13 and every config spends most of the run over budget.
    """
    import db

    from services import history_compactor
    from services.claude_client import ClaudeClient

    from benchmarks.perf.fake_clients import FakeClaudeClient, FakeLocalClient

    fixture = load_fixture("chat_long")
    system_prompt = fixture["system_prompt"]
    budget_chars = int(fixture["budget_chars"])
    window_cap = 40  # mirrors chat_orchestrator.MAX_HISTORY_MESSAGES

    compaction_settings = {
        "history_compaction_keep_recent_msgs": 8,
        "history_compaction_batch_msgs": 6,
        "history_compaction_max_summary_chars": 2000,
    }

    def _trim_to_budget(messages: list) -> list:
        # Mirrors ChatOrchestrator._trim_history_to_budget (oldest-first
        # pop, always keep the newest message) without constructing the
        # orchestrator; the real method is exercised by the backend tests.
        trimmed = list(messages)
        while (len(trimmed) > 1
               and sum(len(m.get("content", "")) for m in trimmed) > budget_chars):
            trimmed.pop(0)
        return trimmed

    configs = {
        "off":        {"history_caching": False, "compaction": False},
        "caching":    {"history_caching": True,  "compaction": False},
        "compaction": {"history_caching": False, "compaction": True},
        "both":       {"history_caching": True,  "compaction": True},
    }

    results: dict[str, dict] = {}
    for config_name, flags in configs.items():
        claude = FakeClaudeClient(replies=[t["reply"] for t in fixture["turns"]])
        summarizer = FakeLocalClient(replies=[
            "Rolling summary: the conversation covers Meridian Atlas rollout "
            "checks — the launch budget is $48,500 at Harbor Hall chaired by "
            "Dana Okafor; detour, tariff, fleet, customs, and incident "
            "policies were each confirmed unchanged with the council's "
            "quarterly stamp on file.",
        ])
        # Real production marker code — only _apply_history_cache is used.
        marker_client = ClaudeClient(
            api_key="perf-fixture", model="claude-sonnet-4-6",
            use_caching=True,
            use_history_caching=flags["history_caching"],
        )

        conversation_id = f"perf-chat-long-{config_name}"
        db.execute(
            "INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)",
            (conversation_id, "perf chat_long", "2026-01-01T00:00:00+00:00"),
        )
        db.commit()

        history: list[dict] = []
        msg_count = 0

        def _persist(role: str, content: str) -> None:
            # The compactor anchors its summary coverage on the
            # conversation's absolute message count, so the scenario
            # persists each message exactly like a production turn would.
            nonlocal msg_count
            msg_count += 1
            db.execute(
                "INSERT INTO messages (id, conversation_id, role, content, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (f"{conversation_id}-{msg_count:04d}", conversation_id, role,
                 content,
                 f"2026-01-01T01:{msg_count // 60:02d}:{msg_count % 60:02d}+00:00"),
            )
            db.commit()

        turns: list[dict] = []
        for i, turn in enumerate(fixture["turns"], start=1):
            history.append({"role": "user", "content": turn["user"]})
            _persist("user", turn["user"])
            window = [dict(m) for m in history[-window_cap:]]
            if flags["compaction"]:
                try:
                    window = history_compactor.compact(
                        conversation_id=conversation_id,
                        messages=window,
                        budget_chars=budget_chars,
                        settings=compaction_settings,
                        local_client=summarizer,
                        claude_client=claude,
                    )
                except Exception:  # mirror the orchestrator's fallback
                    window = _trim_to_budget(window)
            else:
                window = _trim_to_budget(window)
            window = marker_client._apply_history_cache(window)

            usage = claude.chat_unified(system_prompt, window)
            history.append({"role": "assistant", "content": turn["reply"]})
            _persist("assistant", turn["reply"])

            billed = round(
                usage["input_tokens"]
                + 1.25 * usage["cache_creation_tokens"]
                + 0.1 * usage["cache_read_tokens"], 2,
            )
            turns.append({
                "turn": i,
                "input_tokens": usage["input_tokens"],
                "cache_creation_tokens": usage["cache_creation_tokens"],
                "cache_read_tokens": usage["cache_read_tokens"],
                "billed_input_tokens": billed,
            })

        input_total = sum(t["input_tokens"] for t in turns)
        creation_total = sum(t["cache_creation_tokens"] for t in turns)
        read_total = sum(t["cache_read_tokens"] for t in turns)
        results[config_name] = {
            "turn_count": len(turns),
            "turns": turns,
            "input_tokens": input_total,
            "cache_creation_tokens": creation_total,
            "cache_read_tokens": read_total,
            # Billed-input proxy: cache writes cost 1.25×, reads 0.1×.
            "billed_input_tokens": round(
                input_total + 1.25 * creation_total + 0.1 * read_total, 2,
            ),
            # Share of the resent prompt served from cache (creation counts
            # as un-served — it was processed at full price plus premium).
            "cache_hit_rate": _safe_rate(
                read_total, read_total + creation_total + input_total,
            ),
            "summary_regenerations": len(summarizer.calls),
        }

    off_billed = results["off"]["billed_input_tokens"]

    def _reduction_pct(name: str) -> float:
        if off_billed <= 0:
            return 0.0
        return round(
            100.0 * (1 - results[name]["billed_input_tokens"] / off_billed), 2,
        )

    return {
        "configs": results,
        "billed_reduction_pct_caching": _reduction_pct("caching"),
        "billed_reduction_pct_compaction": _reduction_pct("compaction"),
        "billed_reduction_pct_both": _reduction_pct("both"),
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
    "chat_long": run_chat_long,
    "rag_heavy": run_rag_heavy,
    "routing": run_routing,
}
