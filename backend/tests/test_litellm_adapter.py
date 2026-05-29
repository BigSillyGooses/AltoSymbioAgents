"""
tests/test_litellm_adapter.py — LiteLLMClient unit tests.

Stage-1 Atelier: the third LLMClient implementation. These tests pin the
adapter's contract against a mocked ``litellm.completion`` so the real
provider call never fires. The integration test that exercises the live
LiteLLM call belongs upstream (BYO-key, paid quota) and is intentionally
out of scope here.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from services.llm_litellm_adapter import LiteLLMClient


# ── Fake response object that mimics LiteLLM's normalised shape ─────────────

def _make_fake_response(text: str, prompt_tokens: int, completion_tokens: int):
    message = types.SimpleNamespace(content=text)
    choice = types.SimpleNamespace(message=message)
    usage = types.SimpleNamespace(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
    )
    return types.SimpleNamespace(choices=[choice], usage=usage)


def _make_fake_chunk(delta_text: str | None, usage=None):
    delta = types.SimpleNamespace(content=delta_text)
    choice = types.SimpleNamespace(delta=delta)
    return types.SimpleNamespace(choices=[choice], usage=usage)


@pytest.fixture
def patched_completion(monkeypatch):
    """Replace ``litellm.completion`` so no real provider call ever fires."""
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = MagicMock()
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    return fake_litellm.completion


# ── Basic adapter contract ──────────────────────────────────────────────────

def test_is_available_requires_both_model_and_key():
    """An adapter with no key returns False so HubRouter fails closed."""
    assert LiteLLMClient(model="openai/gpt-4o", api_key="").is_available() is False
    assert LiteLLMClient(model="", api_key="sk-test").is_available() is False
    assert LiteLLMClient(model="openai/gpt-4o", api_key="sk-test").is_available() is True


def test_client_name_returns_the_configured_model():
    c = LiteLLMClient(model="groq/llama-3.3-70b-versatile", api_key="x")
    assert c.client_name() == "groq/llama-3.3-70b-versatile"


def test_client_name_falls_back_when_model_unset():
    c = LiteLLMClient(model="", api_key="")
    assert c.client_name() == "litellm"


def test_update_config_replaces_model_and_key():
    c = LiteLLMClient(model="openai/gpt-4o-mini", api_key="old")
    c.update_config(model="gemini/gemini-2.5-flash", api_key="new")
    assert c._model == "gemini/gemini-2.5-flash"
    assert c._api_key == "new"


# ── chat_unified ────────────────────────────────────────────────────────────

def test_chat_unified_prepends_system_message(patched_completion):
    """The system prompt rides in the messages list as role=system."""
    patched_completion.return_value = _make_fake_response("ok", 12, 34)
    c = LiteLLMClient(model="openai/gpt-4o-mini", api_key="sk-test")

    result = c.chat_unified(
        system="be brief",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=512,
    )

    assert result == {"text": "ok", "input_tokens": 12, "output_tokens": 34}
    kwargs = patched_completion.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-4o-mini"
    assert kwargs["api_key"] == "sk-test"
    assert kwargs["max_tokens"] == 512
    assert kwargs["stream"] is False
    assert kwargs["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]


def test_chat_unified_omits_system_when_empty(patched_completion):
    """An empty system prompt should NOT add a stray system message."""
    patched_completion.return_value = _make_fake_response("", 0, 0)
    c = LiteLLMClient(model="openai/gpt-4o-mini", api_key="sk-test")

    c.chat_unified(system="", messages=[{"role": "user", "content": "x"}])

    assert patched_completion.call_args.kwargs["messages"] == [
        {"role": "user", "content": "x"},
    ]


def test_chat_unified_provider_error_returns_sentinel(patched_completion):
    """Provider error returns a well-formed dict so callers don't crash."""
    patched_completion.side_effect = ConnectionError("boom")
    c = LiteLLMClient(model="openai/gpt-4o-mini", api_key="sk-test")

    result = c.chat_unified(system="", messages=[{"role": "user", "content": "x"}])

    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert "LiteLLM call failed" in result["text"]


def test_chat_unified_tolerates_missing_usage(patched_completion):
    """Some providers (Ollama via LiteLLM, mostly) omit usage entirely."""
    resp = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="hi"))],
        usage=None,
    )
    patched_completion.return_value = resp
    c = LiteLLMClient(model="openai/gpt-4o-mini", api_key="sk-test")

    result = c.chat_unified(system="", messages=[{"role": "user", "content": "x"}])

    assert result == {"text": "hi", "input_tokens": 0, "output_tokens": 0}


# ── stream_unified ──────────────────────────────────────────────────────────

def test_stream_unified_accumulates_chunks_and_invokes_callback(patched_completion):
    chunks = [
        _make_fake_chunk("Hel"),
        _make_fake_chunk("lo "),
        _make_fake_chunk("world"),
        _make_fake_chunk(
            None,
            usage=types.SimpleNamespace(prompt_tokens=4, completion_tokens=3),
        ),
    ]
    patched_completion.return_value = iter(chunks)
    c = LiteLLMClient(model="openai/gpt-4o-mini", api_key="sk-test")
    received: list[str] = []

    result = c.stream_unified(
        system="sys",
        messages=[{"role": "user", "content": "go"}],
        on_token=received.append,
        max_tokens=128,
    )

    assert "".join(received) == "Hello world"
    assert result["text"] == "Hello world"
    assert result["input_tokens"] == 4
    assert result["output_tokens"] == 3
    assert patched_completion.call_args.kwargs["stream"] is True
    # include_usage opt-in for the final chunk's usage to arrive.
    assert patched_completion.call_args.kwargs["stream_options"] == {
        "include_usage": True,
    }


def test_stream_unified_falls_back_to_chat_unified_on_error(patched_completion):
    """A failing stream should still return a usable result, not bubble up."""
    # The first call (streaming) raises; the second (fallback chat_unified
    # path) returns a plain response.
    patched_completion.side_effect = [
        ConnectionError("stream died"),
        _make_fake_response("recovered", 1, 2),
    ]
    c = LiteLLMClient(model="openai/gpt-4o-mini", api_key="sk-test")

    result = c.stream_unified(
        system="",
        messages=[{"role": "user", "content": "x"}],
        on_token=lambda _: None,
    )

    assert result == {"text": "recovered", "input_tokens": 1, "output_tokens": 2}
