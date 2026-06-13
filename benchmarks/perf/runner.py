"""
benchmarks/perf/runner.py — Isolated-environment setup + scenario orchestration.

``isolated_env()`` replicates what the backend test suite assembles from
fixtures — ``in_memory_db`` (tests/conftest.py) plus ``vector_env``
(tests/test_trajectory_store.py) — without pytest:

  - a fresh temp-file SQLite DB through ``db.init_db`` (full schema +
    migrations + sqlite-vec virtual tables),
  - the deterministic bag-of-words embedder monkeypatched into
    ``services.semantic_search`` (``_embed_fn`` / ``_embed_dim`` /
    ``_initialized``) exactly where fastembed would sit,
  - cleared BM25 module state (it is module-global in semantic_search),
  - ``services.perf_metrics`` reset + enabled.

Everything is saved and restored on exit so the host process (pytest, or a
multi-scenario CLI run) is untouched — which is also what makes two
consecutive ``run_scenario`` calls start from identical state and therefore
produce identical token/cache/cost numbers.

Determinism contract: only span timings and wall clock may differ between
runs. ``deterministic_view()`` strips exactly those fields so callers can
assert equality on the rest.
"""

from __future__ import annotations

import contextlib
import copy
import tempfile
import time
from pathlib import Path

from benchmarks.perf import scenarios
from benchmarks.perf.embedder import EMBED_DIM, deterministic_embed


@contextlib.contextmanager
def isolated_env():
    """Fresh DB + deterministic embedder + perf metrics, restored on exit."""
    import db
    from services import perf_metrics, semantic_search

    saved_db = (db._conn, db._db_path)
    saved_semantic = (
        semantic_search._embed_fn,
        semantic_search._embed_dim,
        semantic_search._initialized,
    )
    saved_bm25 = (
        semantic_search._bm25_index,
        semantic_search._bm25_doc_ids,
        semantic_search._bm25_corpus,
        semantic_search._bm25_contents,
    )
    saved_perf_enabled = perf_metrics.is_enabled()

    tmp = tempfile.TemporaryDirectory(prefix="alto-perf-bench-")
    try:
        # DB: same shape as the in_memory_db fixture — point the module at a
        # fresh temp file and let init_db build the schema + run migrations.
        db._conn = None
        db._db_path = None
        db.init_db(Path(tmp.name) / "perf.db")

        # Vector store: same wiring as the vector_env fixture.
        semantic_search._embed_fn = deterministic_embed
        semantic_search._embed_dim = EMBED_DIM
        semantic_search._initialized = True

        # BM25 state is module-global; a previous scenario (or the host test
        # process) may have populated it. Start empty, like a fresh process.
        semantic_search._bm25_index = None
        semantic_search._bm25_doc_ids = []
        semantic_search._bm25_corpus = []
        semantic_search._bm25_contents = {}

        perf_metrics.reset()
        perf_metrics.enable()
        yield
    finally:
        if not saved_perf_enabled:
            perf_metrics.disable()
        perf_metrics.reset()

        if db._conn is not None:
            try:
                db._conn.close()
            except Exception:
                pass
        db._conn, db._db_path = saved_db

        (
            semantic_search._embed_fn,
            semantic_search._embed_dim,
            semantic_search._initialized,
        ) = saved_semantic
        (
            semantic_search._bm25_index,
            semantic_search._bm25_doc_ids,
            semantic_search._bm25_corpus,
            semantic_search._bm25_contents,
        ) = saved_bm25

        tmp.cleanup()


def run_scenario(name: str) -> dict:
    """Run one scenario in an isolated environment and return its metrics.

    The returned dict carries the scenario's deterministic metrics plus two
    latency-class fields: ``spans`` (the perf_metrics snapshot — count /
    total_ms / avg_ms per instrumented span) and ``wall_clock_ms``.
    """
    fn = scenarios.SCENARIOS.get(name)
    if fn is None:
        raise KeyError(
            f"Unknown scenario {name!r}; choose from {sorted(scenarios.SCENARIOS)}"
        )
    from services import perf_metrics

    with isolated_env():
        started = time.perf_counter()
        metrics = fn()
        metrics["spans"] = perf_metrics.snapshot()
        metrics["wall_clock_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    metrics["scenario"] = name
    return metrics


def run_all() -> dict:
    """Run every registered scenario. Returns {scenario_name: metrics}."""
    return {name: run_scenario(name) for name in scenarios.SCENARIOS}


# Latency-class fields excluded from the determinism contract: the spans
# snapshot (its *_ms values vary run to run; its counts do not, but the dict
# is dropped wholesale for simplicity), anything ending in ``_ms``, and
# ``_ratio`` fields derived from wall clocks (team_pipeline's
# parallel_over_sequential_ratio).
_LATENCY_KEY = "spans"
_LATENCY_SUFFIXES = ("_ms", "_ratio")


def deterministic_view(metrics: dict) -> dict:
    """Deep-copy ``metrics`` minus the latency-class fields.

    Two consecutive ``run_scenario`` calls must produce EQUAL deterministic
    views — tests/test_perf_harness.py asserts it, and the ``deterministic``
    gate class in benchmarks/perf_thresholds.json relies on it.
    """
    def _strip(node):
        if isinstance(node, dict):
            return {
                k: _strip(v) for k, v in node.items()
                if k != _LATENCY_KEY and not k.endswith(_LATENCY_SUFFIXES)
            }
        if isinstance(node, list):
            return [_strip(v) for v in node]
        return node

    return _strip(copy.deepcopy(metrics))
