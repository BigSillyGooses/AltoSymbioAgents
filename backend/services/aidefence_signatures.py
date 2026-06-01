"""
services/aidefence_signatures.py — deterministic prompt-injection signatures.

A standalone, dependency-free detector that augments the LlamaFirewall-based
scanner in ``input_sanitizer``. It runs even when LlamaFirewall is not
installed, so a baseline of injection/jailbreak defence is always available.

Two layers:
  * ``SIGNATURES`` — a curated pack of regex signatures for the most common
    prompt-injection / jailbreak / exfiltration patterns. Each carries a
    weight in [0,1] and a ``critical`` flag (critical matches force a block).
  * ``jailbreak_score`` — a lightweight heuristic that rewards stacking of
    multiple weaker signals (a hallmark of jailbreak prompts) so that several
    individually-sub-threshold cues still trip a warning.

``scan(text)`` returns ``{matches, score, is_critical, reason}`` where ``score``
is in [0,1] and ``matches`` lists the signature names that fired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Signature:
    name: str
    pattern: re.Pattern
    weight: float
    critical: bool
    description: str


def _sig(name: str, regex: str, weight: float, critical: bool, desc: str) -> Signature:
    return Signature(name, re.compile(regex, re.IGNORECASE), weight, critical, desc)


# 18 signatures across instruction-override, role-hijack, prompt-leak,
# restriction-bypass, exfiltration and code/command injection.
SIGNATURES: list[Signature] = [
    _sig("ignore_previous",
         r"\b(ignore|disregard|forget)\b[^.\n]{0,30}\b(previous|prior|above|earlier|all)\b[^.\n]{0,20}\b(instruction|prompt|rule|message|context)",
         0.9, True, "instruction override (ignore previous instructions)"),
    _sig("forget_everything",
         r"\bforget\b[^.\n]{0,20}\b(everything|all|your)\b[^.\n]{0,20}\b(instruction|rule|training|programming)",
         0.85, True, "instruction wipe"),
    _sig("override_system",
         r"\b(override|overrule|bypass|disable|turn off)\b[^.\n]{0,30}\b(system prompt|safety|security|content|guardrail|filter|restriction|guideline)",
         0.9, True, "safety/system override"),
    _sig("new_instructions",
         r"\b(new|updated|real|actual)\b[^.\n]{0,15}\b(instruction|directive|rule)s?\b\s*[:\-]",
         0.7, False, "injected new-instructions block"),
    _sig("system_role_inject",
         r"(?m)^\s*(system|assistant|developer)\s*[:>]\s",
         0.65, False, "fake system/assistant role turn"),
    _sig("act_as",
         r"\b(act as|pretend (to be|you are|you're)|roleplay as|you are now|from now on,? you are)\b",
         0.55, False, "role hijack / persona override"),
    _sig("dan_mode",
         r"\b(DAN|do anything now|STAN|DUDE|AIM)\b|\bdeveloper mode\b|\bunrestricted mode\b",
         0.85, True, "known jailbreak persona"),
    _sig("jailbreak_word",
         r"\bjailbreak(en|ing|ed)?\b",
         0.6, False, "explicit jailbreak reference"),
    _sig("reveal_system_prompt",
         r"\b(reveal|print|show|repeat|output|display|tell me|what (is|are))\b[^.\n]{0,40}\b(system prompt|initial prompt|your instructions|your rules|your guidelines|the prompt above)",
         0.8, True, "system-prompt exfiltration"),
    _sig("ignore_guidelines",
         r"\bignore\b[^.\n]{0,20}\b(your|the)\b[^.\n]{0,15}\b(guideline|rule|programming|training|policy|alignment)",
         0.8, True, "guideline override"),
    _sig("without_restrictions",
         r"\bwithout\b[^.\n]{0,15}\b(any )?(restriction|filter|limitation|censorship|moral|ethic)",
         0.6, False, "request to drop restrictions"),
    _sig("must_comply",
         r"\byou (must|will|have to|are required to)\b[^.\n]{0,20}\b(comply|obey|not refuse|never refuse|answer (everything|anything))",
         0.6, False, "coercion to comply"),
    _sig("bypass_filter",
         r"\b(bypass|evade|get around|circumvent|sidestep)\b[^.\n]{0,25}\b(safety|filter|guardrail|restriction|moderation|detection)",
         0.85, True, "filter-bypass request"),
    _sig("decode_payload",
         r"\b(decode|base64 ?decode|rot13|from the following (base64|hex|encoded))\b[^.\n]{0,25}\b(and (then )?(execute|run|follow|obey))?",
         0.6, False, "obfuscated-payload smuggling"),
    _sig("exfiltrate",
         r"\b(send|post|upload|exfiltrate|leak|transmit)\b[^.\n]{0,40}\b(to )?(https?://|api[_ ]?key|secret|token|credential|password|env)",
         0.85, True, "data exfiltration"),
    _sig("sql_injection",
         r"(?i)\b(drop|delete|truncate)\s+table\b|\bunion\s+select\b|;--",
         0.7, False, "SQL injection payload"),
    _sig("command_injection",
         r"\brm\s+-rf\b|\b(curl|wget)\b[^\n]{0,40}\|\s*(sh|bash)\b|`[^`]+`|\$\([^)]+\)",
         0.7, False, "shell command injection"),
    _sig("script_injection",
         r"<\s*script\b|javascript:\s|onerror\s*=|\{\{[^}]+\}\}|\$\{[^}]+\}",
         0.55, False, "script/template injection"),
]


def jailbreak_score(text: str, fired_weights: list[float]) -> float:
    """Reward stacking of multiple sub-threshold signals.

    Two or more distinct cues together are far more likely to be a jailbreak
    attempt than any one alone, so the combined score is boosted above the max
    individual weight when several signatures fire.
    """
    if len(fired_weights) < 2:
        return max(fired_weights) if fired_weights else 0.0
    top2 = sorted(fired_weights, reverse=True)[:2]
    # Soft-OR of the two strongest cues.
    combined = top2[0] + (1.0 - top2[0]) * top2[1]
    return round(min(1.0, combined), 3)


def scan(text: str) -> dict:
    """Return ``{matches, score, is_critical, reason}`` for ``text``."""
    if not text or not text.strip():
        return {"matches": [], "score": 0.0, "is_critical": False, "reason": ""}

    matches: list[str] = []
    weights: list[float] = []
    is_critical = False
    critical_reason = ""
    top_reason = ""
    top_weight = 0.0

    for sig in SIGNATURES:
        if sig.pattern.search(text):
            matches.append(sig.name)
            weights.append(sig.weight)
            if sig.critical:
                is_critical = True
                if sig.weight >= top_weight:
                    critical_reason = sig.description
            if sig.weight > top_weight:
                top_weight = sig.weight
                top_reason = sig.description

    if not matches:
        return {"matches": [], "score": 0.0, "is_critical": False, "reason": ""}

    score = max(top_weight, jailbreak_score(text, weights))
    reason = critical_reason or top_reason
    if len(matches) > 1:
        reason = f"{reason} (+{len(matches) - 1} more signal(s))"
    return {
        "matches": matches,
        "score": round(score, 3),
        "is_critical": is_critical,
        "reason": f"AIDefence: {reason}",
    }
