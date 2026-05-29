"""
tests/test_escalation_ladder_margin_proxy.py — QLPT Stage 1 wiring.

Pins the EscalationLadder ↔ margin_proxy ↔ self-score routing matrix
plus the shadow-mode (escalation_log_margin_proxy_scores) audit record.

Coverage:
  Routing (6 cases):
    1. proxy on  + logprobs present + below threshold  → proxy drives, escalates
    2. proxy on  + logprobs None                       → self-score drives
    3. proxy on  + logprobs empty tuple                → self-score drives
    4. proxy off + logprobs present                    → self-score drives
    5. proxy off + logprobs None                       → self-score drives
    6. proxy on  + logprobs present + above threshold  → no escalation
  Shadow mode (3 cases):
    7. shadow on  + proxy on  + below threshold        → both run, audit, proxy drives
    8. shadow on  + proxy off + self-score below thresh→ both run, audit, self drives
    9. shadow off                                       → only one path runs, no audit

Style mirrors test_escalation_ladder.py: pytest, frozen ``_Target`` dataclass,
``_decision`` / ``_ctx`` / ``_local_available`` helpers, inline mocks via
unittest.mock. QUALITY_ESCALATION_THRESHOLD imported from the module under
test rather than hardcoded.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from models import RoutingDecision
from services.escalation_ladder import (
    EscalationLadder,
    QUALITY_ESCALATION_THRESHOLD,
)
from services.turn_context import TurnContext


# ── Local fixtures (match test_escalation_ladder.py style) ───────────────────


@dataclass(frozen=True)
class _Target:
    backend: str = "local"
    max_tokens: int = 1024


def _decision(agent_id: str = "a", backend: str = "local") -> RoutingDecision:
    return RoutingDecision(
        agent_id=agent_id, backend=backend, score=1.0,
        reasoning="initial", used_fallback=False, skill_matched="",
    )


def _ctx(
    user_message: str = "tell me about elephants and their habits",
    worker_logprobs=None,
    conversation_id: str = "c1",
    turn_id: str = "t1",
) -> TurnContext:
    ctx = TurnContext(conversation_id=conversation_id, user_message=user_message)
    ctx.worker_logprobs = worker_logprobs
    ctx.turn_id = turn_id
    return ctx


def _local_available():
    local = MagicMock()
    local.is_available.return_value = True
    return local


def _settings(**overrides) -> MagicMock:
    """A MagicMock that walks like core.settings.Settings.

    Defaults match the SETTINGS_DEFAULTS values for the three QLPT keys
    (all off / None) so tests opt in by passing kwargs.
    """
    base = {
        "escalation_use_margin_proxy":        False,
        "escalation_margin_proxy_params":     None,
        "escalation_log_margin_proxy_scores": False,
    }
    base.update(overrides)
    s = MagicMock()
    s.get.side_effect = lambda key, default=None: base.get(key, default)
    return s


def _logprobs(n: int = 30, seed: int = 0) -> tuple[float, ...]:
    """Synthetic confident logprobs; match test_margin_proxy.py style."""
    rng = random.Random(seed)
    return tuple(rng.gauss(-0.5, 0.2) for _ in range(n))


def _hub_with_rescue() -> MagicMock:
    """A hub_router whose .invoke returns a stub EscalationLadder._escalate
    can read (text + token counts + model_name)."""
    hub = MagicMock()
    hub.invoke.return_value = MagicMock(
        text="claude rescue answer", input_tokens=10,
        output_tokens=20, model_name="claude-sonnet-4",
    )
    return hub


# ── Routing matrix (6 cases) ─────────────────────────────────────────────────


def test_proxy_on_logprobs_present_below_threshold_drives_escalation():
    """Case 1: proxy enabled + logprobs + below threshold → proxy path fires."""
    hub = _hub_with_rescue()
    settings = _settings(escalation_use_margin_proxy=True)
    ladder = EscalationLadder(hub, _local_available(), settings)
    lps = _logprobs(seed=1)

    target_score = QUALITY_ESCALATION_THRESHOLD - 1.0
    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
            return_value=target_score,
        ) as proxy,
        patch(
            "services.task_artifacts.local_first_call",
            return_value='{"score": 9, "reason": "should not be called"}',
        ) as self_call,
    ):
        out = ladder.maybe_escalate(
            ctx=_ctx(worker_logprobs=lps), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text="local answer that is plausibly long enough",
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )

    proxy.assert_called_once()
    # First positional arg is the (list-converted) logprob sequence.
    passed_logprobs = proxy.call_args.args[0]
    assert list(passed_logprobs) == list(lps)
    self_call.assert_not_called()
    assert out.escalated is True
    assert "(margin proxy)" in out.escalation_reason


def test_proxy_on_logprobs_none_falls_back_to_self_score():
    """Case 2: proxy enabled but no logprobs → legacy self-score path."""
    hub = _hub_with_rescue()
    settings = _settings(escalation_use_margin_proxy=True)
    ladder = EscalationLadder(hub, _local_available(), settings)

    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
        ) as proxy,
        patch(
            "services.task_artifacts.local_first_call",
            return_value='{"score": 2, "reason": "off-topic"}',
        ) as self_call,
    ):
        out = ladder.maybe_escalate(
            ctx=_ctx(worker_logprobs=None), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text="local answer that is plausibly long enough",
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )

    proxy.assert_not_called()
    self_call.assert_called_once()
    assert out.escalated is True
    assert "(margin proxy)" not in out.escalation_reason


def test_proxy_on_logprobs_empty_tuple_falls_back_to_self_score():
    """Case 3: proxy enabled but empty-tuple logprobs → self-score path."""
    hub = _hub_with_rescue()
    settings = _settings(escalation_use_margin_proxy=True)
    ladder = EscalationLadder(hub, _local_available(), settings)

    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
        ) as proxy,
        patch(
            "services.task_artifacts.local_first_call",
            return_value='{"score": 1, "reason": "weak"}',
        ) as self_call,
    ):
        out = ladder.maybe_escalate(
            ctx=_ctx(worker_logprobs=tuple()), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text="local answer that is plausibly long enough",
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )

    proxy.assert_not_called()
    self_call.assert_called_once()
    assert out.escalated is True
    assert "(margin proxy)" not in out.escalation_reason


def test_proxy_off_logprobs_present_uses_self_score():
    """Case 4: flag off → proxy never called even when logprobs available."""
    hub = _hub_with_rescue()
    settings = _settings(escalation_use_margin_proxy=False)
    ladder = EscalationLadder(hub, _local_available(), settings)
    lps = _logprobs(seed=4)

    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
        ) as proxy,
        patch(
            "services.task_artifacts.local_first_call",
            return_value='{"score": 3, "reason": "weak"}',
        ) as self_call,
    ):
        out = ladder.maybe_escalate(
            ctx=_ctx(worker_logprobs=lps), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text="local answer that is plausibly long enough",
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )

    proxy.assert_not_called()
    self_call.assert_called_once()
    assert out.escalated is True
    assert "(margin proxy)" not in out.escalation_reason


def test_proxy_off_logprobs_none_uses_self_score():
    """Case 5: flag off + no logprobs → unchanged legacy behavior."""
    hub = _hub_with_rescue()
    settings = _settings(escalation_use_margin_proxy=False)
    ladder = EscalationLadder(hub, _local_available(), settings)

    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
        ) as proxy,
        patch(
            "services.task_artifacts.local_first_call",
            return_value='{"score": 2, "reason": "weak"}',
        ) as self_call,
    ):
        out = ladder.maybe_escalate(
            ctx=_ctx(worker_logprobs=None), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text="local answer that is plausibly long enough",
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )

    proxy.assert_not_called()
    self_call.assert_called_once()
    assert out.escalated is True
    assert "(margin proxy)" not in out.escalation_reason


def test_proxy_on_above_threshold_does_not_escalate():
    """Case 6: proxy returns above threshold → no escalation, response intact."""
    hub = _hub_with_rescue()
    settings = _settings(escalation_use_margin_proxy=True)
    ladder = EscalationLadder(hub, _local_available(), settings)
    lps = _logprobs(seed=6)
    original_text = "solid local answer that meets the bar"

    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
            return_value=QUALITY_ESCALATION_THRESHOLD + 3.0,
        ),
        patch(
            "services.task_artifacts.local_first_call",
        ) as self_call,
    ):
        out = ladder.maybe_escalate(
            ctx=_ctx(worker_logprobs=lps), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text=original_text,
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )

    self_call.assert_not_called()
    hub.invoke.assert_not_called()
    assert out.escalated is False
    assert out.response_text == original_text
    assert out.escalation_reason == ""


# ── Shadow mode (3 cases) ────────────────────────────────────────────────────


def test_shadow_proxy_on_below_threshold_audits_both_paths():
    """Case 7: shadow + proxy on + proxy below → audit row, proxy drives."""
    hub = _hub_with_rescue()
    settings = _settings(
        escalation_use_margin_proxy=True,
        escalation_log_margin_proxy_scores=True,
    )
    audit = MagicMock()
    ladder = EscalationLadder(
        hub, _local_available(), settings, audit_log=audit,
    )
    lps = _logprobs(seed=7)
    proxy_score = QUALITY_ESCALATION_THRESHOLD - 1.5
    self_payload = '{"score": 9, "reason": "looks fine"}'

    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
            return_value=proxy_score,
        ) as proxy,
        patch(
            "services.task_artifacts.local_first_call",
            return_value=self_payload,
        ) as self_call,
    ):
        out = ladder.maybe_escalate(
            ctx=_ctx(worker_logprobs=lps), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text="local answer that is plausibly long enough",
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )

    # Both LLM-side scorers were forced to run for paired data collection.
    proxy.assert_called_once()
    self_call.assert_called_once()

    # Exactly one shadow record was written for this turn.
    audit.append.assert_called_once()
    event_name, kwargs = audit.append.call_args.args[0], audit.append.call_args.kwargs
    assert event_name == "margin_proxy_shadow"
    assert kwargs["proxy_score"] == pytest.approx(proxy_score)
    assert kwargs["self_score"] == pytest.approx(9.0)
    assert kwargs["proxy_threshold_crossed"] is True
    assert kwargs["self_threshold_crossed"] is False
    assert kwargs["which_path_drove_escalation"] == "margin_proxy"
    assert kwargs["conversation_id"] == "c1"
    assert kwargs["turn_id"] == "t1"

    assert out.escalated is True
    assert "(margin proxy)" in out.escalation_reason


def test_shadow_proxy_off_self_below_threshold_audits_both_paths():
    """Case 8: shadow + proxy off + self below → audit row, self-score drives."""
    hub = _hub_with_rescue()
    settings = _settings(
        escalation_use_margin_proxy=False,
        escalation_log_margin_proxy_scores=True,
    )
    audit = MagicMock()
    ladder = EscalationLadder(
        hub, _local_available(), settings, audit_log=audit,
    )
    lps = _logprobs(seed=8)
    proxy_score = QUALITY_ESCALATION_THRESHOLD + 4.0  # well above
    self_payload = '{"score": 1, "reason": "off topic"}'

    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
            return_value=proxy_score,
        ) as proxy,
        patch(
            "services.task_artifacts.local_first_call",
            return_value=self_payload,
        ) as self_call,
    ):
        out = ladder.maybe_escalate(
            ctx=_ctx(worker_logprobs=lps), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text="local answer that is plausibly long enough",
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )

    proxy.assert_called_once()
    self_call.assert_called_once()

    audit.append.assert_called_once()
    event_name = audit.append.call_args.args[0]
    kwargs = audit.append.call_args.kwargs
    assert event_name == "margin_proxy_shadow"
    assert kwargs["proxy_score"] == pytest.approx(proxy_score)
    assert kwargs["self_score"] == pytest.approx(1.0)
    assert kwargs["proxy_threshold_crossed"] is False
    assert kwargs["self_threshold_crossed"] is True
    assert kwargs["which_path_drove_escalation"] == "self_score"

    assert out.escalated is True
    assert "(margin proxy)" not in out.escalation_reason


def test_shadow_off_runs_only_one_path_and_writes_no_audit():
    """Case 9: shadow off → exactly one path runs, no audit record."""
    hub = _hub_with_rescue()
    audit = MagicMock()

    # Sub-case 9a: proxy ON + shadow OFF. Only proxy runs.
    settings_proxy = _settings(
        escalation_use_margin_proxy=True,
        escalation_log_margin_proxy_scores=False,
    )
    ladder_proxy = EscalationLadder(
        hub, _local_available(), settings_proxy, audit_log=audit,
    )
    lps = _logprobs(seed=9)
    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
            return_value=QUALITY_ESCALATION_THRESHOLD - 1.0,
        ) as proxy,
        patch(
            "services.task_artifacts.local_first_call",
            return_value='{"score": 9}',
        ) as self_call,
    ):
        ladder_proxy.maybe_escalate(
            ctx=_ctx(worker_logprobs=lps), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text="local answer that is plausibly long enough",
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )
    assert proxy.call_count == 1
    assert self_call.call_count == 0
    assert audit.append.call_count == 0

    # Sub-case 9b: proxy OFF + shadow OFF. Only self-score runs.
    audit.reset_mock()
    settings_legacy = _settings(
        escalation_use_margin_proxy=False,
        escalation_log_margin_proxy_scores=False,
    )
    ladder_legacy = EscalationLadder(
        hub, _local_available(), settings_legacy, audit_log=audit,
    )
    with (
        patch(
            "services.escalation_ladder.margin_proxy.score_from_logprobs",
        ) as proxy,
        patch(
            "services.task_artifacts.local_first_call",
            return_value='{"score": 9}',
        ) as self_call,
    ):
        ladder_legacy.maybe_escalate(
            ctx=_ctx(worker_logprobs=lps), decision=_decision(), target=_Target(),
            full_system="S", messages=[],
            response_text="local answer that is plausibly long enough",
            tokens_in=0, tokens_out=0, route_model="local", model_name="m",
            had_error=False, split_enabled=False,
        )
    assert proxy.call_count == 0
    assert self_call.call_count == 1
    assert audit.append.call_count == 0
