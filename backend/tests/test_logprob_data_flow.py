"""
tests/test_logprob_data_flow.py — QLPT Stage 1 end-to-end plumbing.

Two narrow data-flow tests:

  1. local_client.chat_unified for both LM Studio and Ollama populates
     the ``logprobs`` field on the returned unified dict from the
     respective backend's response shape.

  2. HubRouter.invoke threads ``logprobs`` from the unified dict into
     the returned WorkerResult, preserving the exact sequence.

The orchestrator is NOT spun up — these tests only validate the
plumbing between adjacent layers. Full-stack coverage already lives in
test_chat_orchestrator.py and the new test_escalation_ladder_margin_proxy.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.local_client import LocalClient
from services.hub_router import HubRouter
from models import RoutingDecision


# ── Helpers ──────────────────────────────────────────────────────────────────


def _settings_for(backend: str) -> MagicMock:
    """A MagicMock Settings keyed for either ``lm_studio`` or ``ollama``."""
    base = {
        "default_local_model": "qwen3-30b-a3b",
        "ollama_url":         "http://localhost:11434",
        "lm_studio_url":      "http://localhost:1234",
        "default_local_backend": backend,
        "local_backend_mode":    backend,
    }
    s = MagicMock()
    s.get.side_effect = lambda key, default=None: base.get(key, default)
    return s


def _post_response(json_payload: dict) -> MagicMock:
    """A requests.Response stub for POST calls (raise_for_status is no-op)."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_payload
    return resp


def _get_version(version: str) -> MagicMock:
    """A requests.Response stub for GET /api/version."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"version": version}
    return resp


# ── Test 1a: local_client (LM Studio) → unified dict ─────────────────────────


def test_lm_studio_chat_unified_returns_logprobs():
    """LM Studio path: mocked POST returns OpenAI-shape body; chat_unified
    surfaces the per-token logprobs on the returned dict unchanged."""
    client = LocalClient(_settings_for("lm_studio"))
    expected = [-0.10, -0.42]
    body = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "stub"},
                "logprobs": {
                    "content": [
                        {"token": "tok0", "logprob": expected[0], "bytes": []},
                        {"token": "tok1", "logprob": expected[1], "bytes": []},
                    ],
                },
            }
        ],
    }
    with patch(
        "services.local_client.requests.post",
        return_value=_post_response(body),
    ) as post:
        result = client.chat_unified("S", [{"role": "user", "content": "hi"}])
    # Confirm we hit the OpenAI-compat endpoint with logprobs requested.
    assert post.call_count == 1
    url_arg = post.call_args.args[0]
    assert "/v1/chat/completions" in url_arg
    payload = post.call_args.kwargs["json"]
    assert payload["logprobs"] is True
    assert payload["top_logprobs"] == 1
    # Plumbed into the unified dict.
    assert result["text"] == "stub"
    assert result["logprobs"] == expected


# ── Test 1b: local_client (Ollama) → unified dict ────────────────────────────


def test_ollama_chat_unified_returns_logprobs():
    """Ollama path: mocked /api/version satisfies the gate, mocked POST
    returns native-shape body; chat_unified surfaces the logprobs."""
    client = LocalClient(_settings_for("ollama"))
    expected = [-0.12, -0.55]
    body = {
        "model": "qwen3:30b",
        "message": {"role": "assistant", "content": "stub"},
        "done": True,
        "logprobs": [
            {"token": "tok0", "logprob": expected[0], "bytes": []},
            {"token": "tok1", "logprob": expected[1], "bytes": []},
        ],
    }
    with (
        patch(
            "services.local_client.requests.get",
            return_value=_get_version("0.13.0"),
        ) as get,
        patch(
            "services.local_client.requests.post",
            return_value=_post_response(body),
        ) as post,
    ):
        result = client.chat_unified("S", [{"role": "user", "content": "hi"}])
    # Version probe fired exactly once and routed to /api/version.
    assert get.call_count == 1
    assert "/api/version" in get.call_args.args[0]
    # Generation hit Ollama native /api/chat with logprobs requested.
    assert post.call_count == 1
    assert "/api/chat" in post.call_args.args[0]
    payload = post.call_args.kwargs["json"]
    assert payload["logprobs"] is True
    assert payload["top_logprobs"] == 1
    # Plumbed into the unified dict.
    assert result["text"] == "stub"
    assert result["logprobs"] == expected


# ── Test 2: hub_router.invoke → WorkerResult.logprobs ────────────────────────


def test_hub_router_invoke_propagates_logprobs_to_worker_result():
    """HubRouter.invoke threads logprobs from the local client's unified
    dict into the returned WorkerResult as a tuple."""
    expected = (-0.21, -0.34, -0.07)
    local = MagicMock()
    local.chat_unified.return_value = {
        "text":          "local answer",
        "input_tokens":  0,
        "output_tokens": 0,
        "logprobs":      list(expected),
    }
    local.client_name.return_value = "local-stub"
    claude = MagicMock()  # not invoked on the local path
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: default

    hub = HubRouter(claude, local, settings)
    decision = RoutingDecision(
        agent_id="a", backend="local", score=1.0,
        reasoning="data flow test", used_fallback=False, skill_matched="",
        thinking_budget=0,  # bypass qwen_thinking branch
    )

    result = hub.invoke(decision, "system", [{"role": "user", "content": "hi"}])

    assert result.text == "local answer"
    assert result.backend == "local"
    assert isinstance(result.logprobs, tuple)
    assert result.logprobs == expected


def test_hub_router_invoke_logprobs_none_when_client_returns_none():
    """When the local client's unified dict has logprobs=None (e.g.
    older Ollama), the WorkerResult.logprobs field stays None."""
    local = MagicMock()
    local.chat_unified.return_value = {
        "text": "local answer", "input_tokens": 0,
        "output_tokens": 0, "logprobs": None,
    }
    local.client_name.return_value = "local-stub"
    claude = MagicMock()
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: default

    hub = HubRouter(claude, local, settings)
    decision = RoutingDecision(
        agent_id="a", backend="local", score=1.0,
        reasoning="none case", used_fallback=False, skill_matched="",
        thinking_budget=0,
    )

    result = hub.invoke(decision, "system", [{"role": "user", "content": "hi"}])
    assert result.logprobs is None
