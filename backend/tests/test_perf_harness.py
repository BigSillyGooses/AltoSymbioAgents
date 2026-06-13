"""
tests/test_perf_harness.py — Perf Phase 1b: benchmark harness smoke tests.

Two concerns:
  - the harness runs in-process and keeps its determinism contract (two
    consecutive runs produce identical token/cache/cost numbers), and
  - FakeClaudeClient's prefix-cache simulation enforces the real Anthropic
    rules (byte-prefix matching, miss-on-mutation, minimum cacheable
    prefix) that Phase 3's history caching will be graded against.

The harness lives at the repo root (benchmarks/perf) while pytest's rootdir
is backend/, hence the sys.path insert below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── chat_short end-to-end ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def chat_short_runs():
    """Run chat_short twice in-process (module-scoped: it's the slow part)."""
    from benchmarks.perf import runner
    return runner.run_scenario("chat_short"), runner.run_scenario("chat_short")


def test_chat_short_metric_structure(chat_short_runs):
    metrics, _ = chat_short_runs
    for key in (
        "scenario", "turn_count", "turns", "totals",
        "tokens_in_per_turn", "tokens_out_per_turn",
        "cache_hit_rate", "cost_per_turn_usd", "spans",
    ):
        assert key in metrics, f"missing metric key: {key}"
    assert metrics["turn_count"] == 5
    assert {"tokens_in", "tokens_out", "cache_read_tokens",
            "cache_creation_tokens", "cost_usd"} <= set(metrics["totals"])
    # The instrumented spans must have fired through the real services.
    assert metrics["spans"]["model_call"]["count"] == 5


def test_chat_short_cache_accounting(chat_short_runs):
    """Turn 1 writes the system-prompt prefix; later turns read it back."""
    metrics, _ = chat_short_runs
    turns = metrics["turns"]
    assert turns[0]["cache_creation_tokens"] > 0
    assert turns[0]["cache_read_tokens"] == 0
    for t in turns[1:]:
        assert t["cache_read_tokens"] > 0
        assert t["cache_creation_tokens"] == 0
    assert metrics["cache_hit_rate"] > 0.5


def test_chat_short_is_deterministic(chat_short_runs):
    from benchmarks.perf import runner
    first, second = chat_short_runs
    assert runner.deterministic_view(first) == runner.deterministic_view(second)


# ── FakeClaudeClient cache simulation ─────────────────────────────────────────


def _client(min_prefix_tokens: int = 8):
    """Client with a tiny min-prefix floor so rules are testable without
    2048-token fixtures. Rule 3 itself is covered explicitly below."""
    from benchmarks.perf.fake_clients import FakeClaudeClient
    return FakeClaudeClient(
        replies=["reply"], min_cacheable_prefix_tokens=min_prefix_tokens,
    )


SYSTEM = "You are a meticulous test assistant. " * 4  # well over 8 fixture tokens


def test_fake_cache_first_call_writes_second_reads():
    client = _client()
    messages = [{"role": "user", "content": "hello"}]

    first = client.chat_unified(SYSTEM, messages)
    assert first["cache_creation_tokens"] > 0
    assert first["cache_read_tokens"] == 0

    second = client.chat_unified(SYSTEM, messages + [
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "more"},
    ])
    assert second["cache_read_tokens"] == first["cache_creation_tokens"]
    assert second["cache_creation_tokens"] == 0


def test_fake_cache_prefix_mutation_is_a_miss():
    client = _client()
    messages = [{"role": "user", "content": "hello"}]
    client.chat_unified(SYSTEM, messages)

    mutated = client.chat_unified(SYSTEM + " v2", messages)
    assert mutated["cache_read_tokens"] == 0
    assert mutated["cache_creation_tokens"] > 0


def test_fake_cache_below_min_prefix_is_silently_ignored():
    # System prompt far below the floor: the marker must report neither
    # creation nor read, and all tokens bill as plain input (rule 3).
    client = _client(min_prefix_tokens=10_000)
    result = client.chat_unified(SYSTEM, [{"role": "user", "content": "hello"}])
    assert result["cache_creation_tokens"] == 0
    assert result["cache_read_tokens"] == 0
    assert result["input_tokens"] > 0
    repeat = client.chat_unified(SYSTEM, [{"role": "user", "content": "hello"}])
    assert repeat["cache_read_tokens"] == 0


def test_fake_cache_usage_splits_total():
    """input + creation + read must always equal the rendered request size."""
    from benchmarks.perf.fake_clients import count_tokens
    client = _client()
    messages = [{"role": "user", "content": "hello there friend"}]
    full_text, _, _ = client._render_request(SYSTEM, messages)
    result = client.chat_unified(SYSTEM, messages)
    total = (result["input_tokens"] + result["cache_creation_tokens"]
             + result["cache_read_tokens"])
    assert total == count_tokens(full_text)


def test_fake_local_client_reports_no_cache():
    from benchmarks.perf.fake_clients import FakeLocalClient
    client = FakeLocalClient(replies=["ok"], simulated_latency_ms=0)
    result = client.chat_unified("system", [{"role": "user", "content": "hi"}])
    assert result["cache_creation_tokens"] == 0
    assert result["cache_read_tokens"] == 0
