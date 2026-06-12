"""
tests/test_cost_predictor.py — Perf Phase 4: pre-turn cost prediction.

Covers the predictor itself (deterministic heuristic numbers, price-table
agreement with the orchestrator's ``_estimate_cost``, the rolling output
estimate over seeded token_usage rows, cached-portion pricing, the
api_count opt-in + fallback), the orchestrator wiring (flag-off turns stay
byte-identical and write NULL predicted_cost_usd; the block flag
short-circuits over-budget turns without invoking a worker), and the
POST /api/usage/predict route.

The orchestrator tests reuse the perf harness's deterministic fake clients
(benchmarks/perf/fake_clients.py) — the same wiring the chat_short scenario
drives through the production send() path — hence the sys.path insert below
(the harness lives at the repo root while pytest's rootdir is backend/).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services import cost_predictor
from services.cost_predictor import CostPrediction, predict

MODEL = "claude-sonnet-4-6"


def _prices(settings=None) -> tuple[float, float]:
    from core.model_catalog import get_catalog
    return get_catalog().prices_for_model(MODEL, None)


def _seed_usage_rows(db, conversation_id: str, tokens_out_values: list[int]) -> None:
    """Insert token_usage rows with strictly increasing created_at so the
    predictor's ORDER BY created_at DESC window is deterministic."""
    for i, out in enumerate(tokens_out_values):
        db.execute(
            "INSERT INTO token_usage (id, conversation_id, model, tokens_in, "
            "tokens_out, cost_usd, routed_reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"u-{conversation_id}-{i:03d}", conversation_id, MODEL,
             100, out, 0.01, "test", f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"),
        )
    db.commit()


# ── Deterministic heuristic numbers ───────────────────────────────────────────

class TestHeuristic:
    def test_known_strings_exact_token_counts(self):
        # system: 400 chars → 100 tokens; user content: 41 chars → 10 tokens.
        result = predict(
            "s" * 400,
            [{"role": "user", "content": "u" * 41}],
            MODEL,
            {},
        )
        assert isinstance(result, CostPrediction)
        assert result.est_input_tokens == 110
        assert result.est_cached_tokens == 0
        # No conversation history → 4096 × default fraction 0.5.
        assert result.est_output_tokens == 2048
        assert result.method == "heuristic"

    def test_block_list_content_counts_text_blocks(self):
        import json
        image_block = {"type": "image", "source": {"type": "base64", "data": "AAAA"}}
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "t" * 80},        # 80 chars
                image_block,                                # sorted-JSON render
            ],
        }]
        expected = (80 + len(json.dumps(image_block, sort_keys=True, default=str))) // 4
        result = predict("", messages, MODEL, {})
        assert result.est_input_tokens == expected

    def test_output_fraction_setting_scales_fallback(self):
        result = predict("s" * 400, [{"role": "user", "content": "hi"}], MODEL,
                         {"cost_prediction_output_fraction": 0.25})
        assert result.est_output_tokens == 1024


# ── Price agreement with _estimate_cost ───────────────────────────────────────

class TestPriceAgreement:
    def test_zero_cached_prediction_equals_estimate_cost(self, in_memory_db):
        """predict() on N input / M output tokens with no cached portion must
        price EXACTLY like the orchestrator's post-turn _estimate_cost — the
        same catalog, the same override extraction."""
        from services.chat_orchestrator import _estimate_cost

        conv = "conv-price-agree"
        _seed_usage_rows(in_memory_db, conv, [100, 200, 300])  # mean M = 200

        system = "s" * 4000           # 1000 tokens
        messages = [{"role": "user", "content": "m" * 400}]  # 100 tokens
        settings = {}

        result = predict(system, messages, MODEL, settings, conversation_id=conv)
        assert result.est_cached_tokens == 0
        assert result.est_input_tokens == 1100
        assert result.est_output_tokens == 200
        assert result.est_cost_usd == pytest.approx(
            _estimate_cost(MODEL, 1100, 200, settings),
        )

    def test_user_price_overrides_flow_through(self, in_memory_db):
        from services.chat_orchestrator import _estimate_cost

        settings = {"model_prices": {"sonnet": [1.0, 2.0]}}
        result = predict("s" * 400, [{"role": "user", "content": "m" * 40}],
                         MODEL, settings)
        assert result.est_cost_usd == pytest.approx(
            _estimate_cost(MODEL, result.est_input_tokens,
                           result.est_output_tokens, settings),
        )


# ── Output estimate: rolling mean over token_usage ────────────────────────────

class TestOutputEstimate:
    def test_rolling_mean_of_seeded_rows(self, in_memory_db):
        conv = "conv-rolling"
        _seed_usage_rows(in_memory_db, conv, [50, 150, 100])  # mean 100
        result = predict("s" * 400, [{"role": "user", "content": "hi"}],
                         MODEL, {}, conversation_id=conv)
        assert result.est_output_tokens == 100

    def test_window_is_last_ten_rows(self, in_memory_db):
        conv = "conv-window"
        # Two old outliers followed by ten recent rows of 10 each: only the
        # newest 10 may count → mean 10, not (2×1000 + 10×10)/12.
        _seed_usage_rows(in_memory_db, conv, [1000, 1000] + [10] * 10)
        result = predict("s" * 400, [{"role": "user", "content": "hi"}],
                         MODEL, {}, conversation_id=conv)
        assert result.est_output_tokens == 10

    def test_no_history_falls_back_to_fraction(self, in_memory_db):
        result = predict("s" * 400, [{"role": "user", "content": "hi"}],
                         MODEL, {}, conversation_id="conv-without-rows")
        assert result.est_output_tokens == 2048


# ── Cached-portion pricing (claude_history_caching on) ────────────────────────

class TestCachedPortion:
    SYSTEM = "s" * 400                                     # 100 tokens
    MESSAGES = [
        {"role": "user", "content": "a" * 40},             # 10 tokens
        {"role": "assistant", "content": "b" * 80},        # 20 tokens
        {"role": "user", "content": "c" * 20},             # 5 tokens
    ]

    def test_stable_prefix_priced_at_cache_read_rate(self):
        result = predict(self.SYSTEM, self.MESSAGES, MODEL,
                         {"claude_history_caching": True})
        # Stable prefix = system + everything before the final user message.
        assert result.est_cached_tokens == 100 + 10 + 20
        assert result.est_input_tokens == 5

        price_in, price_out = _prices()
        expected = (
            5 * price_in
            + 130 * price_in * 0.1
            + result.est_output_tokens * price_out
        ) / 1_000_000
        assert result.est_cost_usd == pytest.approx(expected)

    def test_flag_off_means_no_cached_tokens(self):
        result = predict(self.SYSTEM, self.MESSAGES, MODEL, {})
        assert result.est_cached_tokens == 0
        assert result.est_input_tokens == 135

    def test_first_turn_caches_system_only(self):
        result = predict(self.SYSTEM, [{"role": "user", "content": "c" * 20}],
                         MODEL, {"claude_history_caching": True})
        assert result.est_cached_tokens == 100
        assert result.est_input_tokens == 5


# ── Non-Claude models predict $0 ──────────────────────────────────────────────

class TestNonClaude:
    def test_local_model_costs_nothing_but_keeps_token_estimates(self):
        result = predict("s" * 400, [{"role": "user", "content": "m" * 40}],
                         "qwen3:8b-instruct", {"claude_history_caching": True})
        assert result.est_cost_usd == 0.0
        assert result.est_input_tokens > 0
        assert result.est_cached_tokens == 0  # caching estimate is Claude-only

    def test_empty_model_costs_nothing(self):
        result = predict("s" * 400, [{"role": "user", "content": "hi"}], "", {})
        assert result.est_cost_usd == 0.0


# ── api_count opt-in + fallback ───────────────────────────────────────────────

class _CountingStub:
    def __init__(self, value=None, exc: Exception | None = None):
        self._value = value
        self._exc = exc
        self.calls = 0

    def count_tokens(self, system, messages):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._value


class TestApiCount:
    MESSAGES = [{"role": "user", "content": "m" * 40}]

    def test_api_count_used_when_enabled(self):
        stub = _CountingStub(value=5000)
        result = predict("s" * 400, self.MESSAGES, MODEL,
                         {"cost_prediction_use_api_count": True},
                         claude_client=stub)
        assert stub.calls == 1
        assert result.method == "api_count"
        assert result.est_input_tokens == 5000

    def test_any_failure_falls_back_to_heuristic(self):
        stub = _CountingStub(exc=RuntimeError("api exploded"))
        result = predict("s" * 400, self.MESSAGES, MODEL,
                         {"cost_prediction_use_api_count": True},
                         claude_client=stub)
        assert stub.calls == 1
        assert result.method == "heuristic"
        assert result.est_input_tokens == 110

    def test_flag_off_never_consults_the_client(self):
        stub = _CountingStub(value=5000)
        result = predict("s" * 400, self.MESSAGES, MODEL, {}, claude_client=stub)
        assert stub.calls == 0
        assert result.method == "heuristic"

    def test_non_claude_model_skips_api_count(self):
        stub = _CountingStub(value=5000)
        result = predict("s" * 400, self.MESSAGES, "qwen3:8b",
                         {"cost_prediction_use_api_count": True},
                         claude_client=stub)
        assert stub.calls == 0
        assert result.method == "heuristic"


def test_claude_client_count_tokens_wrapper(claude_client, mock_anthropic):
    """The thin SDK wrapper: short per-call timeout via with_options, exact
    kwargs through to messages.count_tokens, int(input_tokens) back."""
    counted = MagicMock(input_tokens=123)
    mock_anthropic.with_options.return_value.messages.count_tokens.return_value = counted

    messages = [{"role": "user", "content": "hi"}]
    assert claude_client.count_tokens("sys prompt", messages) == 123

    mock_anthropic.with_options.assert_called_once_with(timeout=10.0)
    kwargs = (
        mock_anthropic.with_options.return_value.messages.count_tokens
        .call_args.kwargs
    )
    assert kwargs["system"] == "sys prompt"
    assert kwargs["messages"] is messages
    assert kwargs["model"] == claude_client._model


# ── Orchestrator wiring: flags off / prediction on / block path ───────────────

def _settings(**overrides) -> dict:
    base = {
        "system_prompt": "You are a meticulous, terse test assistant. " * 4,
        "high_stakes_voting_enabled": False,
        "interleaved_reasoning_enabled": False,
        "escalation_channel_enabled": False,
        "max_conversation_budget_usd": 5.0,
        "budget_warning_threshold_pct": 80.0,
    }
    base.update(overrides)
    return base


def _make_orchestrator(settings: dict):
    """The chat_short wiring: real ChatOrchestrator over the deterministic
    perf-harness fakes (scripted Claude replies, no-fact local client,
    fixed-route task router)."""
    from services.chat_orchestrator import ChatOrchestrator
    from services.memory import MemoryManager

    from benchmarks.perf.fake_clients import (
        FakeClaudeClient, FakeLocalClient, FakeTaskRouter,
    )

    claude = FakeClaudeClient(replies=["A short scripted reply."])
    local = FakeLocalClient()
    memory = MemoryManager(None, None, local, settings)
    orchestrator = ChatOrchestrator(
        claude, local, FakeTaskRouter(model="claude", complexity="simple"),
        memory, settings,
    )
    return orchestrator, claude


class TestOrchestratorIntegration:
    MESSAGE = "Please summarize the quarterly planning document."

    def _send(self, orchestrator, conversation_id):
        events: list[tuple[str, dict]] = []
        result = orchestrator.send(
            conversation_id, self.MESSAGE,
            on_event=lambda t, d: events.append((t, dict(d))),
        )
        return result, events

    def test_flags_off_no_event_and_null_predicted_cost(self, in_memory_db):
        orchestrator, claude = _make_orchestrator(_settings())
        cid = orchestrator.create_conversation()

        result, events = self._send(orchestrator, cid)

        assert result.route_reason != "budget_predicted_exceeded"
        assert all(t != "cost_predicted" for t, _ in events)
        row = in_memory_db.fetchone(
            "SELECT predicted_cost_usd FROM token_usage "
            "WHERE conversation_id = ?", (cid,),
        )
        assert row is not None
        assert row["predicted_cost_usd"] is None  # SQL NULL, pre-Phase-4 value
        assert len(claude.calls) == 1  # exactly the normal worker dispatch

    def test_prediction_on_emits_event_and_does_not_perturb_the_turn(
            self, in_memory_db):
        # Control run: flags off.
        orch_off, _ = _make_orchestrator(_settings())
        cid_off = orch_off.create_conversation()
        result_off, _ = self._send(orch_off, cid_off)

        # Prediction on (block off, generous budget): same reply, same
        # tokens, same cost — prediction is read-only.
        orch_on, claude_on = _make_orchestrator(
            _settings(cost_prediction_enabled=True),
        )
        cid_on = orch_on.create_conversation()
        result_on, events = self._send(orch_on, cid_on)

        predicted = [d for t, d in events if t == "cost_predicted"]
        assert len(predicted) == 1
        payload = predicted[0]
        assert payload["conversation_id"] == cid_on
        assert payload["method"] == "heuristic"
        assert payload["est_input_tokens"] > 0
        assert payload["est_cached_tokens"] == 0
        assert payload["est_output_tokens"] == 2048
        assert payload["est_cost_usd"] > 0

        assert result_on.text == result_off.text
        assert result_on.tokens_in == result_off.tokens_in
        assert result_on.tokens_out == result_off.tokens_out
        assert result_on.cost_usd == pytest.approx(result_off.cost_usd)
        assert len(claude_on.calls) == 1

        row = in_memory_db.fetchone(
            "SELECT predicted_cost_usd FROM token_usage "
            "WHERE conversation_id = ?", (cid_on,),
        )
        assert row["predicted_cost_usd"] == pytest.approx(
            payload["est_cost_usd"],
        )

    def test_block_flag_short_circuits_over_budget_turn(self, in_memory_db):
        # Tiny budget: the output estimate alone (2048 tokens at Claude
        # output prices ≈ $0.03) exceeds it, so the guard must fire.
        orchestrator, claude = _make_orchestrator(_settings(
            cost_prediction_enabled=True,
            cost_prediction_block_over_budget=True,
            max_conversation_budget_usd=0.001,
        ))
        cid = orchestrator.create_conversation()

        result, events = self._send(orchestrator, cid)

        # Budget-shaped result, mirrored from the budget_exceeded path.
        assert result.route_reason == "budget_predicted_exceeded"
        assert result.model == ""
        assert result.tokens_in == 0
        assert result.tokens_out == 0
        assert result.cost_usd == 0.0
        assert "budget" in result.text

        # The prediction event still fired (UI sees why the turn stopped).
        assert any(t == "cost_predicted" for t, _ in events)

        # No worker was invoked (the fake hub's client records every call).
        assert claude.calls == []

        # The user message is persisted by open(); no assistant message and
        # no token_usage row exist for the blocked turn.
        roles = [
            r["role"] for r in in_memory_db.fetchall(
                "SELECT role FROM messages WHERE conversation_id = ? "
                "ORDER BY created_at ASC", (cid,),
            )
        ]
        assert roles == ["user"]
        assert in_memory_db.fetchall(
            "SELECT id FROM token_usage WHERE conversation_id = ?", (cid,),
        ) == []

    def test_block_flag_alone_does_nothing_without_prediction(self, in_memory_db):
        """The guard rides on cost_prediction_enabled — block flag on its own
        must leave the turn untouched (no prediction exists to act on)."""
        orchestrator, claude = _make_orchestrator(_settings(
            cost_prediction_block_over_budget=True,
            max_conversation_budget_usd=0.001,
        ))
        cid = orchestrator.create_conversation()

        result, events = self._send(orchestrator, cid)

        assert result.route_reason != "budget_predicted_exceeded"
        assert all(t != "cost_predicted" for t, _ in events)
        assert len(claude.calls) == 1


# ── Route: POST /api/usage/predict ────────────────────────────────────────────

TOKEN = "test-token-usage-predict"


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def predict_app(in_memory_db, tmp_path):
    """Per-router app (the test_voice_routes pattern): only the usage router,
    a stub container whose ``api`` is a real UsageAPI over a facade-lite."""
    from core.api.usage import UsageAPI
    from core.settings import Settings
    from routes import usage as usage_routes
    from server import BearerAuthMiddleware

    a = FastAPI()
    a.add_middleware(BearerAuthMiddleware, expected_token=TOKEN)
    a.include_router(usage_routes.router, prefix="/api/usage")

    settings = Settings(tmp_path / "settings.json")
    facade = SimpleNamespace(
        _settings=settings,
        _claude=None,
        _log=logging.getLogger("test.usage_predict"),
    )
    a.state.container = SimpleNamespace(api=UsageAPI(facade))
    return a


class TestPredictRoute:
    FIELDS = {
        "conversation_id", "model", "est_input_tokens", "est_cached_tokens",
        "est_output_tokens", "est_cost_usd", "method",
    }

    def test_returns_prediction_fields(self, predict_app):
        client = TestClient(predict_app)
        resp = client.post(
            "/api/usage/predict",
            json={"message": "How much will this turn cost me roughly?"},
            headers=_auth(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == self.FIELDS
        assert body["conversation_id"] is None
        assert body["method"] == "heuristic"
        assert body["est_input_tokens"] > 0
        assert body["est_cached_tokens"] == 0
        assert body["est_output_tokens"] == 2048
        assert isinstance(body["est_cost_usd"], float)

    def test_conversation_history_increases_estimate(self, predict_app,
                                                     in_memory_db):
        in_memory_db.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) "
            "VALUES ('c-pred', 'test', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')",
        )
        for i, (role, content) in enumerate([
            ("user", "x" * 400), ("assistant", "y" * 400),
        ]):
            in_memory_db.execute(
                "INSERT INTO messages (id, conversation_id, role, content, "
                "created_at) VALUES (?, 'c-pred', ?, ?, ?)",
                (f"m-{i}", role, content, f"2026-01-01T00:00:0{i}+00:00"),
            )
        in_memory_db.commit()

        client = TestClient(predict_app)
        bare = client.post(
            "/api/usage/predict", json={"message": "hello"}, headers=_auth(),
        ).json()
        with_history = client.post(
            "/api/usage/predict",
            json={"conversation_id": "c-pred", "message": "hello"},
            headers=_auth(),
        ).json()
        # 800 extra chars of history ≈ 200 extra heuristic tokens.
        assert with_history["est_input_tokens"] == bare["est_input_tokens"] + 200
        assert with_history["conversation_id"] == "c-pred"

    def test_rejects_without_bearer_auth(self, predict_app):
        client = TestClient(predict_app)
        resp = client.post("/api/usage/predict", json={"message": "hi"})
        assert resp.status_code == 401

    def test_message_is_required(self, predict_app):
        client = TestClient(predict_app)
        resp = client.post("/api/usage/predict", json={}, headers=_auth())
        assert resp.status_code == 422
