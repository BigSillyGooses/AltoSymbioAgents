"""
tests/test_local_client_logprobs.py — QLPT Stage 1 local-client helpers.

Pins the four pure helpers added to LocalClient for logprob-aware
generation:

  - _parse_lm_studio_logprobs : OpenAI-compat response parser
  - _parse_ollama_logprobs    : Ollama native /api/chat response parser
  - _version_at_least         : dotted version comparison
  - _ollama_supports_logprobs : cached /api/version probe with one-shot warning

Test fixtures are realistic API response shapes (OpenAI logprobs schema,
Ollama native schema as documented at docs.ollama.com/api). Where a
helper has lenient or strict behavior on malformed input, the test
asserts EXACTLY what the helper does so future refactors that change
the contract are caught.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from services.local_client import LocalClient, OLLAMA_MIN_LOGPROBS_VERSION


# ── Helpers ──────────────────────────────────────────────────────────────────


def _client() -> LocalClient:
    """Construct a LocalClient with a MagicMock Settings.

    Only ``settings.get("ollama_url", default)`` is called inside the
    helpers under test, so the mock returns a stable URL.
    """
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: {
        "ollama_url":         "http://localhost:11434",
        "lm_studio_url":      "http://localhost:1234",
        "local_backend_mode": "auto",
    }.get(key, default)
    return LocalClient(settings)


def _lm_studio_response(logprobs: list[float]) -> dict:
    """A realistic LM Studio (OpenAI-compat) /v1/chat/completions body."""
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "model": "qwen3-30b-a3b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "stub answer"},
                "finish_reason": "stop",
                "logprobs": {
                    "content": [
                        {
                            "token": f"tok{i}",
                            "logprob": lp,
                            "bytes": [],
                            "top_logprobs": [
                                {"token": f"tok{i}", "logprob": lp, "bytes": []}
                            ],
                        }
                        for i, lp in enumerate(logprobs)
                    ],
                },
            },
        ],
    }


def _ollama_response(logprobs: list[float]) -> dict:
    """A realistic Ollama native /api/chat body, v0.12.11+."""
    return {
        "model": "qwen3:30b",
        "message": {"role": "assistant", "content": "stub answer"},
        "done": True,
        "logprobs": [
            {
                "token": f"tok{i}",
                "logprob": lp,
                "bytes": [0],
                "top_logprobs": [{"token": f"tok{i}", "logprob": lp, "bytes": [0]}],
            }
            for i, lp in enumerate(logprobs)
        ],
    }


# ── _parse_lm_studio_logprobs ────────────────────────────────────────────────


def test_lm_studio_parser_standard_shape():
    body = _lm_studio_response([-0.10, -0.42])
    out = LocalClient._parse_lm_studio_logprobs(body)
    assert out == [-0.10, -0.42]


def test_lm_studio_parser_no_logprobs_key():
    body = _lm_studio_response([-0.1])
    # Drop the logprobs key entirely.
    body["choices"][0].pop("logprobs")
    assert LocalClient._parse_lm_studio_logprobs(body) is None


def test_lm_studio_parser_empty_content_returns_none():
    body = _lm_studio_response([])
    body["choices"][0]["logprobs"] = {"content": []}
    # Empty content → helper returns None (cannot derive a sequence).
    assert LocalClient._parse_lm_studio_logprobs(body) is None


def test_lm_studio_parser_entry_missing_logprob_field():
    body = _lm_studio_response([-0.2, -0.3])
    # Strip the logprob field on the second token.
    body["choices"][0]["logprobs"]["content"][1].pop("logprob")
    # Helper rejects the whole array (return None) when any entry is
    # malformed — partial parsing would silently lose tokens.
    assert LocalClient._parse_lm_studio_logprobs(body) is None


def test_lm_studio_parser_non_dict_inputs_return_none():
    assert LocalClient._parse_lm_studio_logprobs(None) is None
    assert LocalClient._parse_lm_studio_logprobs([1, 2, 3]) is None
    assert LocalClient._parse_lm_studio_logprobs("not json") is None


def test_lm_studio_parser_int_logprob_coerces_to_float():
    body = _lm_studio_response([0])
    body["choices"][0]["logprobs"]["content"][0]["logprob"] = 0
    out = LocalClient._parse_lm_studio_logprobs(body)
    assert out == [0.0]
    assert isinstance(out[0], float)


def test_lm_studio_parser_bool_logprob_rejected():
    body = _lm_studio_response([-0.1])
    body["choices"][0]["logprobs"]["content"][0]["logprob"] = True
    assert LocalClient._parse_lm_studio_logprobs(body) is None


# ── _parse_ollama_logprobs ───────────────────────────────────────────────────


def test_ollama_parser_standard_shape():
    body = _ollama_response([-0.12, -0.55])
    out = LocalClient._parse_ollama_logprobs(body)
    assert out == [-0.12, -0.55]


def test_ollama_parser_no_logprobs_key():
    body = _ollama_response([-0.1])
    body.pop("logprobs")
    assert LocalClient._parse_ollama_logprobs(body) is None


def test_ollama_parser_empty_array_returns_none():
    body = _ollama_response([])
    body["logprobs"] = []
    assert LocalClient._parse_ollama_logprobs(body) is None


def test_ollama_parser_entry_missing_logprob_field():
    body = _ollama_response([-0.2, -0.7])
    body["logprobs"][1].pop("logprob")
    assert LocalClient._parse_ollama_logprobs(body) is None


def test_ollama_parser_non_dict_inputs_return_none():
    assert LocalClient._parse_ollama_logprobs(None) is None
    assert LocalClient._parse_ollama_logprobs([1, 2, 3]) is None
    assert LocalClient._parse_ollama_logprobs("garbage") is None


def test_ollama_parser_bool_logprob_rejected():
    body = _ollama_response([-0.1])
    body["logprobs"][0]["logprob"] = False
    assert LocalClient._parse_ollama_logprobs(body) is None


# ── _version_at_least ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "actual,minimum,expected",
    [
        ("0.12.11", OLLAMA_MIN_LOGPROBS_VERSION, True),
        ("0.12.12", OLLAMA_MIN_LOGPROBS_VERSION, True),
        ("0.12.10", OLLAMA_MIN_LOGPROBS_VERSION, False),
        ("0.13.0",  OLLAMA_MIN_LOGPROBS_VERSION, True),
        ("1.0.0",   OLLAMA_MIN_LOGPROBS_VERSION, True),
        ("0.11.99", OLLAMA_MIN_LOGPROBS_VERSION, False),
        ("0",       "0.12.11",                    False),
    ],
)
def test_version_at_least_numeric(actual, minimum, expected):
    assert LocalClient._version_at_least(actual, minimum) is expected


def test_version_at_least_empty_string_returns_false():
    # Empty string → parse() returns () which is < (0, 12, 11).
    assert LocalClient._version_at_least("", "0.12.11") is False


def test_version_at_least_garbage_returns_false():
    # Non-numeric leading chunk → parse() returns ()
    assert LocalClient._version_at_least("garbage", "0.12.11") is False


def test_version_at_least_none_returns_false():
    # str(None) == "None"; parse() finds no leading digits → False.
    assert LocalClient._version_at_least(None, "0.12.11") is False


def test_version_at_least_strips_pre_release_suffix():
    # "0.12.11-dev" parses as (0, 12, 11) per the docstring (suffix
    # stripped at first non-digit) — equal to the minimum, returns True.
    assert LocalClient._version_at_least("0.12.11-dev", "0.12.11") is True


def test_version_at_least_strips_plus_build_suffix():
    # "0.12.11+rc1" → (0, 12, 11); satisfies the gate.
    assert LocalClient._version_at_least("0.12.11+rc1", "0.12.11") is True


def test_version_at_least_handles_higher_pre_release():
    # "0.13.0-rc1" → (0, 13, 0) > (0, 12, 11).
    assert LocalClient._version_at_least("0.13.0-rc1", "0.12.11") is True


# ── _ollama_supports_logprobs ────────────────────────────────────────────────


def _stub_response(*, status: int = 200, json_payload=None, raises=None):
    """A requests.Response stub for the version probe."""
    if raises is not None:
        raise raises
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_payload
    return resp


def test_supports_logprobs_returns_true_when_version_meets_min():
    client = _client()
    with patch(
        "services.local_client.requests.get",
        return_value=_stub_response(json_payload={"version": "0.13.0"}),
    ) as get:
        assert client._ollama_supports_logprobs() is True
    assert get.call_count == 1


def test_supports_logprobs_caches_within_session():
    client = _client()
    with patch(
        "services.local_client.requests.get",
        return_value=_stub_response(json_payload={"version": "0.13.0"}),
    ) as get:
        first = client._ollama_supports_logprobs()
        second = client._ollama_supports_logprobs()
        third = client._ollama_supports_logprobs()
    assert first is True and second is True and third is True
    # Three call-site invocations, one HTTP probe — cache holds.
    assert get.call_count == 1


def test_supports_logprobs_returns_false_when_version_below_min(caplog):
    client = _client()
    with (
        patch(
            "services.local_client.requests.get",
            return_value=_stub_response(json_payload={"version": "0.12.10"}),
        ),
        caplog.at_level(logging.WARNING, logger="altosybioagents.local"),
    ):
        assert client._ollama_supports_logprobs() is False
    # First negative result emits a one-time warning naming the min version.
    assert any(
        OLLAMA_MIN_LOGPROBS_VERSION in rec.getMessage()
        and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_supports_logprobs_warning_emitted_only_once(caplog):
    client = _client()
    with (
        patch(
            "services.local_client.requests.get",
            return_value=_stub_response(json_payload={"version": "0.12.10"}),
        ),
        caplog.at_level(logging.WARNING, logger="altosybioagents.local"),
    ):
        client._ollama_supports_logprobs()
        first_count = sum(
            1 for r in caplog.records
            if r.levelno == logging.WARNING
            and OLLAMA_MIN_LOGPROBS_VERSION in r.getMessage()
        )
        client._ollama_supports_logprobs()
        client._ollama_supports_logprobs()
        final_count = sum(
            1 for r in caplog.records
            if r.levelno == logging.WARNING
            and OLLAMA_MIN_LOGPROBS_VERSION in r.getMessage()
        )
    assert first_count == 1
    assert final_count == 1


def test_supports_logprobs_returns_false_on_malformed_json():
    client = _client()
    bad = MagicMock()
    bad.status_code = 200
    bad.json.side_effect = ValueError("not json")
    with patch("services.local_client.requests.get", return_value=bad):
        assert client._ollama_supports_logprobs() is False


def test_supports_logprobs_returns_false_on_request_exception():
    client = _client()
    with patch(
        "services.local_client.requests.get",
        side_effect=ConnectionError("ollama down"),
    ):
        assert client._ollama_supports_logprobs() is False


def test_supports_logprobs_returns_false_on_non_200_status():
    client = _client()
    with patch(
        "services.local_client.requests.get",
        return_value=_stub_response(status=503, json_payload={"version": "0.13.0"}),
    ):
        assert client._ollama_supports_logprobs() is False


def test_supports_logprobs_returns_false_on_missing_version_key():
    client = _client()
    with patch(
        "services.local_client.requests.get",
        return_value=_stub_response(json_payload={"build": "abc"}),
    ):
        # Missing "version" → parse("") → () → < (0,12,11) → False.
        assert client._ollama_supports_logprobs() is False
