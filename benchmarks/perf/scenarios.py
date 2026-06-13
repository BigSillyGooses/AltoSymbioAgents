"""
benchmarks/perf/scenarios.py — The deterministic benchmark scenarios.

Each scenario is a zero-argument callable that assumes ``runner.isolated_env``
has already prepared the process (temp sqlite DB with the full schema, the
deterministic bag-of-words embedder wired into ``services.semantic_search``,
``services.perf_metrics`` enabled) and returns a metrics dict. The runner
attaches the perf_metrics span snapshot and wall clock afterwards.

Five scenarios:

  - ``chat_short``    5-turn single-agent conversation through the REAL
                      ChatOrchestrator with fake clients injected.
  - ``chat_long``     Phase 3: 30 turns that overflow an 8,000-char history
                      budget, run in four flag configs (off / history caching /
                      compaction / both) reporting the billed-input proxy.
  - ``rag_heavy``     ~2,000 fixture chunks through the production ingest +
                      indexer path, then 50 hybrid queries.
  - ``team_pipeline`` Phase 5: a 6-subtask mixed-dependency plan through the
                      REAL PipelineExecutor, run sequential then parallel,
                      reporting the wall-clock ratio and token parity.
  - ``routing``       200 seeded trajectories, then 100 routing-recall
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

    Perf Phase 4: the scenario also enables ``cost_prediction_enabled`` and
    captures each turn's ``cost_predicted`` event, reporting
    ``prediction_mape`` — the mean absolute percentage error of the
    predicted vs actual TOTAL input tokens (actual = input + cache read +
    cache creation, since the API's input_tokens excludes cached tokens).
    The fixture tokenizer is also chars//4, so this gate verifies PLUMBING
    correctness (the predictor saw the same final prompt the worker was
    sent — any drift between the two shows up as error), NOT real-world
    tokenizer accuracy. The small residual comes from role labels and wire
    separators the heuristic deliberately ignores.
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
        # Perf Phase 4: heuristic prediction per turn (block flag stays off
        # — the scenario measures accuracy, not enforcement). Prediction is
        # read-only + one SSE event, so every pre-existing metric is
        # unchanged.
        "cost_prediction_enabled": True,
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
    prediction_apes: list[float] = []
    for i, turn in enumerate(fixture["turns"], start=1):
        predictions: list[dict] = []

        def _on_event(event_type: str, data: dict) -> None:
            if event_type == "cost_predicted":
                predictions.append(dict(data))

        result = orchestrator.send(conversation_id, turn["user"],
                                   on_event=_on_event)
        # Cache accounting comes from token_usage (ChatResult doesn't carry
        # the cache columns) — TurnLifecycle.close just wrote this turn's row.
        usage_row = db.fetchone(
            "SELECT cache_read_tokens, cache_creation_tokens FROM token_usage "
            "WHERE conversation_id = ? ORDER BY rowid DESC LIMIT 1",
            (conversation_id,),
        )
        cache_read = usage_row["cache_read_tokens"] if usage_row else 0
        cache_creation = usage_row["cache_creation_tokens"] if usage_row else 0

        # Predicted vs actual TOTAL input. ``est_input + est_cached`` mirrors
        # ``input + read + creation`` — both sides count the full prompt.
        predicted_input = (
            predictions[0]["est_input_tokens"] + predictions[0]["est_cached_tokens"]
            if predictions else 0
        )
        actual_input = result.tokens_in + cache_read + cache_creation
        ape_pct = (
            round(100.0 * abs(predicted_input - actual_input) / actual_input, 4)
            if actual_input > 0 else 0.0
        )
        prediction_apes.append(ape_pct)

        turns.append({
            "turn": i,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "cost_usd": round(result.cost_usd, 8),
            "model": result.model,
            "route_reason": result.route_reason,
            "predicted_input_tokens": predicted_input,
            "prediction_ape_pct": ape_pct,
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
        # Perf Phase 4: input-side prediction error — plumbing correctness
        # gate (see docstring), deterministic by construction.
        "prediction_mape": round(
            sum(prediction_apes) / len(prediction_apes), 4,
        ) if prediction_apes else 0.0,
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


# ── team_pipeline ─────────────────────────────────────────────────────────────

def run_team_pipeline() -> dict:
    """A 6-subtask team pipeline run TWICE — sequential vs parallel (Phase 5).

    The fixture plan has 4 independent steps plus two dependent ones (step 5
    needs 1+2, step 6 needs 5). Both runs drive the REAL PipelineExecutor +
    HubRouter over the isolated env, against a seeded coordinator + 6
    specialists. Specialists are claude-routed (FakeClaudeClient, simulated
    500 ms/call) so ``pipeline_max_concurrency=3`` is actually exercised;
    the coordinator's decomposition + synthesis are local-routed
    (FakeLocalClient, simulated 300 ms/call) — mirroring the honest speedup
    claim: local inference is single-stream, the parallel win comes from
    Claude-routed subtasks.

    Token parity (``tokens_identical``): parallelism must not change spend.
    Two fixture properties make the per-call prompts byte-identical across
    modes so the assertion is exact:
      - every specialist artifact exceeds MAX_UPSTREAM_CONTEXT_CHARS, so the
        upstream-context builder injects NOTHING in either mode (sequential
        all-prior context and parallel deps-only context both come out
        empty);
      - specialist replies are keyed on the TASK-N marker in each step's
        description, so concurrent arrival order cannot shuffle which step
        gets which reply.
    The decomposition PROMPT does differ between modes by design
    (DECOMPOSITION_PROMPT_PARALLEL adds the depends_on rule), but the
    decomposition runs on the local backend, whose fake charges input by
    reply length — and the production accounting difference is one constant
    prompt, not a per-step cost.

    ``parallel_over_sequential_ratio`` is latency-class (wall clock) even
    though its name doesn't end in ``_ms`` — runner.deterministic_view
    strips ``_ratio`` keys for the same reason.
    """
    import time as _time

    import db
    from services.hub_router import HubRouter
    from services.pipeline import PipelineExecutor

    from benchmarks.perf.fake_clients import FakeClaudeClient, FakeLocalClient

    fixture = load_fixture("team_pipeline")
    now = "2026-01-01T00:00:00+00:00"

    # Seed the real rows PipelineExecutor.run reads: the coordinator + 6
    # specialist agents, the team row, and the membership rows. model
    # preference is the routing key — HubRouter.route_for_agent resolves
    # "local"/"claude" into the backend each step invokes. thinking_budget
    # is pinned to 0 (the column defaults to 2048) so the local coordinator
    # takes the plain chat path instead of qwen_thinking.
    db.execute(
        "INSERT INTO agents (id, name, description, system_prompt, "
        "model_preference, role, is_builtin, skills, thinking_budget, "
        "created_at, updated_at) "
        "VALUES (?, ?, '', ?, 'local', 'coordinator', 0, '[]', 0, ?, ?)",
        (fixture["coordinator_id"], "Coordinator",
         fixture["coordinator_system_prompt"], now, now),
    )
    for spec in fixture["specialists"]:
        db.execute(
            "INSERT INTO agents (id, name, description, system_prompt, "
            "model_preference, role, is_builtin, skills, thinking_budget, "
            "created_at, updated_at) "
            "VALUES (?, ?, '', ?, 'claude', ?, 0, '[]', 0, ?, ?)",
            (spec["id"], spec["name"], spec["system_prompt"], spec["role"],
             now, now),
        )
    db.execute(
        "INSERT INTO agent_teams (id, name, description, coordinator_id, "
        "created_at, updated_at) VALUES (?, 'Perf Team', '', ?, ?, ?)",
        (fixture["team_id"], fixture["coordinator_id"], now, now),
    )
    for i, spec in enumerate(fixture["specialists"]):
        db.execute(
            "INSERT INTO agent_team_members (team_id, agent_id, role, "
            "sort_order) VALUES (?, ?, 'worker', ?)",
            (fixture["team_id"], spec["id"], i),
        )
    db.commit()

    keyed_replies = {
        s["reply_key"]: s["reply"] for s in fixture["specialists"]
    }

    def _run_mode(parallel: bool) -> dict:
        # Fresh fake clients per mode so reply cursors and call logs don't
        # leak between runs; settings is a plain dict like the other
        # scenarios (every collaborator reads via ``settings.get``).
        settings = {
            "pipeline_parallel_enabled": parallel,
            "pipeline_max_concurrency": 3,
            "pipeline_local_concurrency": 1,
            "debate_enabled": False,
        }
        claude = FakeClaudeClient(
            replies=["unused — every specialist reply is keyed"],
            keyed_replies=keyed_replies,
            simulated_latency_ms=float(fixture["claude_latency_ms"]),
        )
        local = FakeLocalClient(
            replies=[fixture["decomposition_reply"], fixture["synthesis_reply"]],
            simulated_latency_ms=float(fixture["local_latency_ms"]),
        )
        hub = HubRouter(claude, local, settings)
        executor = PipelineExecutor(hub, settings)

        started = _time.perf_counter()
        result = executor.run(
            team_id=fixture["team_id"],
            user_message=fixture["user_message"],
            conversation_id=f"perf-team-{'parallel' if parallel else 'sequential'}",
            history=[],
        )
        wall_clock = round((_time.perf_counter() - started) * 1000.0, 3)

        return {
            "steps": len(result.steps),
            "validation_passed_steps": sum(
                1 for s in result.steps if s["validation_passed"]
            ),
            "total_tokens_in": result.total_tokens_in,
            "total_tokens_out": result.total_tokens_out,
            "synthesis_chars": len(result.synthesis),
            "specialist_calls": len(claude.calls),
            "wall_clock_ms": wall_clock,
        }

    sequential = _run_mode(parallel=False)
    parallel = _run_mode(parallel=True)

    tokens_identical = int(
        sequential["total_tokens_in"] == parallel["total_tokens_in"]
        and sequential["total_tokens_out"] == parallel["total_tokens_out"]
    )

    return {
        "sequential": sequential,
        "parallel": parallel,
        # 1/0 so the deterministic gate can express ``min: 1``.
        "tokens_identical": tokens_identical,
        "parallel_over_sequential_ratio": _safe_rate(
            parallel["wall_clock_ms"], sequential["wall_clock_ms"],
        ),
    }


# ── routing ───────────────────────────────────────────────────────────────────

def run_routing() -> dict:
    """Seed 200 trajectories, then make 100 routing-recall decisions.

    Each decision performs the two reads the router makes in production:
    ``find_similar`` (what the explain path uses) and ``bias_table`` (what
    ``HubRouter._trajectory_rates`` consumes). Latency rides in the
    ``traj_find_similar`` / ``traj_bias_table`` spans; the recall counts are
    the deterministic part.

    Perf Phase 6 extension: after the raw-recall decisions, the scenario
    consolidates the 200 trajectories into routing hints and replays the same
    100 queries through ``hint_table`` (the ``traj_hint_table`` span lives
    inside it). It reports ``hints_created`` plus a **routing-regret**
    comparison across three guidance modes — none (a fixed fallback pick),
    raw ``bias_table`` bias, and consolidated hints with support damping.
    Regret = the fraction of the 100 decisions that pick an agent whose
    seeded ground-truth success rate for the query's task family (the
    fixture's verb prefix) is lower than the best agent's. The two ``…_le_…``
    metrics encode the expected ordering (hints ≤ raw ≤ none) as 1/0 gates.

    Scenario-level ``merge_sim`` (NOT the production default): the
    bag-of-words embedder scores same-family paraphrases around 0.37 under
    the monotonic hint mapping (cross-family pairs land at 0), so 0.3 makes
    clusters form per task family the way real embeddings form them at the
    0.75 default. Same spirit as the relaxed min_sim above; min_cluster
    stays at the production default (3).
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

    # Ground truth: per-task-family (verb prefix) per-agent binary success
    # rate over the SEEDED data — the oracle the regret metric scores
    # against. Deterministic by construction (pure fixture arithmetic).
    family_stats: dict[str, dict[str, list[int]]] = {}
    agents: set[str] = set()
    for t in fixture["trajectories"]:
        family = t["task_text"].split()[0]
        agent = t["agent_id"]
        agents.add(agent)
        ok = int(t["quality_verdict"] == "success" and not t["had_error"])
        stat = family_stats.setdefault(family, {}).setdefault(agent, [0, 0])
        stat[0] += ok
        stat[1] += 1
    ground_truth = {
        family: {a: s[0] / s[1] for a, s in per_agent.items()}
        for family, per_agent in family_stats.items()
    }
    fallback_agent = sorted(agents)[0]  # deterministic no-guidance pick

    def _regret(query: str, picked: str) -> int:
        rates = ground_truth.get(query.split()[0], {})
        if not rates:
            return 0
        best = max(rates.values())
        return int(rates.get(picked, 0.0) < best - 1e-9)

    def _argmax_agent(table: dict[str, float]) -> str:
        if not table:
            return fallback_agent
        # Ties break on agent id so the pick is order-independent.
        return max(sorted(table), key=lambda a: table[a])

    decisions = 0
    similar_hits = 0
    bias_tables_nonempty = 0
    agents_biased = 0
    regret_none = 0
    regret_raw = 0
    raw_disagreements = 0
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
        # Regret bookkeeping for the no-guidance and raw-bias modes (the raw
        # mode reuses the bias_table just computed — pre-consolidation, so
        # the full 200-trajectory store backs it).
        regret_none += _regret(query, fallback_agent)
        raw_pick = _argmax_agent(table)
        regret_raw += _regret(query, raw_pick)
        if raw_pick != fallback_agent:
            raw_disagreements += 1

    # ── Perf Phase 6: consolidation + hint-guided regret ─────────────────────
    consolidation_settings = {
        "trajectory_consolidation_min_cluster": 3,
        "trajectory_hint_merge_sim": 0.3,
        "trajectory_hint_max_age_days": 90,
    }
    trajectories_consolidated = trajectory_store.consolidate(
        consolidation_settings)
    import db
    hints_created = db.fetchone("SELECT COUNT(*) AS c FROM routing_hints")["c"]
    vec_rows_remaining = db.fetchone(
        "SELECT COUNT(*) AS c FROM vec_trajectories_map")["c"]

    regret_hints = 0
    hint_tables_nonempty = 0
    for query in fixture["queries"]:
        # traj_hint_table span is recorded INSIDE hint_table.
        hints = trajectory_store.hint_table(query, top_k=5, min_sim=0.3)
        if hints:
            hint_tables_nonempty += 1
        # Hinted agents: support-damped quality (mirrors HubRouter's
        # _apply_hint_bias scaling); unhinted agents fall back to the
        # residual raw bias over the unconsolidated remainder.
        effective: dict[str, float] = {
            agent: 0.5 + (quality - 0.5) * min(1.0, support / 5.0)
            for agent, (quality, support) in hints.items()
        }
        residual = trajectory_store.bias_table(query, top_k=5, min_sim=0.3)
        for agent, rate in residual.items():
            effective.setdefault(agent, rate)
        regret_hints += _regret(query, _argmax_agent(effective))

    n = decisions or 1
    return {
        "trajectories_seeded": seeded,
        "decision_count": decisions,
        "similar_hits": similar_hits,
        "bias_tables_nonempty": bias_tables_nonempty,
        "agents_biased": agents_biased,
        # Perf Phase 6 deterministic metrics.
        "hints_created": hints_created,
        "trajectories_consolidated": trajectories_consolidated,
        "vec_rows_remaining": vec_rows_remaining,
        "hint_tables_nonempty": hint_tables_nonempty,
        "raw_pick_disagreements": raw_disagreements,
        "routing_regret_none": _safe_rate(regret_none, n),
        "routing_regret_raw": _safe_rate(regret_raw, n),
        "routing_regret_hints": _safe_rate(regret_hints, n),
        # 1/0 ordering gates (hints ≤ raw ≤ none) for perf_thresholds.json.
        "regret_hints_le_raw": int(regret_hints <= regret_raw),
        "regret_raw_le_none": int(regret_raw <= regret_none),
    }


# ── Registry ──────────────────────────────────────────────────────────────────

SCENARIOS = {
    "chat_short": run_chat_short,
    "chat_long": run_chat_long,
    "rag_heavy": run_rag_heavy,
    "team_pipeline": run_team_pipeline,
    "routing": run_routing,
}
