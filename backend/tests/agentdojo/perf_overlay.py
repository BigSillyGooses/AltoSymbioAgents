"""
backend/tests/agentdojo/perf_overlay.py — efficiency telemetry for the bench.

Phase 7 part 1 (perf upgrade plan): every AgentDojo run also emits per-task
efficiency numbers — tokens in/out, prompt-cache reads/writes, estimated USD
cost, wall-clock seconds — so the weekly safety run doubles as the public
efficiency receipt, and flag-on vs flag-off configurations can be compared
on identical task suites.

Everything in this module is a pure helper: no ``agentdojo`` import, no
network, no SSE. It is imported both by the bench driver
(``run_suites.py`` / ``runner.py``) and by the runtime test suite
(``tests/test_agentdojo_perf_overlay.py``), so it must work without the
optional ``agentdojo`` package installed.

How usage is captured
---------------------

The bench pipeline does NOT go through ``ChatOrchestrator`` (and therefore
never writes ``token_usage`` rows): the Actor runs on agentdojo's own
``AnthropicLLM`` and the Reader on a bare Anthropic client. Two collection
paths are provided:

  * ``UsageCollector`` + ``wrap_anthropic_client`` — the live bench path.
    The runner wraps the Anthropic client handed to agentdojo so every
    ``messages.create`` response's ``usage`` object is accumulated; the
    Reader stage records its own response the same way.
  * ``task_perf_from_token_usage`` — for harnesses that drive the real
    chat stack (which records per-turn ``token_usage`` rows keyed by
    conversation_id), sum those rows into the same per-task perf shape.

Per-task perf shape (additive — appended to each per_task record):

    {"tokens_in": int, "tokens_out": int, "cache_read_tokens": int,
     "cache_creation_tokens": int, "cost_usd": float, "wall_clock_s": float}
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Iterable

# Make ``core`` importable for the price lookup when this module is loaded
# outside pytest (e.g. by the security-bench workflow). Mirrors runner.py.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

log = logging.getLogger("altosybioagents.bench.perf")

# Anthropic prompt-cache billing multipliers (relative to the input price):
# cache reads bill at 0.1×, cache writes (creation) at 1.25×. Duplicated from
# chat_orchestrator (same precedent as services/cost_predictor.py — one float
# each, rather than importing the whole orchestrator module into the bench).
_CACHE_READ_PRICE_MULT = 0.1
_CACHE_WRITE_PRICE_MULT = 1.25

#: Key order of a per-task perf record. Single source of truth for the
#: collectors, the aggregator, and the tests.
PERF_KEYS = (
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "wall_clock_s",
)


def estimate_cost_usd(
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Cache-aware USD estimate, mirroring chat_orchestrator's math.

    ``tokens_in`` is the UNCACHED input portion (the API's ``input_tokens``
    already excludes cached tokens). Non-Claude models cost $0. Pricing
    comes from ``core.model_catalog`` (no user overrides — the bench has no
    settings file), so the receipt uses the same price table as the app.
    Best-effort: a catalog failure degrades to $0 rather than breaking the
    safety run.
    """
    if not model or "claude" not in model.lower():
        return 0.0
    try:
        from core.model_catalog import get_catalog
        price_in, price_out = get_catalog().prices_for_model(model, None)
    except Exception as exc:  # noqa: BLE001 — telemetry never breaks the run
        log.warning("perf overlay: price lookup failed for %s: %s", model, exc)
        return 0.0
    return (
        tokens_in * price_in
        + cache_read_tokens * price_in * _CACHE_READ_PRICE_MULT
        + cache_creation_tokens * price_in * _CACHE_WRITE_PRICE_MULT
        + tokens_out * price_out
    ) / 1_000_000


def _usage_field(usage: Any, name: str) -> int:
    """Read one int field off an Anthropic usage object OR a plain dict.

    Defensive: missing fields, ``None`` values, and unconvertible types all
    degrade to 0 (same contract as claude_client's ``_cache_tokens``).
    """
    if isinstance(usage, dict):
        val = usage.get(name, 0)
    else:
        val = getattr(usage, name, 0)
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


class UsageCollector:
    """Accumulates Anthropic API usage across the calls of one bench task.

    Every public method is guaranteed never to raise — instrumentation must
    never break the task it observes (same contract as services/perf_metrics).
    """

    def __init__(self) -> None:
        self.api_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0

    def record_usage(self, usage: Any) -> None:
        """Add one usage object (or dict). No-op on None; never raises."""
        if usage is None:
            return
        try:
            self.api_calls += 1
            self.tokens_in += _usage_field(usage, "input_tokens")
            self.tokens_out += _usage_field(usage, "output_tokens")
            self.cache_read_tokens += _usage_field(
                usage, "cache_read_input_tokens")
            self.cache_creation_tokens += _usage_field(
                usage, "cache_creation_input_tokens")
        except Exception:  # noqa: BLE001
            pass

    def record_response(self, response: Any) -> None:
        """Convenience: record the ``usage`` attribute of an API response."""
        try:
            self.record_usage(getattr(response, "usage", None))
        except Exception:  # noqa: BLE001
            pass

    def reset(self) -> None:
        try:
            self.__init__()
        except Exception:  # noqa: BLE001
            pass

    def task_perf(self, *, model: str, wall_clock_s: float) -> dict[str, Any]:
        """Render the accumulated usage as one per-task perf record."""
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cost_usd": round(estimate_cost_usd(
                model, self.tokens_in, self.tokens_out,
                self.cache_read_tokens, self.cache_creation_tokens,
            ), 6),
            "wall_clock_s": round(float(wall_clock_s), 3),
        }


class _RecordingMessages:
    """Proxy for ``client.messages`` that records ``create()`` usage."""

    def __init__(self, inner: Any, collector: UsageCollector) -> None:
        self._inner = inner
        self._collector = collector

    def create(self, *args: Any, **kwargs: Any) -> Any:
        response = self._inner.create(*args, **kwargs)
        self._collector.record_response(response)
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class RecordingAnthropicClient:
    """Duck-typed wrapper around an Anthropic client.

    ``messages.create`` responses feed the collector; every other attribute
    (including ``messages.stream``, ``with_options``, …) passes straight
    through, so agentdojo's ``AnthropicLLM`` sees a normal client.
    """

    def __init__(self, inner: Any, collector: UsageCollector) -> None:
        self._inner = inner
        self.messages = _RecordingMessages(inner.messages, collector)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_anthropic_client(client: Any, collector: UsageCollector) -> Any:
    """Wrap ``client`` so its ``messages.create`` usage feeds ``collector``."""
    return RecordingAnthropicClient(client, collector)


def task_perf_from_token_usage(
    db_module: Any,
    conversation_ids: Iterable[str],
    *,
    wall_clock_s: float,
) -> dict[str, Any]:
    """Sum ``token_usage`` rows for the given conversations into a perf record.

    For harnesses that drive the real chat stack (ChatOrchestrator writes one
    ``token_usage`` row per turn, keyed by conversation_id, with the Phase 1
    cache-telemetry columns). ``cost_usd`` is summed from the rows — i.e. the
    orchestrator's own cache-aware estimate — rather than re-derived here.
    """
    ids = [str(c) for c in conversation_ids if c]
    record = {key: 0 for key in PERF_KEYS}
    record["cost_usd"] = 0.0
    if ids:
        placeholders = ", ".join("?" for _ in ids)
        rows = db_module.fetchall(
            "SELECT tokens_in, tokens_out, cache_read_tokens, "
            "cache_creation_tokens, cost_usd FROM token_usage "
            f"WHERE conversation_id IN ({placeholders})",
            tuple(ids),
        )
        for row in rows:
            record["tokens_in"] += int(row["tokens_in"] or 0)
            record["tokens_out"] += int(row["tokens_out"] or 0)
            record["cache_read_tokens"] += int(row["cache_read_tokens"] or 0)
            record["cache_creation_tokens"] += int(
                row["cache_creation_tokens"] or 0)
            record["cost_usd"] += float(row["cost_usd"] or 0.0)
    record["cost_usd"] = round(record["cost_usd"], 6)
    record["wall_clock_s"] = round(float(wall_clock_s), 3)
    return record


def parse_enable_flags(raw: str | None) -> list[str]:
    """Parse the ``--enable-flags key1,key2`` CLI value. Order-preserving,
    whitespace-tolerant, duplicate-free."""
    out: list[str] = []
    for part in (raw or "").split(","):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def cache_hit_rate(
    tokens_in: int, cache_read_tokens: int, cache_creation_tokens: int,
) -> float:
    """Share of prompt tokens served from the cache.

    Denominator is the full prompt: uncached input + cache reads + cache
    writes (the API's ``input_tokens`` excludes cached tokens). 0.0 when no
    prompt tokens were observed.
    """
    denom = tokens_in + cache_read_tokens + cache_creation_tokens
    if denom <= 0:
        return 0.0
    return round(cache_read_tokens / denom, 4)


def aggregate_suite_perf(
    per_task: list[dict[str, Any]],
    *,
    config_name: str = "default",
    enabled_flags: Iterable[str] = (),
    spans: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Roll per-task ``perf`` records up into the suite-level ``perf`` block.

    ``per_task`` is run_suites' per_task list — records without a ``perf``
    dict (older shapes, partial failures) are simply skipped. The block is
    informational: no thresholds gate on it.

    Shape (additive to the suite results JSON):

        perf:
          config_name        — the --perf-config label for this run
          enabled_flags      — flags forced True via --enable-flags
          tasks_with_perf    — how many per_task records carried perf data
          totals             — summed tokens/cost/wall-clock across tasks
          means              — tokens_per_task (in+out), cost_usd_per_task,
                               wall_clock_s_per_task
          cache_hit_rate     — cache reads / total prompt tokens
          spans              — optional services.perf_metrics snapshot
    """
    perfs = [
        r.get("perf") for r in (per_task or [])
        if isinstance(r.get("perf"), dict)
    ]
    n = len(perfs)
    totals = {
        "tokens_in": sum(int(p.get("tokens_in", 0) or 0) for p in perfs),
        "tokens_out": sum(int(p.get("tokens_out", 0) or 0) for p in perfs),
        "cache_read_tokens": sum(
            int(p.get("cache_read_tokens", 0) or 0) for p in perfs),
        "cache_creation_tokens": sum(
            int(p.get("cache_creation_tokens", 0) or 0) for p in perfs),
        "cost_usd": round(
            sum(float(p.get("cost_usd", 0.0) or 0.0) for p in perfs), 6),
        "wall_clock_s": round(
            sum(float(p.get("wall_clock_s", 0.0) or 0.0) for p in perfs), 3),
    }
    means = {
        "tokens_per_task": round(
            (totals["tokens_in"] + totals["tokens_out"]) / n, 2) if n else 0.0,
        "cost_usd_per_task": round(totals["cost_usd"] / n, 6) if n else 0.0,
        "wall_clock_s_per_task": round(
            totals["wall_clock_s"] / n, 3) if n else 0.0,
    }
    block: dict[str, Any] = {
        "config_name": config_name,
        "enabled_flags": list(enabled_flags),
        "tasks_with_perf": n,
        "totals": totals,
        "means": means,
        "cache_hit_rate": cache_hit_rate(
            totals["tokens_in"],
            totals["cache_read_tokens"],
            totals["cache_creation_tokens"],
        ),
    }
    if spans:
        block["spans"] = spans
    return block
