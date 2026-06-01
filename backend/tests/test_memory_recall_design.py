"""tests/test_memory_recall_design.py — design_block threading in MemoryRecall.

Verifies that the Design Studio block is appended to full_system only when
the flag is on, survives the RAG-trim rebuild, and that a flag-off turn is
byte-identical to the pre-feature behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

from services.memory_recall import MemoryRecall


class _FakeSettings:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


class _FakeMemory:
    """get_context() returns a context whose suffix is empty and that carries
    a controllable rag_chunks list so trim_for_complexity can be exercised."""

    def __init__(self, n_chunks: int = 0):
        self._n = n_chunks

    def get_context(self, _conversation_id, _user_message):
        return SimpleNamespace(
            rag_chunks=[{"i": i} for i in range(self._n)],
            session_facts=[],
            memories=[],
            to_system_suffix=lambda: "",
        )


BASE_PROMPT = "You are a helpful AI assistant."


def test_flag_off_is_byte_identical_to_base():
    recall = MemoryRecall(_FakeMemory(), _FakeSettings({"design_studio_enabled": False}))
    result = recall.recall("c1", "hello", BASE_PROMPT)
    assert result.full_system == BASE_PROMPT
    assert result.design_block == ""


def test_flag_on_appends_design_block():
    settings = _FakeSettings(
        {"design_studio_enabled": True, "design_system_id": "linear-app"}
    )
    recall = MemoryRecall(_FakeMemory(), settings)
    result = recall.recall("c1", "build me a landing page", BASE_PROMPT)
    assert result.full_system.startswith(BASE_PROMPT)
    assert result.design_block
    assert "Design Studio mode" in result.full_system
    assert "Active design system — Linear" in result.full_system
    assert result.full_system.endswith(result.design_block)


def test_design_block_survives_rag_trim():
    settings = _FakeSettings(
        {"design_studio_enabled": True, "design_system_id": "linear-app"}
    )
    # 10 chunks > the "simple" cap (2) so trim_for_complexity rebuilds.
    recall = MemoryRecall(_FakeMemory(n_chunks=10), settings)
    result = recall.recall("c1", "x", BASE_PROMPT)
    block_before = result.design_block
    assert block_before

    trimmed = recall.trim_for_complexity(result, "simple", BASE_PROMPT)
    assert len(trimmed.mem.rag_chunks) == 2
    assert trimmed.design_block == block_before
    assert "Design Studio mode" in trimmed.full_system
