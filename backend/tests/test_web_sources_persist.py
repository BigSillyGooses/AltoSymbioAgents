"""tests/test_web_sources_persist.py — web-source provenance survives a reload.

The source chips used to live only in the live SSE event stream, so they
vanished when the assistant message was saved and reloaded from SQLite. These
tests prove the full persistence path with real components (real DB, real RAG
ingest with the deterministic test embedder, real local fixture server):

  1. _maybe_autofetch_urls returns the deduped sources it fetched (and [] when
     the flags are off — a flag-off turn is unchanged).
  2. A real solo turn persists those sources on the assistant message, and
     get_conversation_messages decodes them back into ``web_sources``.
  3. The read-path decode also revives ``pipeline_steps`` (the same latent gap
     this change fixes) and degrades gracefully on NULL/malformed JSON.
"""

from __future__ import annotations

import hashlib
import json
import math

import pytest

EMBED_DIM = 384


def _deterministic_embed(texts):
    out = []
    for t in texts:
        vec = [0.0] * EMBED_DIM
        for tok in (t or "").lower().split():
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % EMBED_DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out


@pytest.fixture
def orchestrator(in_memory_db, claude_client, local_client_unavailable,
                 settings, tmp_path, monkeypatch):
    """A real ChatOrchestrator wired to a real RAG + the deterministic embedder.

    Mirrors the wiring in test_web_to_rag_integration so the auto-fetch hook
    can fetch + index the loopback fixture for real, with web research on.
    """
    from unittest.mock import MagicMock
    from services import semantic_search
    from services.rag_index import RAGIndex
    from services.memory import MemoryManager
    from services.chat_orchestrator import ChatOrchestrator
    from models import RouteDecision
    from core import paths

    monkeypatch.setattr(semantic_search, "_embed_fn", _deterministic_embed)
    monkeypatch.setattr(semantic_search, "_embed_dim", EMBED_DIM)
    monkeypatch.setattr(semantic_search, "_initialized", True)
    monkeypatch.setattr(paths, "rag_cache_dir", lambda: tmp_path)

    settings.set("web_research_enabled", True)
    settings.set("web_research_auto_fetch", True)
    settings.set("web_research_allow_private", True)  # reach loopback fixture

    rag = RAGIndex(model=None)
    rag._semantic = semantic_search

    mem = MemoryManager(rag, semantic_search, local_client_unavailable, settings)

    router = MagicMock()
    router.classify.return_value = RouteDecision(
        model="claude", complexity="complex", reasoning="test",
    )
    return ChatOrchestrator(claude_client, local_client_unavailable, router,
                            mem, settings)


def test_maybe_autofetch_returns_sources(orchestrator, local_web_server):
    seen = []
    sources = orchestrator._maybe_autofetch_urls(
        f"please read {local_web_server.url('/')}", lambda *a: seen.append(a),
    )
    assert sources == [{"url": local_web_server.url("/"), "title": "Acme Widgets"}]


def test_maybe_autofetch_returns_empty_when_flag_off(orchestrator, local_web_server):
    orchestrator._settings.set("web_research_auto_fetch", False)
    assert orchestrator._maybe_autofetch_urls(
        f"read {local_web_server.url('/')}", lambda *a: None,
    ) == []


def test_solo_turn_persists_and_reloads_web_sources(orchestrator, local_web_server):
    import db

    conv_id = orchestrator.create_conversation()
    orchestrator.send(conv_id, f"What is at {local_web_server.url('/')} ?")

    # Persisted on the assistant row as JSON.
    row = db.fetchone(
        "SELECT web_sources_json FROM messages "
        "WHERE conversation_id = ? AND role = 'assistant'",
        (conv_id,),
    )
    assert row is not None and row["web_sources_json"]
    stored = json.loads(row["web_sources_json"])
    assert stored == [{"url": local_web_server.url("/"), "title": "Acme Widgets"}]

    # And the read path decodes it back into the clean field the UI reads.
    msgs = orchestrator.get_conversation_messages(conv_id)
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    assert assistant["web_sources"] == [
        {"url": local_web_server.url("/"), "title": "Acme Widgets"},
    ]


def test_decode_message_row_revives_both_json_columns():
    from services.chat_orchestrator import ChatOrchestrator

    decoded = ChatOrchestrator._decode_message_row({
        "id": "m1",
        "pipeline_steps_json": json.dumps([{"step": 1, "agent": "Researcher"}]),
        "web_sources_json": json.dumps([{"url": "https://x", "title": "X"}]),
    })
    assert decoded["pipeline_steps"] == [{"step": 1, "agent": "Researcher"}]
    assert decoded["web_sources"] == [{"url": "https://x", "title": "X"}]


def test_decode_message_row_handles_null_and_malformed():
    from services.chat_orchestrator import ChatOrchestrator

    null_row = ChatOrchestrator._decode_message_row(
        {"id": "m2", "pipeline_steps_json": None, "web_sources_json": None},
    )
    assert null_row["pipeline_steps"] == []
    assert null_row["web_sources"] == []

    bad_row = ChatOrchestrator._decode_message_row(
        {"id": "m3", "web_sources_json": "{not json", "pipeline_steps_json": "42"},
    )
    assert bad_row["web_sources"] == []
    assert bad_row["pipeline_steps"] == []  # valid JSON but not a list → []
