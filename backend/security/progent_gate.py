"""
security/progent_gate.py — deterministic, per-argument tool-call policy gate.

This is a CLEAN-ROOM implementation of the privilege-control idea described in
the Progent paper ("Progent: Programmable Privilege Control for LLM Agents",
arXiv:2504.11703). It contains NO code from the Progent repository
(github.com/sunblaze-ucb/progent): that project ships WITHOUT a license and so
cannot be vendored. Only the published *concept* is reused — a deterministic,
priority-ordered policy that allows/denies a tool call based on the tool name
AND constraints over its arguments, returning a fixed fallback message on deny.

It COMPLEMENTS the existing GovernanceEngine (services/governance.py), which
gates on tool *name* + call budgets + the Reader/Actor split. This gate adds
the argument-level dimension governance does not check (governance's
``forbidden_patterns`` field is declared but unused).

Design:
  - One per-agent policy file, JSON, named ``{agent_id}.json``, looked up under
    a user-writable dir first (``<user_data>/policies/``) then the shipped
    defaults (``backend/core/config/policies/``).
  - NO policy file for an agent  -> FAIL-OPEN (allow all) + a one-time warning,
    so existing agents are unaffected. Set ``PROGENT_FAIL_CLOSED = True`` to
    flip to deny-by-default later.
  - Fixed policy only: no dynamic / LLM-generated policy updates in this cut
    (the paper's "Disable Update" mode). Monotonic confinement is therefore
    trivially preserved (policies never change at runtime).
  - Pure standard library (``re``, ``json``) — no third-party dependencies.

Policy format (see ``core/config/policies/_example.json``)::

    {
      "version": 1,
      "default": "allow",            # effect when no rule matches: allow | deny
      "rules": [
        { "name": "reads-in-workspace",
          "tool": "read_file",
          "args": { "path": { "prefix": "/workspace/" } },
          "effect": "allow",
          "fallback": "read_file is limited to /workspace/." },
        { "tool": "send_email", "effect": "deny",
          "fallback": "Email is disabled for this agent." }
      ]
    }

Rule semantics: rules are evaluated in order; the FIRST rule whose ``tool``
matches (exact name or ``"*"``) AND whose argument constraints are all
satisfied decides the outcome via its ``effect``. If no rule applies,
``default`` decides.

Supported per-argument constraints (a value must satisfy ALL predicates listed
for it): ``const``, ``enum``, ``type``, ``pattern`` (regex search), ``prefix``,
``suffix``, ``contains``, ``not_contains``, ``min``, ``max``, ``min_length``,
``max_length``. A bare (non-object) constraint means exact equality.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("altosybioagents.security.progent_gate")

# Global kill-switch for the no-policy behaviour. Default fail-OPEN so adding
# this gate cannot change the behaviour of any existing agent. Flip to True for
# deny-by-default once policies exist for every agent that should run.
PROGENT_FAIL_CLOSED = False

_ALLOW = "allow"
_DENY = "deny"


# ── result type ─────────────────────────────────────────────────────────────
@dataclass
class GateResult:
    """ALLOW, or BLOCK carrying the fallback message to feed back to the agent."""

    allowed: bool
    fallback_message: str = ""
    rule: str = ""  # label of the deciding rule/policy, for logging

    @classmethod
    def allow(cls, rule: str = "") -> "GateResult":
        return cls(True, "", rule)

    @classmethod
    def block(cls, fallback_message: str, rule: str = "") -> "GateResult":
        return cls(False, fallback_message or "This action is blocked by policy.", rule)


# ── constraint evaluation ───────────────────────────────────────────────────
def _is_type(value: Any, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "array":
        return isinstance(value, (list, tuple))
    if t == "object":
        return isinstance(value, dict)
    if t == "null":
        return value is None
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _arg_satisfies(value: Any, constraint: Any) -> bool:
    """True iff a single argument ``value`` meets ALL predicates in ``constraint``.

    A bare (non-dict) constraint is treated as an exact-equality ``const``.
    Any type mismatch (e.g. ``prefix`` on a non-string) makes the predicate fail
    rather than raising, so a malformed call simply does not match the rule.
    """
    if not isinstance(constraint, dict):
        return value == constraint

    for key, expected in constraint.items():
        try:
            if key == "const":
                if value != expected:
                    return False
            elif key == "enum":
                if value not in expected:
                    return False
            elif key == "type":
                if not _is_type(value, expected):
                    return False
            elif key == "pattern":
                if not isinstance(value, str) or re.search(expected, value) is None:
                    return False
            elif key == "prefix":
                if not isinstance(value, str) or not value.startswith(expected):
                    return False
            elif key == "suffix":
                if not isinstance(value, str) or not value.endswith(expected):
                    return False
            elif key == "contains":
                if expected not in value:
                    return False
            elif key == "not_contains":
                if expected in value:
                    return False
            elif key == "min":
                if not _is_number(value) or value < expected:
                    return False
            elif key == "max":
                if not _is_number(value) or value > expected:
                    return False
            elif key == "min_length":
                if len(value) < expected:
                    return False
            elif key == "max_length":
                if len(value) > expected:
                    return False
            else:
                # Unknown predicate -> be conservative: the rule does not match.
                log.debug("progent_gate: unknown arg predicate %r; rule will not match", key)
                return False
        except TypeError:
            # e.g. len()/`in` on an unsized value, or a cross-type comparison.
            return False
    return True


def _rule_applies(rule: dict, tool_name: str, args: dict) -> bool:
    tool = rule.get("tool", "*")
    if tool != "*" and tool != tool_name:
        return False
    constraints = rule.get("args") or {}
    if not isinstance(constraints, dict):
        return False
    for arg_name, constraint in constraints.items():
        if arg_name not in args:
            # The rule constrains an argument the call did not supply, so it
            # does not apply (an allow-rule won't grant; a deny-rule won't fire).
            return False
        if not _arg_satisfies(args[arg_name], constraint):
            return False
    return True


# ── policy ──────────────────────────────────────────────────────────────────
@dataclass
class _Policy:
    default: str = _ALLOW
    rules: list = field(default_factory=list)

    def evaluate(self, tool_name: str, args: dict) -> GateResult:
        for i, rule in enumerate(self.rules):
            if _rule_applies(rule, tool_name, args):
                effect = str(rule.get("effect", _ALLOW)).lower()
                label = rule.get("name") or f"{rule.get('tool', '*')}#{i}"
                if effect == _DENY:
                    return GateResult.block(rule.get("fallback", ""), label)
                return GateResult.allow(label)
        if self.default == _DENY:
            return GateResult.block("No policy rule permits this action.", "default-deny")
        return GateResult.allow("default-allow")


def _parse_policy_file(path: Path) -> "_Policy | None":
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("progent_gate: failed to read policy %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.error("progent_gate: policy %s is not a JSON object", path)
        return None
    default = str(data.get("default", _ALLOW)).lower()
    if default not in (_ALLOW, _DENY):
        default = _ALLOW
    rules = data.get("rules") or []
    if not isinstance(rules, list):
        rules = []
    return _Policy(default=default, rules=[r for r in rules if isinstance(r, dict)])


def _default_policy_dirs() -> list[Path]:
    dirs: list[Path] = []
    # 1) user-writable overrides (best-effort; core.paths may pull settings).
    try:
        from core import paths as _paths  # type: ignore

        dirs.append(Path(_paths.user_dir()) / "policies")
    except Exception:
        pass
    # 2) shipped defaults: backend/core/config/policies/
    dirs.append(Path(__file__).resolve().parents[1] / "core" / "config" / "policies")
    return dirs


# ── the gate ────────────────────────────────────────────────────────────────
class ToolCallPolicyGate:
    """Deterministic per-agent, per-argument tool-call policy gate.

    One instance per process (see the module-level ``gate`` singleton). Parsed
    policies are cached by ``agent_id``; agents with no policy are re-checked on
    each call (cheap stat) so dropping a new policy file activates it without a
    restart. Call :meth:`reload` to drop the cache after editing a policy.
    """

    def __init__(self, policies_dirs: "list[Path] | None" = None):
        self._dirs = policies_dirs if policies_dirs is not None else _default_policy_dirs()
        self._cache: dict[str, _Policy] = {}
        self._warned: set[str] = set()

    def check(self, agent_id: str, tool_name: str, args: "dict | None") -> GateResult:
        """Return ALLOW or BLOCK(fallback_message) for one tool call.

        No policy for ``agent_id`` -> fail-open ALLOW (+ one-time warning) unless
        ``PROGENT_FAIL_CLOSED`` is set. Never raises: an internal error fails
        open (or closed under the flag) so the gate cannot break a turn.
        """
        policy = self._load(agent_id)
        if policy is None:
            if PROGENT_FAIL_CLOSED:
                return GateResult.block(
                    f"No tool-call policy is defined for agent '{agent_id}'.",
                    "fail-closed",
                )
            if agent_id not in self._warned:
                self._warned.add(agent_id)
                log.warning(
                    "progent_gate: no policy for agent %r — allowing all tool calls (fail-open)",
                    agent_id,
                )
            return GateResult.allow("fail-open")
        try:
            return policy.evaluate(tool_name, dict(args or {}))
        except Exception as exc:  # defensive: never let the gate break a turn
            log.error(
                "progent_gate: policy evaluation error (agent=%r tool=%r): %s",
                agent_id, tool_name, exc,
            )
            if PROGENT_FAIL_CLOSED:
                return GateResult.block("Policy evaluation failed.", "error")
            return GateResult.allow("error-fail-open")

    def _load(self, agent_id: str) -> "_Policy | None":
        cached = self._cache.get(agent_id)
        if cached is not None:
            return cached
        for d in self._dirs:
            p = d / f"{agent_id}.json"
            try:
                is_file = p.is_file()
            except OSError:
                is_file = False
            if is_file:
                pol = _parse_policy_file(p)
                if pol is not None:
                    self._cache[agent_id] = pol
                return pol
        return None

    def reload(self) -> None:
        """Drop cached policies (and the no-policy warnings) so edits take effect."""
        self._cache.clear()
        self._warned.clear()


def to_args_dict(args: "list | tuple | None", kwargs: "dict | None") -> dict:
    """Map a CaMeL call's positional ``args`` + ``kwargs`` to a name->value dict.

    Keyword arguments are used by name. Positional arguments carry no parameter
    name at the interpreter layer, so they are exposed under ``"$0"``, ``"$1"``,
    … allowing a policy to reference them by index if it must. In practice
    CaMeL plans call tools with keyword arguments, so name-based constraints are
    the common path.
    """
    merged: dict = dict(kwargs or {})
    for i, v in enumerate(args or []):
        merged.setdefault(f"${i}", v)
    return merged


# Module-level singleton used by the dispatch choke point (services/camel/adapter.py).
gate = ToolCallPolicyGate()
