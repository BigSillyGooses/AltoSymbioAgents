"""
tests/test_margin_proxy.py — Pure scorer for QLPT Stage 1.

Pins the score_from_logprobs contract:
  - Empty / non-numeric input → None
  - Confident generations → high score (≥ 7)
  - Collapsed generations → low score (≤ 3)
  - Score is always clamped to [0, 10]
  - SCORING_PARAMS override via params_override actually changes output
  - params() returns a snapshot that can't mutate module state

Inputs are generated from controlled distributions seeded for
determinism — no hardcoded arrays of made-up numbers.
"""

from __future__ import annotations

import random

import pytest

from services import margin_proxy


# ── Helpers ────────────────────────────────────────────────────────────────────


def _confident(n: int = 50, seed: int = 0) -> list[float]:
    """A confident generation: tight Gaussian around -0.3, no low-tail."""
    rng = random.Random(seed)
    return [rng.gauss(-0.3, 0.1) for _ in range(n)]


def _collapsed(n: int = 50, seed: int = 1) -> list[float]:
    """A collapsed generation: 40% near -1.0, 60% near -4.5.

    Pushes mean_logprob below -3 and drives fraction_low above the
    threshold so the penalty term dominates the base term.
    """
    rng = random.Random(seed)
    out: list[float] = []
    for _ in range(n):
        if rng.random() < 0.4:
            out.append(rng.gauss(-1.0, 0.2))
        else:
            out.append(rng.gauss(-4.5, 0.3))
    return out


# ── Empty / malformed input ────────────────────────────────────────────────────


def test_empty_logprobs_returns_none():
    assert margin_proxy.score_from_logprobs([]) is None


def test_none_input_returns_none():
    assert margin_proxy.score_from_logprobs(None) is None


def test_non_numeric_entry_returns_none():
    rng = random.Random(7)
    arr: list = [rng.gauss(-0.5, 0.1) for _ in range(5)]
    arr.insert(2, "oops")  # non-numeric token logprob entry
    assert margin_proxy.score_from_logprobs(arr) is None


def test_bool_entry_rejected_even_though_bool_is_int():
    # ``True`` is an instance of ``int``; the scorer must still reject
    # it so a caller accidentally passing booleans doesn't get a silent
    # score of 10 (True coerces to 1.0).
    assert margin_proxy.score_from_logprobs([True, False, True]) is None


def test_non_iterable_input_returns_none():
    assert margin_proxy.score_from_logprobs(42) is None


# ── Confident / collapsed bands ────────────────────────────────────────────────


def test_confident_response_scores_high():
    score = margin_proxy.score_from_logprobs(_confident())
    assert score is not None
    assert score >= 7.0, f"expected >=7 for a confident generation, got {score}"


def test_collapsed_response_scores_low():
    score = margin_proxy.score_from_logprobs(_collapsed())
    assert score is not None
    assert score <= 3.0, f"expected <=3 for a collapsed generation, got {score}"


# ── Degenerate distributions ───────────────────────────────────────────────────


def test_single_token_returns_valid_score():
    score = margin_proxy.score_from_logprobs([-0.5])
    assert score is not None
    assert isinstance(score, float)
    assert 0.0 <= score <= 10.0


def test_all_identical_logprobs():
    # Zero variance: the formula must still produce a finite, in-range
    # score and not divide by zero or hit a NaN somewhere.
    score = margin_proxy.score_from_logprobs([-1.0] * 20)
    assert score is not None
    assert 0.0 <= score <= 10.0


def test_all_zero_logprobs_scores_at_ceiling():
    score = margin_proxy.score_from_logprobs([0.0] * 10)
    assert score is not None
    # mean = 0, fraction_low = 0 → base = 10, penalty = 0 → 10.
    assert score == pytest.approx(10.0)


def test_all_very_negative_logprobs_scores_at_floor():
    # Every token below ``clamp_low`` AND below ``threshold_uncertain``
    # → base clamps to 0, penalty maxes at penalty_weight * 10.
    score = margin_proxy.score_from_logprobs([-10.0] * 10)
    assert score is not None
    assert score == pytest.approx(0.0)


# ── Range invariant ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "logprobs",
    [
        _confident(seed=2),
        _collapsed(seed=3),
        [0.0] * 10,
        [-10.0] * 10,
        [-0.5],
        [-1.0] * 20,
        # Mixed: a noisy run that straddles the threshold.
        [random.Random(11).gauss(-2.0, 1.5) for _ in range(30)],
    ],
)
def test_score_always_in_range(logprobs):
    score = margin_proxy.score_from_logprobs(logprobs)
    assert score is not None
    assert 0.0 <= score <= 10.0


# ── Interface contract ─────────────────────────────────────────────────────────


def test_returns_float_for_valid_input():
    result = margin_proxy.score_from_logprobs([-0.5, -0.3, -0.8])
    assert isinstance(result, float)


def test_returns_none_for_empty_not_zero():
    # Guard against a future refactor returning 0.0 (which would silently
    # cross the QUALITY_ESCALATION_THRESHOLD = 4.0 floor and fire bogus
    # escalations on empty input).
    assert margin_proxy.score_from_logprobs([]) is None
    assert margin_proxy.score_from_logprobs(None) is None


# ── Settings override ──────────────────────────────────────────────────────────


def test_params_override_changes_score():
    # Same logprobs scored with default and with a looser clamp_low.
    # Loosening clamp_low from -5 to -10 stretches the mapping so the
    # same negative mean produces a higher base score, which must show
    # up in the output.
    arr = [random.Random(13).gauss(-2.5, 0.2) for _ in range(40)]
    default_score = margin_proxy.score_from_logprobs(arr)
    overridden = margin_proxy.score_from_logprobs(
        arr, params_override={"clamp_low": -10.0},
    )
    assert default_score is not None
    assert overridden is not None
    assert overridden != default_score
    assert overridden > default_score, (
        "Loosening clamp_low should raise the base term and therefore "
        f"the final score; got default={default_score} override={overridden}"
    )


def test_params_override_threshold_changes_fraction_low_penalty():
    # Pushing threshold_uncertain to a more negative number reclassifies
    # borderline tokens as "confident", lowering the penalty term.
    arr = [random.Random(17).gauss(-2.0, 0.5) for _ in range(40)]
    strict = margin_proxy.score_from_logprobs(
        arr, params_override={"threshold_uncertain": -1.0},
    )
    lenient = margin_proxy.score_from_logprobs(
        arr, params_override={"threshold_uncertain": -5.0},
    )
    assert strict is not None
    assert lenient is not None
    assert lenient >= strict


def test_params_override_ignores_unknown_keys():
    arr = _confident(seed=21)
    baseline = margin_proxy.score_from_logprobs(arr)
    junk = margin_proxy.score_from_logprobs(arr, params_override={"nope": 99.0})
    assert junk == baseline


def test_params_override_ignores_non_numeric_value():
    arr = _confident(seed=22)
    baseline = margin_proxy.score_from_logprobs(arr)
    bad = margin_proxy.score_from_logprobs(
        arr, params_override={"clamp_low": "not-a-number"},
    )
    assert bad == baseline


# ── params() helper ────────────────────────────────────────────────────────────


def test_params_helper_returns_snapshot():
    snap = margin_proxy.params()
    assert "clamp_low" in snap
    assert "threshold_uncertain" in snap
    assert "penalty_weight" in snap
    # Mutating the returned dict must not leak into the module state.
    snap["clamp_low"] = 999.0
    assert margin_proxy.params()["clamp_low"] != 999.0


def test_params_helper_reflects_current_module_state():
    # Pin the defaults so a silent drift in SCORING_PARAMS gets caught.
    snap = margin_proxy.params()
    assert snap["clamp_low"] == pytest.approx(-5.0)
    assert snap["threshold_uncertain"] == pytest.approx(-3.0)
    assert snap["penalty_weight"] == pytest.approx(0.5)
