"""
services/history_compactor.py — Rolling history compaction (Perf Phase 3b).

When a conversation's history outgrows the context budget, the legacy
behavior (``ChatOrchestrator._trim_history_to_budget``) silently drops the
oldest messages. This module replaces the drop with a rolling summary:
overflowing messages are folded into ONE persisted summary per conversation
(``conversation_summaries`` table) and the turn is sent as

    [ {"role": "user",      "content": "<conversation_summary>…</conversation_summary>"},
      {"role": "assistant", "content": "Understood."} ] + recent messages

The summary rides in MESSAGES, not the system prompt, for two reasons:
the system cache block (``_build_system_with_cache``) stays byte-frozen, and
the summary pair itself becomes part of the stable cacheable prefix that the
Phase 3a history breakpoint (``claude_history_caching``) reads back at 0.1×.

── Count anchoring + batched regeneration (the cache-hit economics) ──────────

The messages at the trim site are plain ``{"role", "content"}`` dicts from
the history query — they carry no ids — so the summary's coverage is
anchored by COUNT: ``covers_through_message_count`` = how many of the
conversation's user/assistant rows (in ``messages``, ordered by created_at)
the stored summary covers. Everything from that absolute position onward is
sent verbatim.

The critical detail is that the verbatim window is anchored at the STORED
``covers_through_message_count`` while the summary is fresh — NOT recomputed
as "last N messages" every turn. Anthropic prompt caching is a strict
byte-prefix match: if the kept-verbatim window slid forward each turn, the
bytes right after the summary pair would change every turn, every history
breakpoint would miss, and we would pay the 1.25× cache-write premium per
turn with zero reads — strictly worse than no caching at all. Anchoring at
the stored cut means consecutive turns only APPEND bytes (the new user +
assistant messages), so each turn reads the previous turn's prefix back from
cache.

Regeneration is therefore batched: the summary (and with it the cut point)
only moves once ``overflow_count - covers_through_message_count >=
history_compaction_batch_msgs``. That one regeneration turn takes a full
cache miss (plus one summarizer call, preferring the free local model), and
the cost is amortized across the following batch of cache-hit turns.

Failure contract: ANY exception propagates to the caller.
``ChatOrchestrator._compact_or_trim`` wraps the call and falls back to the
legacy ``_trim_history_to_budget`` so a summarizer failure can never break a
chat turn.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import db

log = logging.getLogger("altosybioagents.history_compactor")

DEFAULT_KEEP_RECENT_MSGS = 8
DEFAULT_BATCH_MSGS = 6
DEFAULT_MAX_SUMMARY_CHARS = 2000

# Fixed prompt — part of the determinism contract (the perf harness replays
# it against fake clients) and deliberately terse: the summarizer may be a
# small local model.
SUMMARY_SYSTEM_PROMPT = (
    "You maintain a rolling summary of an ongoing conversation. Produce a "
    "dense, factual summary that preserves every name, number, date, "
    "decision, and open question. No preamble, no commentary — output the "
    "summary text only."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _total_message_count(conversation_id: str) -> int:
    """Absolute user/assistant message count for the conversation.

    The window handed to ``compact`` is capped upstream (MAX_HISTORY_MESSAGES)
    so its indices shift as the conversation grows; this count is the fixed
    frame of reference that ``covers_through_message_count`` anchors against.
    """
    row = db.fetchone(
        "SELECT COUNT(*) AS cnt FROM messages "
        "WHERE conversation_id = ? AND role IN ('user', 'assistant')",
        (conversation_id,),
    )
    return int(row["cnt"]) if row else 0


def _pick_client(local_client, claude_client):
    """Prefer the free local model; fall back to Claude."""
    if local_client is not None:
        try:
            if local_client.is_available():
                return local_client
        except Exception:  # noqa: BLE001 — availability probe must not abort
            pass
    if claude_client is None:
        raise RuntimeError("history compaction: no summarizer client available")
    return claude_client


def _generate_summary(prior_summary: str, evicted: list, max_chars: int,
                      local_client, claude_client) -> tuple[str, str]:
    """One LLM call folding ``evicted`` into ``prior_summary``.

    Returns ``(summary_text, model_used)``. Hard-truncates at ``max_chars``.
    Raises on any model failure or empty output — the caller's caller
    (orchestrator wrapper) falls back to the legacy trim.
    """
    lines = [
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in evicted
    ]
    parts = []
    if prior_summary:
        parts.append(
            "Existing summary of the conversation so far:\n" + prior_summary
        )
    parts.append(
        "New messages to fold into the summary:\n" + "\n\n".join(lines)
    )
    parts.append(
        f"Reply with the updated summary only (at most {max_chars} characters)."
    )

    client = _pick_client(local_client, claude_client)
    result = client.chat_unified(
        SUMMARY_SYSTEM_PROMPT,
        [{"role": "user", "content": "\n\n".join(parts)}],
        max_tokens=1024,
    )
    text = (result or {}).get("text", "") if isinstance(result, dict) else ""
    text = (text or "").strip()
    if not text:
        raise RuntimeError("history compaction: summarizer returned no text")
    try:
        model_used = client.client_name()
    except Exception:  # noqa: BLE001
        model_used = "unknown"
    return text[:max_chars], model_used


def _persist(conversation_id: str, covers: int, source_count: int,
             summary_text: str, model_used: str, created_at: str | None) -> None:
    """Replace the conversation's single live summary row atomically."""
    now = _now()
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,),
        )
        conn.execute(
            "INSERT INTO conversation_summaries "
            "(conversation_id, covers_through_message_count, "
            " source_message_count, summary_text, model_used, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, covers, source_count, summary_text,
             model_used, created_at or now, now),
        )


def compact(conversation_id: str, messages: list, budget_chars: int,
            settings, local_client, claude_client) -> list:
    """Compact ``messages`` to fit ``budget_chars``, preserving old facts.

    Under budget the input list is returned untouched (the same object —
    byte-identical request downstream). Over budget, the messages older than
    the anchored cut point are represented by the persisted rolling summary
    (regenerated at most every ``history_compaction_batch_msgs`` messages —
    see module docstring) and the rest are returned verbatim.

    Raises on any failure (missing table, summarizer error, nothing
    evictable); the orchestrator wrapper falls back to the legacy trim.
    """
    if not messages:
        return messages
    total_chars = sum(len(m.get("content", "")) for m in messages)
    if total_chars <= budget_chars:
        return messages

    keep_recent = max(1, int(settings.get(
        "history_compaction_keep_recent_msgs", DEFAULT_KEEP_RECENT_MSGS,
    ) or DEFAULT_KEEP_RECENT_MSGS))
    batch_msgs = max(1, int(settings.get(
        "history_compaction_batch_msgs", DEFAULT_BATCH_MSGS,
    ) or DEFAULT_BATCH_MSGS))
    max_summary_chars = max(1, int(settings.get(
        "history_compaction_max_summary_chars", DEFAULT_MAX_SUMMARY_CHARS,
    ) or DEFAULT_MAX_SUMMARY_CHARS))

    total_abs = _total_message_count(conversation_id)
    # The window can only be a suffix of the conversation; if the count query
    # disagrees (tests passing ad-hoc lists), the window is authoritative.
    total_abs = max(total_abs, len(messages))
    offset = total_abs - len(messages)  # absolute index of messages[0]

    # Where the cut WOULD land if regenerated this turn.
    overflow = total_abs - keep_recent
    if overflow <= 0:
        raise RuntimeError(
            "history compaction: over budget but nothing evictable "
            f"({total_abs} messages, keep_recent={keep_recent})"
        )

    row = db.fetchone(
        "SELECT covers_through_message_count, summary_text, created_at "
        "FROM conversation_summaries WHERE conversation_id = ?",
        (conversation_id,),
    )
    covers_old = int(row["covers_through_message_count"]) if row else 0
    summary_old = (row["summary_text"] or "") if row else ""

    # Fresh = the stored summary's cut is still within one batch of where a
    # regeneration would put it (and sane w.r.t. the current history — a
    # branched/edited conversation can leave covers_old beyond the overflow
    # point, in which case the anchor is stale the other way and we rebuild).
    fresh = (
        row is not None
        and 0 < covers_old <= overflow
        and (overflow - covers_old) < batch_msgs
    )

    if fresh:
        cut = covers_old
        summary_text = summary_old
    else:
        cut = overflow
        if row is not None and 0 < covers_old <= cut:
            # Incremental: fold only the newly-evicted messages into the
            # stored summary. Messages already outside the capped window
            # (absolute index < offset) are covered by the stored text.
            base_summary = summary_old
            evict_from = covers_old
        else:
            # No usable prior row — rebuild from everything evictable that
            # is still inside the window.
            base_summary = ""
            evict_from = 0
        start = max(0, evict_from - offset)
        end = max(start, cut - offset)
        evicted = messages[start:end]
        summary_text, model_used = _generate_summary(
            base_summary, evicted, max_summary_chars,
            local_client, claude_client,
        )
        _persist(
            conversation_id, cut, total_abs, summary_text, model_used,
            created_at=(row["created_at"] if row else None),
        )
        log.info(
            "History compacted: conversation %s summarized through message "
            "%d/%d (%d evicted this pass, summary %d chars)",
            conversation_id, cut, total_abs, len(evicted), len(summary_text),
        )

    recent = messages[max(0, cut - offset):]
    return [
        {
            "role": "user",
            "content": (
                "<conversation_summary>" + summary_text
                + "</conversation_summary>"
            ),
        },
        {"role": "assistant", "content": "Understood."},
    ] + recent
