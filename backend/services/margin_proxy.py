"""
services/margin_proxy.py — Pure logprob-based quality scorer.

QLPT (Quantization-Loss Predictor) Stage 1. Maps a list of per-token
logprobs from a local generation onto a 0..10 quality score on the same
scale as the existing self-score gate, so EscalationLadder can swap
implementations behind a feature flag without changing its threshold.

Thesis: quantized models stay confident (high mean logprob) when they
answer well and collapse into uncertainty (low mean, many low-logprob
tokens) when they degrade. Stage 1 uses logprobs only. Attention entropy
and hidden-state norms are out of scope here.

The module exposes:

  - ``score_from_logprobs(logprobs, params_override=None) -> float | None``
        Pure function. Returns None for empty input or any non-numeric
        entry; otherwise a float in [0, 10].

  - ``params() -> dict[str, float]``
        Read-only snapshot of ``SCORING_PARAMS`` for tests and observability.

  - ``SCORING_PARAMS``
        Module-level dict of tunable constants. Callers may pass a
        ``params_override`` dict on each call (e.g. from a settings key)
        to avoid mutating the module-level defaults.
"""

from numbers import Real


# Tunable constants. Initial values match the QLPT Stage 1 spec; the
# escalation ladder may pass a ``params_override`` dict (sourced from
# ``settings.escalation_margin_proxy_params``) to tune them per install
# without editing this file. No hardcoded numbers live inside the score
# function body — every magic number is named here.
SCORING_PARAMS: dict[str, float] = {
    "clamp_low":           -5.0,   # mean_logprob lower bound for [0,10] mapping
    "threshold_uncertain": -3.0,   # token-level cutoff for "uncertain"
    "penalty_weight":      0.5,    # scales the uncertain-fraction penalty
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _resolved(params_override: dict | None) -> dict[str, float]:
    """Merge ``params_override`` over ``SCORING_PARAMS``.

    Unknown keys are silently dropped (matches ``core/settings._coerce``
    lenience). Override values are coerced to float; non-coercible
    entries fall back to the default for that key.
    """
    if not params_override:
        return dict(SCORING_PARAMS)
    merged = dict(SCORING_PARAMS)
    for key, raw in params_override.items():
        if key not in SCORING_PARAMS:
            continue
        try:
            merged[key] = float(raw)
        except (TypeError, ValueError):
            continue
    return merged


def score_from_logprobs(
    logprobs,
    params_override: dict | None = None,
) -> float | None:
    """Map per-token logprobs to a 0..10 quality score.

    Returns None when ``logprobs`` is empty, not iterable, or contains a
    non-numeric entry. Otherwise returns a float in [0, 10].

    Formula:
        mean_lp      = mean(logprobs)
        fraction_low = count(lp < threshold_uncertain) / len(logprobs)
        base         = clamp((mean_lp - clamp_low) / (0 - clamp_low) * 10, 0, 10)
        penalty      = penalty_weight * fraction_low * 10
        score        = clamp(base - penalty, 0, 10)
    """
    if logprobs is None:
        return None
    try:
        n = len(logprobs)
    except TypeError:
        return None
    if n == 0:
        return None

    nums: list[float] = []
    for lp in logprobs:
        # ``bool`` is a subclass of ``int``; reject it explicitly so a
        # caller passing ``[True, False]`` does not get silently scored.
        if isinstance(lp, bool) or not isinstance(lp, Real):
            return None
        nums.append(float(lp))

    p = _resolved(params_override)
    clamp_low = p["clamp_low"]
    threshold = p["threshold_uncertain"]
    weight = p["penalty_weight"]

    mean_lp = sum(nums) / len(nums)
    fraction_low = sum(1 for x in nums if x < threshold) / len(nums)

    # Guard against a pathological clamp_low of 0 (or positive): the
    # default mapping would divide by zero. Degenerate but well-defined
    # behavior: any non-positive mean lands at 0, any positive lands at 10.
    if clamp_low >= 0:
        base = 10.0 if mean_lp >= 0 else 0.0
    else:
        base = _clamp((mean_lp - clamp_low) / (0.0 - clamp_low) * 10.0, 0.0, 10.0)

    penalty = weight * fraction_low * 10.0
    return _clamp(base - penalty, 0.0, 10.0)


def params() -> dict[str, float]:
    """Return a read-only snapshot of ``SCORING_PARAMS``.

    Useful for tests that pin the active constants without importing the
    dict directly, and for observability code that wants to log the
    parameter set behind a margin-proxy score.
    """
    return dict(SCORING_PARAMS)
