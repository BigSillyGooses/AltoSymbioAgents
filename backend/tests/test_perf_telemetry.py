"""
tests/test_perf_telemetry.py — Perf Phase 1: span recorder + cache telemetry.

Covers the three pieces Phase 1a added:
  - services.perf_metrics (no-op when disabled, accurate when enabled)
  - the cache-aware cost estimate (_estimate_cost_cached) and its agreement
    with _estimate_cost when no cached tokens are involved
  - TurnLifecycle.close() persisting cache_read_tokens / cache_creation_tokens
    into token_usage (and defaulting to 0 for legacy callers)
"""

from __future__ import annotations

import pytest


# ── perf_metrics ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_perf_metrics():
    from services import perf_metrics
    perf_metrics.disable()
    perf_metrics.reset()
    yield
    perf_metrics.disable()
    perf_metrics.reset()


def test_perf_metrics_disabled_is_noop():
    from services import perf_metrics
    perf_metrics.record("x", 5.0)
    with perf_metrics.span("y"):
        pass
    assert perf_metrics.snapshot() == {}


def test_perf_metrics_records_when_enabled():
    from services import perf_metrics
    perf_metrics.enable()
    perf_metrics.record("search", 10.0)
    perf_metrics.record("search", 30.0)
    snap = perf_metrics.snapshot()
    assert snap["search"]["count"] == 2
    assert snap["search"]["total_ms"] == 40.0
    assert snap["search"]["avg_ms"] == 20.0


def test_perf_metrics_span_records_on_exception():
    from services import perf_metrics
    perf_metrics.enable()
    with pytest.raises(ValueError):
        with perf_metrics.span("boom"):
            raise ValueError("expected")
    assert perf_metrics.snapshot()["boom"]["count"] == 1


def test_perf_metrics_reset_clears_samples():
    from services import perf_metrics
    perf_metrics.enable()
    perf_metrics.record("a", 1.0)
    perf_metrics.reset()
    assert perf_metrics.snapshot() == {}


# ── cache-aware cost estimate ─────────────────────────────────────────────────

def test_cached_cost_matches_plain_cost_with_zero_cache_tokens():
    from services.chat_orchestrator import _estimate_cost, _estimate_cost_cached
    model = "claude-sonnet-4-6"
    plain = _estimate_cost(model, 1000, 500, None)
    cached = _estimate_cost_cached(model, 1000, 500, 0, 0, None)
    assert cached == pytest.approx(plain)


def test_cached_cost_applies_anthropic_multipliers():
    from core.model_catalog import get_catalog
    from services.chat_orchestrator import _estimate_cost_cached
    model = "claude-sonnet-4-6"
    price_in, price_out = get_catalog().prices_for_model(model, None)
    got = _estimate_cost_cached(model, 1000, 500, 2000, 4000, None)
    expected = (
        1000 * price_in
        + 2000 * price_in * 0.1     # cache reads
        + 4000 * price_in * 1.25    # cache writes
        + 500 * price_out
    ) / 1_000_000
    assert got == pytest.approx(expected)


def test_cached_cost_is_zero_for_local_models():
    from services.chat_orchestrator import _estimate_cost_cached
    assert _estimate_cost_cached("qwen3:8b", 1000, 500, 100, 100, None) == 0.0


# ── token_usage persistence ───────────────────────────────────────────────────

def _open_turn(in_memory_db, conversation_id="conv-perf"):
    import db
    from services.turn_context import TurnContext
    from services.turn_lifecycle import TurnLifecycle

    db.execute(
        "INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)",
        (conversation_id, "perf", "2026-01-01T00:00:00+00:00"),
    )
    db.commit()
    lifecycle = TurnLifecycle(settings={"max_conversation_budget_usd": 5.0})
    ctx = TurnContext(conversation_id=conversation_id, user_message="hello there")
    assert lifecycle.open(ctx) is True
    return lifecycle, ctx


def test_close_persists_cache_token_columns(in_memory_db):
    import db
    lifecycle, ctx = _open_turn(in_memory_db)
    lifecycle.close(
        ctx,
        asst_msg_id="am-1",
        response_text="hi",
        route_reason="test",
        model_name="claude-sonnet-4-6",
        tokens_in=100,
        tokens_out=50,
        cost=0.001,
        cache_read_tokens=2000,
        cache_creation_tokens=300,
    )
    row = db.fetchone(
        "SELECT cache_read_tokens, cache_creation_tokens FROM token_usage "
        "WHERE conversation_id = ?",
        (ctx.conversation_id,),
    )
    assert row["cache_read_tokens"] == 2000
    assert row["cache_creation_tokens"] == 300


def test_close_defaults_cache_columns_to_zero(in_memory_db):
    import db
    lifecycle, ctx = _open_turn(in_memory_db, conversation_id="conv-perf-2")
    lifecycle.close(
        ctx,
        asst_msg_id="am-2",
        response_text="hi",
        route_reason="test",
        model_name="claude-sonnet-4-6",
        tokens_in=10,
        tokens_out=5,
        cost=0.0001,
    )
    row = db.fetchone(
        "SELECT cache_read_tokens, cache_creation_tokens FROM token_usage "
        "WHERE conversation_id = ?",
        (ctx.conversation_id,),
    )
    assert row["cache_read_tokens"] == 0
    assert row["cache_creation_tokens"] == 0
