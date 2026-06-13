"""
tests/test_agentdojo_perf_overlay.py — Phase 7 part 1: AgentDojo efficiency
overlay. No API key, no agentdojo package needed — covers the pure helpers:

  - per-task perf collection (UsageCollector + the token_usage summing helper
    against a real in_memory_db)
  - suite-level aggregation math, including the cache hit rate
  - generate_benchmarks_md.py rendering: old-shape suite JSONs stay
    byte-identical; the "Efficiency (same runs)" table appears only when the
    perf block is present
  - eval_stats helpers are indifferent to records carrying the new perf keys
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.agentdojo.perf_overlay import (
    UsageCollector,
    aggregate_suite_perf,
    cache_hit_rate,
    estimate_cost_usd,
    parse_enable_flags,
    task_perf_from_token_usage,
    wrap_anthropic_client,
)
from tests.eval_stats import bootstrap_ci, paired_diff_ci

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── UsageCollector ────────────────────────────────────────────────────────────


def test_usage_collector_accumulates_objects_and_dicts():
    c = UsageCollector()
    c.record_usage(SimpleNamespace(
        input_tokens=100, output_tokens=20,
        cache_read_input_tokens=30, cache_creation_input_tokens=40,
    ))
    c.record_usage({"input_tokens": 1, "output_tokens": 2})  # cache keys absent
    c.record_usage(None)  # ignored, not an API call
    assert c.api_calls == 2
    assert c.tokens_in == 101
    assert c.tokens_out == 22
    assert c.cache_read_tokens == 30
    assert c.cache_creation_tokens == 40


def test_usage_collector_tolerates_none_and_junk_fields():
    c = UsageCollector()
    c.record_usage(SimpleNamespace(input_tokens=None, output_tokens="junk"))
    assert (c.tokens_in, c.tokens_out) == (0, 0)
    assert c.api_calls == 1


def test_task_perf_shape_and_cost_math():
    from core.model_catalog import get_catalog

    model = "claude-sonnet-4-6"
    c = UsageCollector()
    c.record_usage(SimpleNamespace(
        input_tokens=1000, output_tokens=500,
        cache_read_input_tokens=2000, cache_creation_input_tokens=4000,
    ))
    perf = c.task_perf(model=model, wall_clock_s=12.3456)

    price_in, price_out = get_catalog().prices_for_model(model, None)
    expected_cost = (
        1000 * price_in
        + 2000 * price_in * 0.1
        + 4000 * price_in * 1.25
        + 500 * price_out
    ) / 1_000_000
    assert set(perf) == {
        "tokens_in", "tokens_out", "cache_read_tokens",
        "cache_creation_tokens", "cost_usd", "wall_clock_s",
    }
    assert perf["tokens_in"] == 1000
    assert perf["tokens_out"] == 500
    assert perf["cache_read_tokens"] == 2000
    assert perf["cache_creation_tokens"] == 4000
    assert perf["cost_usd"] == pytest.approx(expected_cost, abs=1e-6)
    assert perf["wall_clock_s"] == 12.346


def test_estimate_cost_is_zero_for_non_claude_models():
    assert estimate_cost_usd("llama3:8b", 10_000, 5_000, 100, 100) == 0.0
    assert estimate_cost_usd("", 10_000, 5_000) == 0.0


def test_wrap_anthropic_client_records_and_delegates():
    class _Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=7, output_tokens=3,
                    cache_read_input_tokens=2, cache_creation_input_tokens=1,
                ),
                content=[],
            )

        def stream(self, **kwargs):
            return "stream-handle"

    class _Client:
        def __init__(self):
            self.messages = _Messages()
            self.api_key = "k"

    inner = _Client()
    collector = UsageCollector()
    wrapped = wrap_anthropic_client(inner, collector)

    resp = wrapped.messages.create(model="m", max_tokens=8)
    assert inner.messages.calls == [{"model": "m", "max_tokens": 8}]
    assert resp.usage.input_tokens == 7  # response passes through untouched
    assert (collector.tokens_in, collector.tokens_out) == (7, 3)
    assert (collector.cache_read_tokens, collector.cache_creation_tokens) == (2, 1)
    # Non-create attributes delegate to the real client.
    assert wrapped.api_key == "k"
    assert wrapped.messages.stream() == "stream-handle"


# ── token_usage summing helper (stack-driven runs) ────────────────────────────


def test_task_perf_from_token_usage_sums_seeded_rows(in_memory_db):
    db = in_memory_db
    rows = [
        # (id, conversation_id, tokens_in, tokens_out, cost, cache_r, cache_c)
        ("u1", "conv-1", 100, 50, 0.01, 200, 300),
        ("u2", "conv-1", 10, 5, 0.001, 0, 0),
        ("u3", "conv-2", 1000, 500, 0.1, 50, 25),
        ("u4", "conv-other", 9999, 9999, 9.9, 9, 9),  # must NOT be counted
    ]
    for rid, conv, t_in, t_out, cost, c_read, c_create in rows:
        db.execute(
            "INSERT INTO token_usage (id, conversation_id, model, tokens_in, "
            "tokens_out, cost_usd, cache_read_tokens, cache_creation_tokens, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, conv, "claude-sonnet-4-6", t_in, t_out, cost,
             c_read, c_create, "2026-01-01T00:00:00"),
        )
    db.commit()

    perf = task_perf_from_token_usage(
        db, ["conv-1", "conv-2"], wall_clock_s=2.5,
    )
    assert perf["tokens_in"] == 1110
    assert perf["tokens_out"] == 555
    assert perf["cache_read_tokens"] == 250
    assert perf["cache_creation_tokens"] == 325
    assert perf["cost_usd"] == pytest.approx(0.111)
    assert perf["wall_clock_s"] == 2.5


def test_task_perf_from_token_usage_empty_conversations(in_memory_db):
    perf = task_perf_from_token_usage(in_memory_db, [], wall_clock_s=1.0)
    assert perf == {
        "tokens_in": 0, "tokens_out": 0, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "cost_usd": 0.0, "wall_clock_s": 1.0,
    }


# ── suite-level aggregation ───────────────────────────────────────────────────


def _per_task_fixture() -> list[dict]:
    return [
        {
            "user_task": "ut0", "injection_task": "it0",
            "utility": True, "asr": False,
            "perf": {
                "tokens_in": 1000, "tokens_out": 200,
                "cache_read_tokens": 3000, "cache_creation_tokens": 1000,
                "cost_usd": 0.02, "wall_clock_s": 10.0,
            },
        },
        {
            "user_task": "ut0", "injection_task": "it1",
            "utility": False, "asr": False,
            "perf": {
                "tokens_in": 2000, "tokens_out": 400,
                "cache_read_tokens": 1000, "cache_creation_tokens": 1000,
                "cost_usd": 0.04, "wall_clock_s": 14.0,
            },
        },
        # A pre-overlay / failed record without perf must be skipped quietly.
        {"user_task": "ut1", "injection_task": "it0",
         "utility": False, "asr": False, "error": "boom"},
    ]


def test_aggregate_suite_perf_totals_means_and_hit_rate():
    block = aggregate_suite_perf(
        _per_task_fixture(),
        config_name="flags-on",
        enabled_flags=["claude_history_caching", "history_compaction_enabled"],
    )
    assert block["config_name"] == "flags-on"
    assert block["enabled_flags"] == [
        "claude_history_caching", "history_compaction_enabled",
    ]
    assert block["tasks_with_perf"] == 2
    assert block["totals"] == {
        "tokens_in": 3000, "tokens_out": 600,
        "cache_read_tokens": 4000, "cache_creation_tokens": 2000,
        "cost_usd": 0.06, "wall_clock_s": 24.0,
    }
    assert block["means"] == {
        "tokens_per_task": 1800.0,       # (3000 + 600) / 2
        "cost_usd_per_task": 0.03,
        "wall_clock_s_per_task": 12.0,
    }
    # 4000 reads / (3000 uncached + 4000 reads + 2000 writes) = 4/9
    assert block["cache_hit_rate"] == pytest.approx(4000 / 9000, abs=1e-4)
    assert "spans" not in block  # only present when a snapshot was passed


def test_aggregate_suite_perf_empty_and_spans():
    block = aggregate_suite_perf([], config_name="default", enabled_flags=[])
    assert block["tasks_with_perf"] == 0
    assert block["totals"]["tokens_in"] == 0
    assert block["means"] == {
        "tokens_per_task": 0.0, "cost_usd_per_task": 0.0,
        "wall_clock_s_per_task": 0.0,
    }
    assert block["cache_hit_rate"] == 0.0

    spans = {"hybrid_search": {"count": 1, "total_ms": 5.0, "avg_ms": 5.0}}
    with_spans = aggregate_suite_perf([], spans=spans)
    assert with_spans["spans"] == spans


def test_cache_hit_rate_zero_denominator():
    assert cache_hit_rate(0, 0, 0) == 0.0


def test_parse_enable_flags():
    assert parse_enable_flags(" a, b ,a,,") == ["a", "b"]
    assert parse_enable_flags("") == []
    assert parse_enable_flags(None) == []


# ── BENCHMARKS.md rendering ───────────────────────────────────────────────────


def _load_generator():
    path = _REPO_ROOT / "build-scripts" / "generate_benchmarks_md.py"
    spec = importlib.util.spec_from_file_location(
        "generate_benchmarks_md_under_test", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _old_shape_suite(suite: str) -> dict:
    return {
        "suite": suite,
        "agentdojo_version": "1.2.1",
        "model": "claude-sonnet-4-6",
        "split_enabled": True,
        "started_at": 1750000000.0,
        "duration_seconds": 120.0,
        "total_tasks": 9,
        "utility": 88.8889,
        "asr": 0.0,
        "targeted_asr": 0.0,
        "per_task": [],
    }


_PERF_BLOCK = {
    "config_name": "default",
    "enabled_flags": [],
    "tasks_with_perf": 9,
    "totals": {
        "tokens_in": 90_000, "tokens_out": 21_600,
        "cache_read_tokens": 36_000, "cache_creation_tokens": 9_000,
        "cost_usd": 1.111104, "wall_clock_s": 85.59,
    },
    "means": {
        "tokens_per_task": 12_400.0,
        "cost_usd_per_task": 0.123456,
        "wall_clock_s_per_task": 9.51,
    },
    "cache_hit_rate": 0.2667,
}


def _run_generator(gen, bench_dir: Path, out: Path) -> str:
    thresholds = bench_dir / "thresholds.json"
    if not thresholds.exists():
        thresholds.write_text(json.dumps({
            "suites": {"workspace": {"baseline_asr_pct": 22.0}},
        }))
    rc = gen.main([
        "--benchmarks-dir", str(bench_dir),
        "--thresholds", str(thresholds),
        "--output", str(out),
    ])
    assert rc == 0
    return out.read_text()


def test_old_shape_json_renders_without_efficiency_section(tmp_path):
    gen = _load_generator()
    bench = tmp_path / "benchmarks"
    bench.mkdir()
    (bench / "workspace.json").write_text(json.dumps(_old_shape_suite("workspace")))

    output = _run_generator(gen, bench, tmp_path / "old.md")
    assert "Efficiency" not in output
    assert "| workspace | 9 | 88.89% | 0.00% | 0.00% | 22.0% |" in output


def test_perf_block_adds_efficiency_table_and_changes_nothing_else(tmp_path):
    gen = _load_generator()
    bench = tmp_path / "benchmarks"
    bench.mkdir()

    # Pass 1 — old shape.
    (bench / "workspace.json").write_text(json.dumps(_old_shape_suite("workspace")))
    old_output = _run_generator(gen, bench, tmp_path / "old.md")

    # Pass 2 — identical run, perf block added (purely additive).
    with_perf = _old_shape_suite("workspace")
    with_perf["perf"] = _PERF_BLOCK
    (bench / "workspace.json").write_text(json.dumps(with_perf))
    new_output = _run_generator(gen, bench, tmp_path / "new.md")

    assert "## Efficiency (same runs)" in new_output
    assert "| workspace | 12,400 | $0.1235 | 26.7% | 9.5s |" in new_output
    # Suites without a results.json render as placeholders in BOTH tables.
    assert "| banking | — | — | — | — |" in new_output
    assert "Perf config: `default` — enabled flags: (none)." in new_output

    # Removing the efficiency section yields byte-identical pre-Phase-7 output.
    start = new_output.index("## Efficiency (same runs)")
    end = new_output.index("## Methodology")
    assert new_output[:start] + new_output[end:] == old_output


def test_efficiency_table_renders_enabled_flags(tmp_path):
    gen = _load_generator()
    bench = tmp_path / "benchmarks"
    bench.mkdir()
    with_perf = _old_shape_suite("workspace")
    with_perf["perf"] = dict(
        _PERF_BLOCK,
        config_name="caching-on",
        enabled_flags=["claude_history_caching", "history_compaction_enabled"],
    )
    (bench / "workspace.json").write_text(json.dumps(with_perf))

    output = _run_generator(gen, bench, tmp_path / "flags.md")
    assert ("Perf config: `caching-on` — enabled flags: "
            "`claude_history_caching`, `history_compaction_enabled`.") in output


def test_efficiency_table_tolerates_partial_perf_block(tmp_path):
    gen = _load_generator()
    bench = tmp_path / "benchmarks"
    bench.mkdir()
    with_perf = _old_shape_suite("workspace")
    with_perf["perf"] = {"config_name": "default"}  # no means / hit rate
    (bench / "workspace.json").write_text(json.dumps(with_perf))

    output = _run_generator(gen, bench, tmp_path / "partial.md")
    assert "| workspace | — | — | — | — |" in output


# ── eval_stats indifference to the perf keys ──────────────────────────────────


def test_eval_stats_ignores_perf_blocks():
    """eval_stats consumes plain float lists; per-task records carrying the
    new ``perf`` dicts feed it exactly as before — the keys are simply never
    read. Mirrors how a flags-off vs flags-on comparison would use it."""
    suite_off = {"per_task": _per_task_fixture(), "perf": _PERF_BLOCK}
    walls_off = [
        t["perf"]["wall_clock_s"] for t in suite_off["per_task"]
        if isinstance(t.get("perf"), dict)
    ]
    walls_on = [w * 0.8 for w in walls_off]

    mean, lo, hi = bootstrap_ci(walls_off, n_resamples=200)
    assert lo <= mean <= hi
    assert mean == pytest.approx(12.0)

    diff, dlo, dhi = paired_diff_ci(walls_off, walls_on)
    assert diff == pytest.approx(0.2 * 12.0)
    assert dlo <= diff <= dhi
