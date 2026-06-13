"""
tests/test_history_compactor.py — Perf Phase 3b: rolling history compaction.

The heart of the suite is ``test_fresh_summary_reused_with_byte_stable_prefix``:
between regenerations the compacted prefix must be BYTE-IDENTICAL across
consecutive turns (kept-verbatim window anchored at the stored
``covers_through_message_count``, NOT recomputed per turn) — otherwise the
Phase 3a history cache breakpoint misses every turn and the feature pays the
1.25× write premium with zero reads. See the history_compactor module
docstring for the full economics argument.

Uses the real in_memory_db fixture; the summarizer clients are tiny local
counting fakes (no mocks of the code under test).
"""

from __future__ import annotations

import pytest

CONV_ID = "conv-compact-test"
BUDGET = 1_000  # chars — tiny so a handful of fixture messages overflows


# ── Tiny counting summarizer fakes ────────────────────────────────────────────

class CountingClient:
    """Summarizer stand-in: counts calls, returns a fixed text."""

    def __init__(self, text: str = "Summary: Ada chose plan B on May 3.",
                 available: bool = True, name: str = "fake-summarizer"):
        self.calls = 0
        self.prompts: list[list] = []
        self._text = text
        self._available = available
        self._name = name

    def is_available(self) -> bool:
        return self._available

    def chat_unified(self, system, messages, max_tokens: int = 4096) -> dict:
        self.calls += 1
        self.prompts.append(messages)
        return {"text": self._text, "input_tokens": 1, "output_tokens": 1}

    def client_name(self) -> str:
        return self._name


class FailingClient(CountingClient):
    def chat_unified(self, system, messages, max_tokens: int = 4096) -> dict:
        self.calls += 1
        raise RuntimeError("summarizer exploded")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_conversation(db, n_messages: int, char_len: int = 120) -> list[dict]:
    """Insert a conversation + n user/assistant message rows; return the
    in-memory message dicts in the same shape the trim site sees."""
    db.execute(
        "INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)",
        (CONV_ID, "compactor test", "2026-01-01T00:00:00+00:00"),
    )
    db.commit()
    messages = []
    for i in range(n_messages):
        messages.append(_append_message(db, i, char_len))
    return messages


def _append_message(db, index: int, char_len: int = 120) -> dict:
    role = "user" if index % 2 == 0 else "assistant"
    content = f"message {index:03d} " + ("x" * char_len)
    db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"{CONV_ID}-{index:04d}", CONV_ID, role, content,
         f"2026-01-01T01:{index // 60:02d}:{index % 60:02d}+00:00"),
    )
    db.commit()
    return {"role": role, "content": content}


def _settings(**overrides) -> dict:
    base = {
        "history_compaction_keep_recent_msgs": 8,
        "history_compaction_batch_msgs": 6,
        "history_compaction_max_summary_chars": 2000,
    }
    base.update(overrides)
    return base


def _compact(db, messages, local, claude=None, settings=None):
    from services import history_compactor
    return history_compactor.compact(
        conversation_id=CONV_ID,
        messages=messages,
        budget_chars=BUDGET,
        settings=settings or _settings(),
        local_client=local,
        claude_client=claude,
    )


def _summary_row(db):
    return db.fetchone(
        "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
        (CONV_ID,),
    )


# ── Under budget: untouched ───────────────────────────────────────────────────

def test_under_budget_returns_same_object(in_memory_db):
    messages = _seed_conversation(in_memory_db, 4, char_len=10)
    local = CountingClient()
    result = _compact(in_memory_db, messages, local)
    assert result is messages          # the SAME object — byte-identical turn
    assert local.calls == 0
    assert _summary_row(in_memory_db) is None


# ── Over budget: summary generated, shape correct ─────────────────────────────

def test_over_budget_generates_summary_and_keeps_recent(in_memory_db):
    messages = _seed_conversation(in_memory_db, 20)  # 20 × ~132 chars > BUDGET
    local = CountingClient()

    result = _compact(in_memory_db, messages, local)

    assert local.calls == 1
    # Summary pair + the 8 most recent messages (keep_recent default).
    assert result[0]["role"] == "user"
    assert result[0]["content"].startswith("<conversation_summary>")
    assert result[0]["content"].endswith("</conversation_summary>")
    assert local._text in result[0]["content"]
    assert result[1] == {"role": "assistant", "content": "Understood."}
    assert result[2:] == messages[12:]  # covers = 20 - keep_recent(8) = 12

    row = _summary_row(in_memory_db)
    assert row is not None
    assert row["covers_through_message_count"] == 12
    assert row["source_message_count"] == 20
    assert row["summary_text"] == local._text
    assert row["model_used"] == "fake-summarizer"
    # The evicted messages (0..11) were in the summarizer prompt.
    prompt_text = local.prompts[0][0]["content"]
    assert "message 000" in prompt_text and "message 011" in prompt_text
    assert "message 012" not in prompt_text


# ── THE cache-economics test: fresh row → no LLM call, byte-stable prefix ─────

def test_fresh_summary_reused_with_byte_stable_prefix(in_memory_db):
    messages = _seed_conversation(in_memory_db, 20)
    local = CountingClient()

    first = _compact(in_memory_db, list(messages), local)
    assert local.calls == 1

    # Next turn: one more user+assistant pair lands (still < batch_msgs=6
    # past the stored cut), so the stored summary must be reused verbatim.
    for i in (20, 21):
        messages.append(_append_message(in_memory_db, i))
    second = _compact(in_memory_db, list(messages), local)

    assert local.calls == 1                       # NO new LLM call
    # Byte-stability: the second result extends the first — identical
    # prefix, new messages appended at the end. This is what lets the
    # Phase 3a history breakpoint read the previous turn's cache entry.
    assert second[:len(first)] == first
    assert second[len(first):] == messages[20:]


def test_stale_by_batch_triggers_regeneration(in_memory_db):
    messages = _seed_conversation(in_memory_db, 20)
    local = CountingClient()
    _compact(in_memory_db, list(messages), local)  # covers -> 12

    # Add 6 more messages: overflow (26-8=18) - covers (12) == batch (6).
    for i in range(20, 26):
        messages.append(_append_message(in_memory_db, i))
    _compact(in_memory_db, list(messages), local)

    assert local.calls == 2
    row = _summary_row(in_memory_db)
    assert row["covers_through_message_count"] == 18
    assert row["source_message_count"] == 26
    # Incremental regeneration: the prior summary + ONLY the newly-evicted
    # messages (12..17) went into the second prompt.
    prompt_text = local.prompts[1][0]["content"]
    assert local._text in prompt_text             # prior summary folded in
    assert "message 012" in prompt_text and "message 017" in prompt_text
    assert "message 011" not in prompt_text and "message 018" not in prompt_text


# ── Truncation ────────────────────────────────────────────────────────────────

def test_summary_hard_truncated_at_max_chars(in_memory_db):
    messages = _seed_conversation(in_memory_db, 20)
    local = CountingClient(text="y" * 5000)
    settings = _settings(history_compaction_max_summary_chars=2000)

    result = _compact(in_memory_db, messages, local, settings=settings)

    inner = result[0]["content"]
    inner = inner[len("<conversation_summary>"):-len("</conversation_summary>")]
    assert len(inner) == 2000
    assert len(_summary_row(in_memory_db)["summary_text"]) == 2000


# ── Client selection ──────────────────────────────────────────────────────────

def test_local_preferred_over_claude(in_memory_db):
    messages = _seed_conversation(in_memory_db, 20)
    local = CountingClient(name="local-summarizer")
    claude = CountingClient(name="claude-summarizer")

    _compact(in_memory_db, messages, local, claude=claude)

    assert local.calls == 1
    assert claude.calls == 0
    assert _summary_row(in_memory_db)["model_used"] == "local-summarizer"


def test_claude_used_when_local_unavailable(in_memory_db):
    messages = _seed_conversation(in_memory_db, 20)
    local = CountingClient(available=False)
    claude = CountingClient(name="claude-summarizer")

    _compact(in_memory_db, messages, local, claude=claude)

    assert local.calls == 0
    assert claude.calls == 1
    assert _summary_row(in_memory_db)["model_used"] == "claude-summarizer"


# ── Failure propagates (orchestrator wrapper owns the fallback) ───────────────

def test_summarizer_failure_propagates(in_memory_db):
    messages = _seed_conversation(in_memory_db, 20)
    with pytest.raises(RuntimeError):
        _compact(in_memory_db, messages, FailingClient())
    assert _summary_row(in_memory_db) is None  # nothing half-persisted


# ── Persistence: single live row, replaced not duplicated ─────────────────────

def test_single_row_replaced_not_duplicated(in_memory_db):
    messages = _seed_conversation(in_memory_db, 20)
    local = CountingClient()
    _compact(in_memory_db, list(messages), local)
    for i in range(20, 26):
        messages.append(_append_message(in_memory_db, i))
    _compact(in_memory_db, list(messages), local)

    rows = in_memory_db.fetchall(
        "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
        (CONV_ID,),
    )
    assert len(rows) == 1
    assert local.calls == 2


# ── Orchestrator wrapper: flag gate + legacy fallback ─────────────────────────

def _make_orchestrator(settings: dict):
    from services.chat_orchestrator import ChatOrchestrator
    from services.memory import MemoryManager

    class _Local(CountingClient):
        def chat(self, *a, **k):
            return "[]"

        def stream_unified(self, system, messages, on_token, max_tokens=4096):
            return self.chat_unified(system, messages, max_tokens)

    local = _Local()
    claude = CountingClient(name="claude")
    memory = MemoryManager(None, None, local, settings)

    class _Router:
        def classify(self, *a, **k):  # never reached by _compact_or_trim
            raise AssertionError("router must not be consulted")

    return ChatOrchestrator(claude, local, _Router(), memory, settings)


def test_wrapper_flag_off_is_legacy_trim(in_memory_db, monkeypatch):
    orch = _make_orchestrator({"history_compaction_enabled": False})
    from services import history_compactor

    def _boom(**kwargs):
        raise AssertionError("compactor must not run when the flag is off")

    monkeypatch.setattr(history_compactor, "compact", _boom)
    messages = [{"role": "user", "content": "x" * 200_000}]
    assert orch._compact_or_trim("cid", list(messages)) == \
        orch._trim_history_to_budget(list(messages))


def test_wrapper_falls_back_to_trim_on_compactor_error(in_memory_db, monkeypatch):
    orch = _make_orchestrator({"history_compaction_enabled": True})
    from services import history_compactor

    calls = {"n": 0}

    def _boom(**kwargs):
        calls["n"] += 1
        raise RuntimeError("compactor exploded")

    monkeypatch.setattr(history_compactor, "compact", _boom)
    messages = [
        {"role": "user", "content": "a" * 90_000},
        {"role": "assistant", "content": "b" * 10_000},
        {"role": "user", "content": "c"},
    ]
    result = orch._compact_or_trim("cid", list(messages))
    assert calls["n"] == 1
    assert result == orch._trim_history_to_budget(list(messages))
