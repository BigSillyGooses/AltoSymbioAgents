"""
services/cost_predictor.py — Pre-turn cost prediction (Perf Phase 4).

``predict()`` estimates what the turn that is about to be dispatched will
cost BEFORE any model call, so the orchestrator can emit a ``cost_predicted``
SSE event (UI hint) and — behind a separate flag — short-circuit a turn whose
predicted spend would blow the conversation budget.

Design contract (mirrors the Phase 4 plan):

  - Pure function: no SSE, no DB writes. The single DB *read* (the rolling
    mean over the conversation's past ``tokens_out``) is wrapped in
    try/except so a missing table can never break a prediction.
  - Token estimate: deterministic ``chars // 4`` heuristic over the rendered
    system prompt + message contents (block-list content renders its text
    blocks; other blocks render as sorted JSON, matching the harness's
    FakeClaudeClient). Opt-in exact counts via the Anthropic
    ``count_tokens`` endpoint (``cost_prediction_use_api_count``, default
    off) — ANY failure falls back to the heuristic.
  - Output estimate: rolling mean of the conversation's last
    ``_OUTPUT_HISTORY_ROWS`` ``tokens_out`` values; with no history,
    ``4096 × cost_prediction_output_fraction``.
  - Cached portion: when ``claude_history_caching`` is on, the stable prefix
    (system + everything before the final user message — the part Phase 3a's
    history breakpoint reads back) is priced at the 0.1× cache-read rate.
    This deliberately ignores cache-write premiums and the min-cacheable-
    prefix floor: it is an estimate for a UI hint, not billing.
  - Pricing comes from ``core.model_catalog`` with the same
    ``model_prices`` user-override extraction as
    ``chat_orchestrator._estimate_cost``, so predicted-vs-actual comparisons
    are apples-to-apples. Non-Claude models predict $0 (local inference is
    free), though token estimates are still returned.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import db

log = logging.getLogger("altosybioagents.cost_predictor")

# Anthropic bills prompt-cache reads at 0.1× the input price — the same
# number as chat_orchestrator._CACHE_READ_PRICE_MULT (one float, duplicated
# rather than importing the whole orchestrator module for it).
_CACHE_READ_PRICE_MULT = 0.1

# The orchestrator's default per-request output ceiling. With no history to
# average, the output estimate is this ceiling scaled by the
# cost_prediction_output_fraction setting.
_DEFAULT_MAX_TOKENS = 4096

# Rolling window for the output-tokens estimate.
_OUTPUT_HISTORY_ROWS = 10


@dataclass(frozen=True)
class CostPrediction:
    """One pre-turn estimate. Token fields mirror the API usage object:
    ``est_input_tokens`` is the UNCACHED portion only; the full prompt is
    ``est_input_tokens + est_cached_tokens``."""

    est_input_tokens: int
    est_cached_tokens: int
    est_output_tokens: int
    est_cost_usd: float
    method: str  # "heuristic" | "api_count"


def _count_text(text: str) -> int:
    """Deterministic heuristic tokenizer: 4 chars ≈ 1 token."""
    return len(text or "") // 4


def _render_content(content) -> str:
    """Flatten a message's content to text the way the request renders it.

    Strings pass through; block lists contribute their text blocks verbatim
    and any other block (image, tool_use, …) as sorted JSON — the same
    deterministic rendering the perf harness's FakeClaudeClient uses, so the
    heuristic and the simulated tokenizer agree to within role-separator
    noise.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", "") or "")
            else:
                parts.append(json.dumps(block, sort_keys=True, default=str))
        return "".join(parts)
    return str(content)


def _message_tokens(messages: list) -> list[int]:
    """Heuristic token count per message (content only — role labels and
    wire separators are noise within the estimate's tolerance)."""
    counts: list[int] = []
    for msg in messages:
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        counts.append(_count_text(_render_content(content)))
    return counts


def _stable_prefix_tokens(system_tokens: int, msg_tokens: list[int],
                          messages: list) -> int:
    """Heuristic size of the prefix the Phase 3a history cache serves.

    The breakpoint sits at the stable end of the previous turn, so the
    cached portion is approximated as the system prompt plus every message
    BEFORE the final user message. No final user message → predict nothing
    cached (conservative: less cached ⇒ higher cost estimate).
    """
    last_user = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            last_user = i
            break
    if last_user is None:
        return 0
    return system_tokens + sum(msg_tokens[:last_user])


def _estimate_output_tokens(conversation_id, settings) -> int:
    """Rolling mean of the conversation's recent ``tokens_out``; fraction-of-
    max-tokens fallback when there is no usable history."""
    if conversation_id:
        try:
            rows = db.fetchall(
                "SELECT tokens_out FROM token_usage WHERE conversation_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (conversation_id, _OUTPUT_HISTORY_ROWS),
            )
            vals = [int(r["tokens_out"] or 0) for r in rows]
            if vals:
                return int(round(sum(vals) / len(vals)))
        except Exception as exc:  # noqa: BLE001 — a read failure must not break prediction
            log.debug("cost prediction: tokens_out history read failed: %s", exc)
    try:
        fraction = float(settings.get("cost_prediction_output_fraction", 0.5) or 0.5)
    except Exception:  # noqa: BLE001
        fraction = 0.5
    fraction = min(max(fraction, 0.0), 1.0)
    return int(round(_DEFAULT_MAX_TOKENS * fraction))


def _prices(model_name: str, settings) -> tuple[float, float]:
    """(input, output) price per MTok — EXACTLY the resolution
    ``chat_orchestrator._estimate_cost`` performs, including the
    ``model_prices`` user-override extraction, so predicted and actual costs
    share one price table."""
    from core.model_catalog import get_catalog

    user_overrides: dict[str, tuple[float, float]] | None = None
    if settings:
        custom = settings.get("model_prices", None)
        if custom and isinstance(custom, dict):
            user_overrides = {}
            for key, val in custom.items():
                if isinstance(val, (list, tuple)) and len(val) == 2:
                    user_overrides[key] = (float(val[0]), float(val[1]))

    return get_catalog().prices_for_model(model_name, user_overrides)


def predict(full_system: str, messages: list, model_name: str, settings, *,
            claude_client=None, conversation_id=None) -> CostPrediction:
    """Estimate the cost of the turn about to be sent. Never raises for the
    expected failure modes (count_tokens errors, missing tables) — those
    degrade to the heuristic / fallback paths documented in the module
    docstring."""
    messages = list(messages or [])
    is_claude = bool(model_name) and "claude" in model_name.lower()

    system_tokens = _count_text(full_system or "")
    msg_tokens = _message_tokens(messages)
    total_tokens = system_tokens + sum(msg_tokens)
    method = "heuristic"

    if (
        is_claude
        and claude_client is not None
        and settings.get("cost_prediction_use_api_count", False)
    ):
        try:
            counted = claude_client.count_tokens(full_system or "", messages)
            if counted is not None and int(counted) > 0:
                total_tokens = int(counted)
                method = "api_count"
        except Exception as exc:  # noqa: BLE001 — ANY failure → heuristic
            log.debug("cost prediction: count_tokens failed (%s); "
                      "using heuristic", exc)

    est_cached_tokens = 0
    if is_claude and settings.get("claude_history_caching", False):
        est_cached_tokens = min(
            _stable_prefix_tokens(system_tokens, msg_tokens, messages),
            total_tokens,
        )
    est_input_tokens = max(0, total_tokens - est_cached_tokens)

    est_output_tokens = _estimate_output_tokens(conversation_id, settings)

    if is_claude:
        price_in, price_out = _prices(model_name, settings)
        est_cost_usd = (
            est_input_tokens * price_in
            + est_cached_tokens * price_in * _CACHE_READ_PRICE_MULT
            + est_output_tokens * price_out
        ) / 1_000_000
    else:
        est_cost_usd = 0.0

    return CostPrediction(
        est_input_tokens=est_input_tokens,
        est_cached_tokens=est_cached_tokens,
        est_output_tokens=est_output_tokens,
        est_cost_usd=est_cost_usd,
        method=method,
    )
