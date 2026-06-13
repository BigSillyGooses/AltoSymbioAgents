"""
services/perf_metrics.py — Tiny in-process span recorder.

Phase 1 (perf upgrade plan): the benchmark harness and tests need wall-clock
visibility into hot paths (hybrid retrieval, model invocation, pipeline
steps) without adding a user-facing setting or a metrics dependency.

Design:
  - Module-level singleton; DISABLED by default. ``record()`` is a no-op
    unless ``enable()`` was called, so production turns pay only a single
    boolean check per instrumented call site.
  - NOT a user setting — only the perf harness (benchmarks/perf) and tests
    flip it on. Nothing here is persisted.
  - Every public function is guaranteed never to raise: instrumentation
    must never break the call it wraps. Call sites can therefore use the
    one-liner ``with perf_metrics.span("name"):`` without their own
    try/except.

Usage:
    from services import perf_metrics

    perf_metrics.enable()
    with perf_metrics.span("hybrid_search"):
        ...work...
    stats = perf_metrics.snapshot()
    # {"hybrid_search": {"count": 1, "total_ms": 12.3, "avg_ms": 12.3}}
"""

from __future__ import annotations

import contextlib
import threading
import time

_lock = threading.Lock()
_enabled = False
# span name -> [count, total_ms]
_spans: dict[str, list] = {}


def enable() -> None:
    """Turn recording on (harness/tests only)."""
    global _enabled
    _enabled = True


def disable() -> None:
    """Turn recording off. Recorded data is kept until reset()."""
    global _enabled
    _enabled = False


def is_enabled() -> bool:
    return _enabled


def record(span: str, ms: float) -> None:
    """Record one timing sample. No-op unless enabled; never raises."""
    if not _enabled:
        return
    try:
        with _lock:
            entry = _spans.get(span)
            if entry is None:
                _spans[span] = [1, float(ms)]
            else:
                entry[0] += 1
                entry[1] += float(ms)
    except Exception:
        pass  # instrumentation must never break the instrumented call


def snapshot() -> dict[str, dict]:
    """Return {span: {count, total_ms, avg_ms}} for everything recorded."""
    try:
        with _lock:
            out: dict[str, dict] = {}
            for name, (count, total_ms) in _spans.items():
                out[name] = {
                    "count": count,
                    "total_ms": round(total_ms, 3),
                    "avg_ms": round(total_ms / count, 3) if count else 0.0,
                }
            return out
    except Exception:
        return {}


def reset() -> None:
    """Drop all recorded samples (does not change enabled state)."""
    try:
        with _lock:
            _spans.clear()
    except Exception:
        pass


@contextlib.contextmanager
def span(name: str):
    """Context manager timing a block into ``record(name, elapsed_ms)``.

    Records on both normal exit and exception (the exception still
    propagates — only the timing is best-effort).
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        record(name, (time.perf_counter() - start) * 1000.0)
