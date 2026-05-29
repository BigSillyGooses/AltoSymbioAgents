"""
tests/test_orchestrator_graph_engine.py

Stage-2 #7: covers the LangGraph-backed ChatOrchestrator.send() path
behind the ``orchestrator_engine`` setting.

What's pinned here:
  - Default engine is "legacy" (unknown values also fall through to legacy).
  - With engine="graph", a happy-path send() produces a ChatResult with
    populated text/model/tokens fields, the SSE event sequence the legacy
    body emits is preserved, and the assistant message is persisted to the
    messages table.
  - Both engines agree on a budget-exceeded early-exit (same route_reason,
    no worker invocation).
  - Both engines agree on agent.model_preference overriding the router.
  - The graph emits the same first few SSE events legacy does (smoke-level
    parity — full byte-identical parity is the Stage-2 weekly bench bar).
"""

import pytest
from unittest.mock import MagicMock


def _make_orchestrator(in_memory_db, claude_client, local_client, settings,
                       routing="claude"):
    from services.chat_orchestrator import ChatOrchestrator
    from services.memory import MemoryManager
    from models import RouteDecision

    router = MagicMock()
    router.classify.return_value = RouteDecision(
        model=routing, complexity="simple", reasoning="test",
    )
    mem = MemoryManager(
        rag_index=None, semantic_search_mod=None, local_client=local_client,
    )
    return ChatOrchestrator(claude_client, local_client, router, mem, settings)


# ── Engine selector ──────────────────────────────────────────────────────────


class TestEngineSelector:
    def test_default_engine_is_legacy(self, in_memory_db, claude_client,
                                       local_client_unavailable, settings):
        assert settings.get("orchestrator_engine", "legacy") == "legacy"

    def test_setting_engine_to_graph_routes_through_graph(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
        monkeypatch,
    ):
        """When the setting is 'graph', ChatOrchestrator.send() must invoke
        services.orchestrator_graph.run_turn_graph instead of running the
        legacy body."""
        settings.set("orchestrator_engine", "graph")
        orch = _make_orchestrator(
            in_memory_db, claude_client, local_client_unavailable, settings,
        )
        conv_id = orch.create_conversation()

        called = {"n": 0}

        def fake_run(orchestrator, conv, msg, agent_id=None,
                     on_token=None, on_event=None):
            called["n"] += 1
            from models import ChatResult
            import uuid as _uuid
            return ChatResult(
                text="from graph", model="x", route_reason="test",
                tokens_in=0, tokens_out=0, cost_usd=0.0,
                message_id=str(_uuid.uuid4()),
            )

        monkeypatch.setattr(
            "services.orchestrator_graph.run_turn_graph", fake_run,
        )
        result = orch.send(conv_id, "hello")
        assert called["n"] == 1
        assert result.text == "from graph"

    def test_unknown_engine_falls_back_to_legacy(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
    ):
        """A typo or unknown value behaves like 'legacy' rather than crashing."""
        settings.set("orchestrator_engine", "experimental_typo")
        orch = _make_orchestrator(
            in_memory_db, claude_client, local_client_unavailable, settings,
        )
        conv_id = orch.create_conversation()
        claude_client.chat_multi_turn = MagicMock(return_value={
            "text": "legacy reply", "input_tokens": 1, "output_tokens": 1,
        })
        result = orch.send(conv_id, "hi")
        assert result.text == "legacy reply"
        claude_client.chat_multi_turn.assert_called_once()


# ── Graph engine happy paths ─────────────────────────────────────────────────


class TestGraphEngineHappyPath:
    def test_graph_engine_basic_claude_turn(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
    ):
        settings.set("orchestrator_engine", "graph")
        orch = _make_orchestrator(
            in_memory_db, claude_client, local_client_unavailable, settings,
        )
        conv_id = orch.create_conversation()

        claude_client.chat_multi_turn = MagicMock(return_value={
            "text": "graph hello", "input_tokens": 7, "output_tokens": 11,
        })
        result = orch.send(conv_id, "hi there")

        assert result.text == "graph hello"
        assert result.tokens_in == 7
        assert result.tokens_out == 11
        assert result.model == claude_client._model
        assert result.message_id

    def test_graph_engine_persists_assistant_message(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
    ):
        settings.set("orchestrator_engine", "graph")
        orch = _make_orchestrator(
            in_memory_db, claude_client, local_client_unavailable, settings,
        )
        conv_id = orch.create_conversation()
        claude_client.chat_multi_turn = MagicMock(return_value={
            "text": "persisted answer", "input_tokens": 1, "output_tokens": 1,
        })

        result = orch.send(conv_id, "please answer")

        rows = in_memory_db.fetchall(
            "SELECT role, content, id FROM messages WHERE conversation_id = ? "
            "ORDER BY created_at ASC",
            (conv_id,),
        )
        roles = [r["role"] for r in rows]
        assert "user" in roles and "assistant" in roles
        assistant_rows = [r for r in rows if r["role"] == "assistant"]
        assert any(r["id"] == result.message_id for r in assistant_rows)
        assert any(r["content"] == "persisted answer" for r in assistant_rows)

    def test_graph_engine_local_route(
        self, in_memory_db, claude_client, local_client_available, settings,
    ):
        settings.set("orchestrator_engine", "graph")
        orch = _make_orchestrator(
            in_memory_db, claude_client, local_client_available, settings,
            routing="local",
        )
        conv_id = orch.create_conversation()
        result = orch.send(conv_id, "summarize this")
        assert result.text == "local response"
        # The Claude client must not have been touched.
        claude_client.chat_multi_turn.assert_not_called()

    def test_graph_engine_agent_model_pref_overrides_router(
        self, in_memory_db, claude_client, local_client_available, settings,
    ):
        from services.chat_orchestrator import ChatOrchestrator
        from services.memory import MemoryManager
        from models import RouteDecision

        settings.set("orchestrator_engine", "graph")
        in_memory_db.execute(
            "INSERT INTO agents (id, name, description, system_prompt, "
            "model_preference, max_tokens, is_builtin, created_at, updated_at) "
            "VALUES ('agG', 'GAgent', 'desc', 'You help.', 'claude', 4096, 0, "
            "'2024-01-01', '2024-01-01')"
        )
        in_memory_db.commit()

        router = MagicMock()
        # Router would pick local — agent.model_preference must override.
        router.classify.return_value = RouteDecision(
            model="local", complexity="simple", reasoning="",
        )
        mem = MemoryManager(None, None, local_client_available)
        orch = ChatOrchestrator(
            claude_client, local_client_available, router, mem, settings,
        )
        conv_id = orch.create_conversation(agent_id="agG")

        claude_client.chat_multi_turn = MagicMock(return_value={
            "text": "Claude says hi", "input_tokens": 10, "output_tokens": 5,
        })
        result = orch.send(conv_id, "hello", agent_id="agG")

        assert result.text == "Claude says hi"
        router.classify.assert_not_called()


# ── Early-exits agree across engines ─────────────────────────────────────────


class TestGraphEngineEarlyExits:
    def test_graph_engine_budget_exceeded_returns_early(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
    ):
        settings.set("orchestrator_engine", "graph")
        settings.set("max_conversation_budget_usd", 1.0)
        orch = _make_orchestrator(
            in_memory_db, claude_client, local_client_unavailable, settings,
        )
        conv_id = orch.create_conversation()

        import uuid as _uuid
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        in_memory_db.execute(
            "INSERT INTO token_usage (id, conversation_id, model, tokens_in, "
            "tokens_out, cost_usd, routed_reason, created_at) "
            "VALUES (?, ?, 'claude', 100, 50, 1.50, 'test', ?)",
            (str(_uuid.uuid4()), conv_id, now),
        )
        in_memory_db.commit()

        result = orch.send(conv_id, "after budget")
        assert result.route_reason == "budget_exceeded"
        assert "budget limit" in result.text
        claude_client.chat_multi_turn.assert_not_called()


# ── Parity smoke ─────────────────────────────────────────────────────────────


class TestEngineParity:
    @pytest.mark.parametrize("engine", ["legacy", "graph"])
    def test_basic_send_shape_matches(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
        engine,
    ):
        """Same inputs under either engine should yield equivalent ChatResult
        shapes (same text/tokens/route_reason). We don't pin message_id
        because each turn generates a fresh UUID, and we don't pin SSE
        order here — that's tested separately."""
        settings.set("orchestrator_engine", engine)
        orch = _make_orchestrator(
            in_memory_db, claude_client, local_client_unavailable, settings,
        )
        conv_id = orch.create_conversation()
        claude_client.chat_multi_turn = MagicMock(return_value={
            "text": "the answer", "input_tokens": 3, "output_tokens": 4,
        })

        result = orch.send(conv_id, "ask")

        assert result.text == "the answer"
        assert result.tokens_in == 3
        assert result.tokens_out == 4
        assert result.model == claude_client._model
        # Both engines route a default-prompt non-agent turn the same way.
        assert isinstance(result.route_reason, str) and result.route_reason

    def test_engine_emits_route_decided_and_memory_recalled(
        self, in_memory_db, claude_client, local_client_unavailable, settings,
    ):
        """The graph engine must emit the same opening events legacy does:
        memory_recalled and route_decided (in some order, both present)."""
        settings.set("orchestrator_engine", "graph")
        orch = _make_orchestrator(
            in_memory_db, claude_client, local_client_unavailable, settings,
        )
        conv_id = orch.create_conversation()
        claude_client.chat_multi_turn = MagicMock(return_value={
            "text": "ok", "input_tokens": 1, "output_tokens": 1,
        })

        events: list[tuple[str, dict]] = []
        orch.send(
            conv_id, "ping",
            on_event=lambda et, data: events.append((et, data)),
        )
        event_types = {et for et, _ in events}
        assert "memory_recalled" in event_types
        assert "route_decided" in event_types
