"""tests/test_design_orchestrator.py — Design Studio end-to-end through send().

The unit tests cover prompt composition; these cover the orchestrator wiring
the tracer flagged as a gap: that the design_block actually reaches the model's
`system` parameter — including the Reader/Actor split path, where the Actor
gets a deliberately bare persona prompt and would otherwise miss the directive.
Uses real vendored assets (design_studio_enabled + a real design_system_id).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


def _make_orchestrator(claude_client, local_client, settings):
    from services.chat_orchestrator import ChatOrchestrator
    from services.memory import MemoryContext, MemoryManager
    from models import RouteDecision

    memory = MemoryManager(rag_index=None, semantic_search_mod=None,
                           local_client=local_client)
    memory.get_context = lambda *a, **k: MemoryContext(
        recent_messages=[], session_facts=[], rag_chunks=[], memories=[],
    )
    router = MagicMock()
    router.classify.return_value = RouteDecision(
        model="claude", complexity="simple", reasoning="test",
    )
    return ChatOrchestrator(claude_client, local_client, router, memory, settings)


def _capture_claude(claude_client, scripts):
    it = iter(scripts)
    calls = []

    def _call(system, messages, max_tokens=4096):
        calls.append({"system": system, "messages": list(messages)})
        try:
            text = next(it)
        except StopIteration:
            text = ""
        return {"text": text, "input_tokens": 5, "output_tokens": 5}

    claude_client.chat_unified = _call
    claude_client.stream_unified = _call
    return calls


def _enable_design(settings):
    settings.set("design_studio_enabled", True)
    settings.set("design_system_id", "linear-app")


class TestMonolithicPath:
    def test_design_block_reaches_model_system(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
    ):
        _enable_design(settings)
        orch = _make_orchestrator(claude_client, local_client_unavailable, settings)
        conv = orch.create_conversation()
        captured = _capture_claude(claude_client, ["<artifact>...</artifact>"])

        orch.send(conv, "build me a landing page")

        assert len(captured) == 1
        system = captured[0]["system"]
        assert "Design Studio mode" in system
        assert "Active design system — Linear" in system
        assert "#5e6ad2" in system

    def test_flag_off_keeps_system_clean(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
    ):
        settings.set("design_studio_enabled", False)
        orch = _make_orchestrator(claude_client, local_client_unavailable, settings)
        conv = orch.create_conversation()
        captured = _capture_claude(claude_client, ["plain answer"])

        orch.send(conv, "hello")

        assert "Design Studio mode" not in captured[0]["system"]


class TestReaderActorSplit:
    def test_actor_receives_design_block(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
    ):
        # This is the tracer's bug: the Actor gets a bare persona prompt, so
        # without the fix the design directive never reaches the final model.
        _enable_design(settings)
        settings.set("reader_actor_split_enabled", True)
        orch = _make_orchestrator(claude_client, local_client_unavailable, settings)
        conv = orch.create_conversation()

        captured = _capture_claude(claude_client, [
            json.dumps({
                "intent": "build a landing page", "constraints": [],
                "relevant_facts": [], "proposed_tools": [], "red_flags": [],
            }),
            "<artifact>...</artifact>",
        ])

        orch.send(conv, "build me a landing page")

        # captured[0] = reader, captured[1] = actor.
        actor_system = captured[1]["system"]
        assert "Design Studio mode" in actor_system
        assert "Active design system — Linear" in actor_system
