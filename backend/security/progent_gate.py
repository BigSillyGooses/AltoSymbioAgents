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
the argument-level dimension governance does not check.

SECURITY POSTURE (deliberate, after an adversarial review):
  - A policy is a deterministic, priority-ordered rule list. Rules are tried in
    order; the first APPLICABLE rule decides; otherwise the policy `default`.
  - DENY rules fail SAFE: a deny fires unless the call is *provably* outside its
    scope. If a constrained argument is missing (e.g. passed positionally, so it
    arrives as "$0" rather than by name), of the wrong type, or otherwise not
    evaluable, the deny still fires. (Consequence: a deny rule keyed on a named
    argument may over-block positional calls to that tool. That is the safe
    direction and is intentional — author rules on keyword arguments.)
  - ALLOW rules grant only when every constraint is PROVABLY satisfied.
  - A STRUCTURALLY INVALID policy (bad JSON, unknown predicate, uncompilable
    regex, invalid `default`/`effect`) fails CLOSED for that agent (blocks all
    tool calls) and logs an error — operators must fix it. This is distinct from
    an agent that simply has NO policy file, which FAILS OPEN (allow + a one-time
    warning) so existing agents are unaffected. Set PROGENT_FAIL_CLOSED=True to
    make the no-policy case deny-by-default too.
  - Fixed policy only: no dynamic / LLM-generated updates in this cut.
  - Pure standard library — no third-party dependencies.

Policy file lookup (first match wins): for agent X, "<dir>/X.json" then
"<dir>/default.json", over the user-writable dir (<user_data>/policies/) then
the shipped defaults (backend/core/config/policies/). Edits and deletions take
effect without a restart (each load re-stats and compares mtime).

Policy format (see core/config/policies/_example.json)::

    {
      "version": 1,
      "default": "allow" | "deny",     # decision when no rule applies
      "rules": [
        { "name": "...", "tool": "read_file" | "*",
          "args": { "<name|a.b.c>": { <predicate>: <value>, ... } },
          "effect": "allow" | "deny",
          "fallback": "message shown when this deny fires" },
        ...
      ]
    }

Supported argument predicates (a value must satisfy ALL listed for it):
  const, enum, type, pattern (regex search), prefix, suffix, contains,
  not_contains, min, max, min_length, max_length, path_under (normalized
  path-containment that rejects ".." traversal). A bare (non-object) constraint
  means exact equality (strict: bool != int). Argument names may be dotted to
  address nested object fields (e.g. "params.url").
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger("altosybioagents.security.progent_gate")

# When an agent has NO policy file: False -> allow (+warn once); True -> deny.
# (A policy that EXISTS but is invalid always fails closed, regardless.)
PROGENT_FAIL_CLOSED = False

_ALLOW = "allow"
_DENY = "deny"

# Tri-state outcome of evaluating a constraint against an argument value.
_MATCH = "match"        # provably satisfied
_NOMATCH = "nomatch"    # provably NOT satisfied
_INDET = "indet"        # cannot be evaluated (missing / wrong type / N/A)

_KNOWN_PREDICATES = frozenset({
    "const", "enum", "type", "pattern", "prefix", "suffix", "contains",
    "not_contains", "min", "max", "min_length", "max_length", "path_under",
})

_VALID_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object", "null"})


class _PolicyError(ValueError):
    """A policy file exists but is structurally invalid -> fail closed."""


class _Corrupt:
    """Sentinel: an existing policy file failed to parse/validate."""


_CORRUPT = _Corrupt()


# ── result type ───────────────────────────────────────────────────────────────
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


# ── predicate helpers ─────────────────────────────────────────────────────────
@lru_cache(maxsize=512)
def _compile(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_type(value: Any, t: str) -> bool:
    if t == "string":
        return isinstance(value, str)
    if t == "number":
        return _is_number(value)
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


def _strict_eq(value: Any, expected: Any) -> bool:
    # bool and int are distinct kinds here: 1 != True, 0 != False.
    if isinstance(value, bool) != isinstance(expected, bool):
        return False
    return value == expected


def _path_under(value: str, root: str) -> bool:
    """True iff `value`, normalized (collapsing ``..``), is the root or under it.

    Pure path math (no filesystem access / no symlink resolution): rejects
    traversal like ``/workspace/../etc``. Treats paths as POSIX (the sandbox is
    Linux).
    """
    norm = posixpath.normpath(value)
    root_norm = posixpath.normpath(root)
    if norm == root_norm:
        return True
    prefix = root_norm if root_norm.endswith("/") else root_norm + "/"
    return norm.startswith(prefix)


def _eval_predicate(key: str, expected: Any, value: Any) -> str:
    """Evaluate ONE predicate -> _MATCH / _NOMATCH / _INDET."""
    if key == "const":
        return _MATCH if _strict_eq(value, expected) else _NOMATCH
    if key == "enum":
        return _MATCH if any(_strict_eq(value, e) for e in expected) else _NOMATCH
    if key == "type":
        # A type MATCH is provable; a mismatch is INDETERMINATE (not a safe
        # skip) so a deny rule guarded by `type` still fires on wrong-type args.
        return _MATCH if _is_type(value, expected) else _INDET
    if key == "pattern":
        if not isinstance(value, str):
            return _INDET
        try:
            return _MATCH if _compile(expected).search(value) is not None else _NOMATCH
        except re.error:
            return _INDET
    if key in ("prefix", "suffix", "contains", "not_contains"):
        if not isinstance(value, str):
            return _INDET
        if key == "prefix":
            return _MATCH if value.startswith(expected) else _NOMATCH
        if key == "suffix":
            return _MATCH if value.endswith(expected) else _NOMATCH
        if key == "contains":
            return _MATCH if expected in value else _NOMATCH
        return _MATCH if expected not in value else _NOMATCH  # not_contains
    if key in ("min", "max"):
        if not _is_number(value):
            return _INDET
        ok = value >= expected if key == "min" else value <= expected
        return _MATCH if ok else _NOMATCH
    if key in ("min_length", "max_length"):
        try:
            n = len(value)
        except TypeError:
            return _INDET
        ok = n >= expected if key == "min_length" else n <= expected
        return _MATCH if ok else _NOMATCH
    if key == "path_under":
        if not isinstance(value, str):
            return _INDET
        return _MATCH if _path_under(value, expected) else _NOMATCH
    # Unknown predicates are rejected at parse time and never reach here.
    return _INDET


def _eval_constraint(value: Any, constraint: Any) -> str:
    """Combine a constraint's predicates over a present value -> tri-state.

    NOMATCH if any predicate is provably false; else INDET if any is
    indeterminate; else MATCH. A bare constraint is exact equality.
    """
    if not isinstance(constraint, dict):
        return _MATCH if _strict_eq(value, constraint) else _NOMATCH
    result = _MATCH
    for key, expected in constraint.items():
        r = _eval_predicate(key, expected, value)
        if r == _NOMATCH:
            return _NOMATCH
        if r == _INDET:
            result = _INDET
    return result


def _resolve_arg(args: dict, dotted: str):
    """Resolve a (possibly dotted) arg name -> (present: bool, value)."""
    if "." not in dotted:
        return (dotted in args, args.get(dotted))
    cur: Any = args
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return (False, None)
        cur = cur[seg]
    return (True, cur)


def _args_result(constraints: dict, args: dict) -> str:
    """Tri-state over ALL of a rule's argument constraints.

    NOMATCH if any constraint is provably unsatisfied (the call is definitively
    outside this rule's scope); INDET if some constraint can't be evaluated
    (missing/wrong-type arg); MATCH only if every constraint provably holds.
    """
    result = _MATCH
    for arg_name, constraint in constraints.items():
        present, value = _resolve_arg(args, arg_name)
        r = _INDET if not present else _eval_constraint(value, constraint)
        if r == _NOMATCH:
            return _NOMATCH
        if r == _INDET:
            result = _INDET
    return result


# ── policy ─────────────────────────────────────────────────────────────────────
@dataclass
class _Policy:
    default: str = _ALLOW
    rules: list = field(default_factory=list)  # normalized: {tool, args, effect, fallback, label}

    def evaluate(self, tool_name: str, args: dict) -> GateResult:
        for rule in self.rules:
            tool = rule["tool"]
            if tool != "*" and tool != tool_name:
                continue
            res = _args_result(rule["args"], args)
            if rule["effect"] == _ALLOW:
                if res == _MATCH:  # provably in scope -> grant
                    return GateResult.allow(rule["label"])
                # NOMATCH / INDET -> allow rule does not grant; keep looking
            else:  # deny: fire unless provably out of scope
                if res != _NOMATCH:
                    return GateResult.block(rule["fallback"], rule["label"])
        if self.default == _DENY:
            return GateResult.block("No policy rule permits this action.", "default-deny")
        return GateResult.allow("default-allow")


def _validate_constraint(constraint: Any, where: str) -> None:
    if not isinstance(constraint, dict):
        return  # bare value -> exact-equality const
    for key, expected in constraint.items():
        if key not in _KNOWN_PREDICATES:
            raise _PolicyError(f"{where}: unknown predicate {key!r}")
        if key == "pattern":
            if not isinstance(expected, str):
                raise _PolicyError(f"{where}: 'pattern' must be a string")
            try:
                _compile(expected)
            except re.error as exc:
                raise _PolicyError(f"{where}: invalid regex {expected!r}: {exc}")
        if key in ("prefix", "suffix", "path_under", "contains", "not_contains") and not isinstance(expected, str):
            raise _PolicyError(f"{where}: {key!r} must be a string")
        if key == "enum" and not isinstance(expected, list):
            raise _PolicyError(f"{where}: 'enum' must be a list")
        if key == "type" and expected not in _VALID_TYPES:
            raise _PolicyError(f"{where}: invalid type {expected!r}")
        if key in ("min", "max") and not (isinstance(expected, (int, float)) and not isinstance(expected, bool)):
            raise _PolicyError(f"{where}: {key!r} must be a number")
        if key in ("min_length", "max_length") and not (isinstance(expected, int) and not isinstance(expected, bool)):
            raise _PolicyError(f"{where}: {key!r} must be an integer")


def _parse_policy_file(path: Path) -> _Policy:
    """Parse + validate a policy file. Raises _PolicyError on any invalidity."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise _PolicyError(f"unreadable/invalid JSON: {exc}")
    if not isinstance(data, dict):
        raise _PolicyError("top level must be a JSON object")
    default = str(data.get("default", _ALLOW)).strip().lower()
    if default not in (_ALLOW, _DENY):
        raise _PolicyError(f"invalid 'default' {data.get('default')!r}")
    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        raise _PolicyError("'rules' must be a list")
    rules = []
    for i, rule in enumerate(raw_rules):
        if not isinstance(rule, dict):
            raise _PolicyError(f"rule #{i} must be an object")
        tool = rule.get("tool", "*")
        if not isinstance(tool, str):
            raise _PolicyError(f"rule #{i}: 'tool' must be a string")
        effect = str(rule.get("effect", _ALLOW)).strip().lower()
        # Anything that is not exactly "allow" is treated as deny (fail safe).
        effect = _ALLOW if effect == _ALLOW else _DENY
        constraints = rule.get("args", {}) or {}
        if not isinstance(constraints, dict):
            raise _PolicyError(f"rule #{i}: 'args' must be an object")
        for arg_name, constraint in constraints.items():
            _validate_constraint(constraint, f"rule #{i} arg {arg_name!r}")
        label = rule.get("name") or f"{tool}#{i}"
        rules.append({
            "tool": tool,
            "args": constraints,
            "effect": effect,
            "fallback": rule.get("fallback", ""),
            "label": str(label),
        })
    return _Policy(default=default, rules=rules)


def _default_policy_dirs() -> list[Path]:
    dirs: list[Path] = []
    try:
        from core import paths as _paths  # type: ignore

        dirs.append(Path(_paths.user_dir()) / "policies")
    except Exception:
        pass
    dirs.append(Path(__file__).resolve().parents[1] / "core" / "config" / "policies")
    return dirs


# ── the gate ────────────────────────────────────────────────────────────────────
class ToolCallPolicyGate:
    """Deterministic per-agent, per-argument tool-call policy gate.

    One instance per process (see the module-level ``gate`` singleton). Policy
    dirs are resolved lazily on each load (so a late-set MYAI_USER_DATA is
    honoured and there is no import-time filesystem I/O). Parsed policies are
    cached by (path, mtime); edits and deletions take effect without a restart.
    """

    def __init__(self, policies_dirs: "list[Path] | None" = None):
        # None => resolve _default_policy_dirs() lazily per load.
        self._dirs = policies_dirs
        # agent_id -> (path_str, mtime_ns, _Policy | _CORRUPT)
        self._cache: dict[str, tuple] = {}
        self._warned: set[str] = set()

    def check(self, agent_id: str, tool_name: str, args: "dict | None") -> GateResult:
        """Return ALLOW or BLOCK(fallback) for one tool call. Never raises."""
        try:
            policy = self._load(agent_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("progent_gate: policy load error (agent=%r): %s", agent_id, exc)
            return GateResult.block("Tool-call policy could not be loaded.", "load-error")

        if policy is _CORRUPT:
            return GateResult.block(
                f"The tool-call policy for agent '{agent_id}' is invalid; tool calls are refused until it is fixed.",
                "policy-invalid",
            )
        if policy is None:
            if PROGENT_FAIL_CLOSED:
                return GateResult.block(
                    f"No tool-call policy is defined for agent '{agent_id}'.", "fail-closed",
                )
            if agent_id not in self._warned:
                self._warned.add(agent_id)
                log.warning(
                    "progent_gate: no policy for agent %r — allowing all tool calls (fail-open)", agent_id,
                )
            return GateResult.allow("fail-open")

        try:
            return policy.evaluate(tool_name, dict(args or {}))
        except Exception as exc:  # defensive: an eval error on a real policy fails CLOSED
            log.error("progent_gate: evaluation error (agent=%r tool=%r): %s", agent_id, tool_name, exc)
            return GateResult.block("Policy evaluation error; tool call refused.", "eval-error")

    def _resolve_path(self, agent_id: str) -> "Path | None":
        dirs = self._dirs if self._dirs is not None else _default_policy_dirs()
        for d in dirs:
            for name in (f"{agent_id}.json", "default.json"):
                p = d / name
                try:
                    if p.is_file():
                        return p
                except OSError:
                    continue
        return None

    def _load(self, agent_id: str):
        p = self._resolve_path(agent_id)
        if p is None:
            self._cache.pop(agent_id, None)  # file deleted -> drop stale entry
            return None
        try:
            mtime = p.stat().st_mtime_ns
        except OSError:
            self._cache.pop(agent_id, None)
            return None
        cached = self._cache.get(agent_id)
        if cached and cached[0] == str(p) and cached[1] == mtime:
            return cached[2]
        try:
            value: Any = _parse_policy_file(p)
        except _PolicyError as exc:
            log.error("progent_gate: invalid policy %s: %s (failing closed)", p, exc)
            value = _CORRUPT
        self._cache[agent_id] = (str(p), mtime, value)
        return value

    def reload(self) -> None:
        """Drop cached policies and no-policy warnings (also done automatically on mtime change)."""
        self._cache.clear()
        self._warned.clear()


def to_args_dict(args: "list | tuple | None", kwargs: "dict | None") -> dict:
    """Map a CaMeL call's positional ``args`` + ``kwargs`` to a name->value dict.

    Keyword arguments are used by name. Positional arguments carry no parameter
    name at the interpreter layer, so they are exposed under ``"$0"``, ``"$1"``,
    … A deny rule keyed on a NAMED argument will therefore see that name as
    missing for a positional call — which, under the fail-safe deny semantics in
    this module, makes the deny FIRE rather than be bypassed.
    """
    merged: dict = dict(kwargs or {})
    for i, v in enumerate(args or []):
        merged.setdefault(f"${i}", v)
    return merged


# Module-level singleton used by the dispatch choke point (services/camel/adapter.py).
gate = ToolCallPolicyGate()
