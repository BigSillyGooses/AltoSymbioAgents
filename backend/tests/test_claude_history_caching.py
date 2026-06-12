"""
tests/test_claude_history_caching.py — Perf Phase 3a: history cache breakpoint.

Asserts the contract of ClaudeClient._apply_history_cache:
  - flag OFF: the kwargs handed to the Anthropic SDK are byte-identical to a
    pre-Phase-3 client (and ``messages`` is the very same object),
  - flag ON: EXACTLY one cache_control block lands on the last assistant
    message before the final user message,
  - no assistant message → request unchanged,
  - block-list (vision) content gets the marker appended to its last block,
    not wrapped in another layer,
  - the total breakpoint budget (system + history) stays ≤ the API's 4.

The Anthropic SDK is replaced by a recording stand-in (no MagicMock kwargs
noise — recorded dicts compare with plain ==).
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import patch

SYSTEM = "You are a meticulous test assistant. " * 8


# ── Recording SDK stand-in ────────────────────────────────────────────────────

class _FakeStream:
    """Context manager matching the slice of messages.stream() we use."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return iter(["ok"])

    def get_final_usage(self):
        return SimpleNamespace(
            input_tokens=10, output_tokens=2,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )


class _RecordingSDK:
    """Stands in for ``Anthropic()``; records every create/stream kwargs."""

    def __init__(self, api_key: str = "test-key"):
        self.api_key = api_key
        self.create_kwargs: list[dict] = []
        self.stream_kwargs: list[dict] = []
        self.messages = self  # client._client.messages.create(...) resolves here

    def create(self, **kwargs):
        self.create_kwargs.append(kwargs)
        block = SimpleNamespace(type="text", text="ok")
        usage = SimpleNamespace(
            input_tokens=10, output_tokens=2,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        )
        return SimpleNamespace(content=[block], usage=usage, stop_reason="end_turn")

    def stream(self, **kwargs):
        self.stream_kwargs.append(kwargs)
        return _FakeStream()


def _make_client(**ctor_kwargs):
    """Real ClaudeClient constructed against the recording SDK."""
    from services.claude_client import ClaudeClient
    with patch("services.claude_client.Anthropic", _RecordingSDK):
        client = ClaudeClient(api_key="test-key", model="claude-sonnet-4-6",
                              **ctor_kwargs)
    return client, client._client


def _count_markers(node) -> int:
    """Count cache_control occurrences anywhere in a kwargs structure."""
    if isinstance(node, dict):
        return int("cache_control" in node) + sum(
            _count_markers(v) for k, v in node.items() if k != "cache_control"
        )
    if isinstance(node, (list, tuple)):
        return sum(_count_markers(v) for v in node)
    return 0


def _history() -> list:
    return [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
        {"role": "user", "content": "third question"},
    ]


# ── Flag off: byte-identical requests ─────────────────────────────────────────

def test_flag_off_create_kwargs_byte_identical():
    baseline, base_sdk = _make_client()  # pre-Phase-3 construction (no flag arg)
    flagged, flag_sdk = _make_client(use_history_caching=False)
    messages = _history()

    baseline.chat_multi_turn(SYSTEM, copy.deepcopy(messages))
    flagged.chat_multi_turn(SYSTEM, messages)

    assert base_sdk.create_kwargs == flag_sdk.create_kwargs
    # Flag off must pass through the SAME object, not a copy.
    assert flag_sdk.create_kwargs[0]["messages"] is messages


def test_flag_off_tools_and_stream_kwargs_byte_identical():
    baseline, base_sdk = _make_client()
    flagged, flag_sdk = _make_client(use_history_caching=False)
    messages = _history()
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]

    baseline.call_with_tools(SYSTEM, copy.deepcopy(messages), tools)
    flagged.call_with_tools(SYSTEM, messages, tools)
    assert base_sdk.create_kwargs == flag_sdk.create_kwargs
    assert flag_sdk.create_kwargs[0]["messages"] is messages

    baseline.stream_multi_turn(SYSTEM, copy.deepcopy(messages), lambda t: None)
    flagged.stream_multi_turn(SYSTEM, messages, lambda t: None)
    assert base_sdk.stream_kwargs == flag_sdk.stream_kwargs
    assert flag_sdk.stream_kwargs[0]["messages"] is messages


# ── Flag on: marker placement ─────────────────────────────────────────────────

def test_flag_on_marks_last_assistant_before_final_user():
    client, sdk = _make_client(use_history_caching=True)
    messages = _history()
    snapshot = copy.deepcopy(messages)

    client.chat_multi_turn(SYSTEM, messages)

    sent = sdk.create_kwargs[0]["messages"]
    # Exactly one marker in the whole messages payload…
    assert _count_markers(sent) == 1
    # …on index 3 (the last assistant before the final user message),
    # whose string content was converted to a single marked text block.
    assert sent[3]["role"] == "assistant"
    assert sent[3]["content"] == [{
        "type": "text",
        "text": "second answer",
        "cache_control": {"type": "ephemeral"},
    }]
    # Every other message is untouched.
    for i in (0, 1, 2, 4):
        assert sent[i] == snapshot[i]
    # The caller's list was deep-copied, never mutated.
    assert messages == snapshot


def test_flag_on_no_assistant_message_leaves_request_unchanged():
    client, sdk = _make_client(use_history_caching=True)
    messages = [{"role": "user", "content": "only question"}]

    client.chat_multi_turn(SYSTEM, messages)

    sent = sdk.create_kwargs[0]["messages"]
    assert _count_markers(sent) == 0
    assert sent == messages


def test_flag_on_vision_block_list_marks_last_block_not_wrapped():
    client, sdk = _make_client(use_history_caching=True)
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "AAAA"}},
        {"type": "text", "text": "what the image shows"},
    ]
    messages = [
        {"role": "user", "content": "look at this"},
        {"role": "assistant", "content": blocks},
        {"role": "user", "content": "and now?"},
    ]
    snapshot = copy.deepcopy(messages)

    client.chat_multi_turn(SYSTEM, messages)

    sent = sdk.create_kwargs[0]["messages"]
    assert _count_markers(sent) == 1
    content = sent[1]["content"]
    # Still the same two blocks — NOT wrapped in another list/text block.
    assert isinstance(content, list) and len(content) == 2
    assert "cache_control" not in content[0]
    assert content[1]["cache_control"] == {"type": "ephemeral"}
    assert content[1]["text"] == "what the image shows"
    assert messages == snapshot  # caller's list unmutated


def test_flag_on_applies_to_tools_and_stream_paths():
    client, sdk = _make_client(use_history_caching=True)
    messages = _history()
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]

    client.call_with_tools(SYSTEM, messages, tools)
    assert _count_markers(sdk.create_kwargs[0]["messages"]) == 1

    client.stream_multi_turn(SYSTEM, messages, lambda t: None)
    assert _count_markers(sdk.stream_kwargs[0]["messages"]) == 1


# ── Breakpoint budget ─────────────────────────────────────────────────────────

def test_total_breakpoints_within_api_limit():
    """System (1, via _build_system_with_cache) + history (1) = 2 of 4."""
    client, sdk = _make_client(use_caching=True, use_history_caching=True)
    client.chat_multi_turn(SYSTEM, _history())

    kwargs = sdk.create_kwargs[0]
    total = _count_markers(kwargs["system"]) + _count_markers(kwargs["messages"])
    assert total == 2
    assert total <= 4  # the Anthropic per-request cache_control ceiling
