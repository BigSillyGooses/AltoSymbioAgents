"""
tests/test_progent_gate.py

security.progent_gate — deterministic, per-argument tool-call policy gate
(clean-room implementation of the Progent concept, arXiv:2504.11703). It
complements GovernanceEngine's name/budget checks with argument-level
constraints, enforced at the CaMeL tool-dispatch choke point.

Covers (incl. the hardening from the adversarial review):
  - No-policy fail-open / PROGENT_FAIL_CLOSED; INVALID policy fails CLOSED.
  - Rule semantics: priority, wildcard, default, effect normalization
    (anything != "allow" blocks).
  - DENY rules FAIL SAFE: a deny fires on missing / positional / wrong-type
    args (no silent bypass) but is correctly skipped when provably out of scope.
  - Argument predicates incl. path_under (".." traversal rejected), nested
    dotted keys, and strict bool != int.
  - Validation failures (bad default / bad regex / unknown predicate / corrupt
    file) fail closed.
  - mtime-based hot reload of edits and deletions; default.json fallback.
  - Integration through the real services/camel/adapter.py choke point.
"""

import json
import os
from pathlib import Path

import pytest

import security.progent_gate as pg
from security.progent_gate import ToolCallPolicyGate, to_args_dict


# ── helpers / fixtures ────────────────────────────────────────────────────────

def _write(d: Path, agent_id: str, policy: dict) -> Path:
    p = d / f"{agent_id}.json"
    p.write_text(json.dumps(policy), encoding="utf-8")
    return p


@pytest.fixture
def policies_dir(tmp_path):
    d = tmp_path / "policies"
    d.mkdir()
    return d


@pytest.fixture
def gate(policies_dir):
    return ToolCallPolicyGate(policies_dirs=[policies_dir])


# ── fail-open / fail-closed ───────────────────────────────────────────────────

def test_no_policy_fails_open(gate):
    res = gate.check("ghost", "send_email", {"to": "x@y.com"})
    assert res.allowed is True and res.rule == "fail-open"


def test_no_policy_fail_closed(monkeypatch, gate):
    monkeypatch.setattr(pg, "PROGENT_FAIL_CLOSED", True)
    res = gate.check("ghost", "send_email", {})
    assert res.allowed is False and res.fallback_message


# ── basic rule semantics ──────────────────────────────────────────────────────

def test_deny_rule_by_name(policies_dir, gate):
    _write(policies_dir, "a", {"default": "allow",
           "rules": [{"tool": "send_email", "effect": "deny", "fallback": "no email"}]})
    res = gate.check("a", "send_email", {})
    assert res.allowed is False and res.fallback_message == "no email"


def test_priority_first_match_wins(policies_dir, gate):
    _write(policies_dir, "a", {"default": "deny", "rules": [
        {"tool": "fs", "args": {"path": {"prefix": "/tmp/"}}, "effect": "deny", "fallback": "no /tmp"},
        {"tool": "fs", "effect": "allow"},
    ]})
    assert gate.check("a", "fs", {"path": "/tmp/x"}).allowed is False
    assert gate.check("a", "fs", {"path": "/var/x"}).allowed is True


def test_wildcard_tool_and_positional_arg(policies_dir, gate):
    _write(policies_dir, "a", {"default": "allow",
           "rules": [{"tool": "*", "args": {"$0": {"contains": "rm -rf"}}, "effect": "deny", "fallback": "danger"}]})
    assert gate.check("a", "shell", {"$0": "rm -rf /"}).allowed is False
    assert gate.check("a", "shell", {"$0": "ls -la"}).allowed is True


@pytest.mark.parametrize("effect,blocks", [
    ("deny", True), ("DENY", True), ("deny ", True), ("block", True),
    ("reject", True), ("forbid", True), ("allow", False), ("ALLOW", False),
])
def test_effect_only_allow_permits(policies_dir, gate, effect, blocks):
    # default deny so a non-blocking matched rule must explicitly allow.
    _write(policies_dir, "a", {"default": "deny",
           "rules": [{"tool": "t", "effect": effect, "fallback": "x"}]})
    assert gate.check("a", "t", {}).allowed is (not blocks)


# ── DENY rules fail SAFE ──────────────────────────────────────────────────────

def test_deny_fires_on_missing_or_positional_arg(policies_dir, gate):
    _write(policies_dir, "a", {"default": "allow",
           "rules": [{"tool": "read_file", "args": {"path": {"prefix": "/etc"}}, "effect": "deny", "fallback": "no"}]})
    assert gate.check("a", "read_file", {"path": "/etc/shadow"}).allowed is False   # in scope
    assert gate.check("a", "read_file", {"path": "/home/ok"}).allowed is True        # provably out -> allowed
    assert gate.check("a", "read_file", {"$0": "/etc/shadow"}).allowed is False      # positional -> still blocked
    assert gate.check("a", "read_file", {}).allowed is False                         # missing arg -> still blocked


def test_deny_fires_on_type_mismatch(policies_dir, gate):
    _write(policies_dir, "a", {"default": "allow",
           "rules": [{"tool": "transfer", "args": {"amount": {"min": 1000}}, "effect": "deny", "fallback": "cap"}]})
    assert gate.check("a", "transfer", {"amount": 5000}).allowed is False     # >= 1000 -> deny
    assert gate.check("a", "transfer", {"amount": 500}).allowed is True       # provably out -> allowed
    assert gate.check("a", "transfer", {"amount": "5000"}).allowed is False   # wrong type -> deny fires


def test_deny_with_type_guard_fires_on_wrong_type(policies_dir, gate):
    # A deny rule guarded by `type` must still fire on wrong-type args (a type
    # mismatch is indeterminate, not a safe skip).
    _write(policies_dir, "a", {"default": "allow", "rules": [
        {"tool": "shell", "args": {"cmd": {"type": "string", "contains": "rm -rf"}},
         "effect": "deny", "fallback": "blocked"}]})
    assert gate.check("a", "shell", {"cmd": "ls"}).allowed is True            # provably out of scope
    assert gate.check("a", "shell", {"cmd": "rm -rf /"}).allowed is False     # in scope
    assert gate.check("a", "shell", {"cmd": ["rm", "-rf", "/"]}).allowed is False  # wrong type -> deny fires


def test_allow_rule_still_conservative(policies_dir, gate):
    # allow grants only when the constraint is PROVABLY satisfied.
    _write(policies_dir, "a", {"default": "deny",
           "rules": [{"tool": "read_file", "args": {"path": {"path_under": "/workspace/"}}, "effect": "allow"}]})
    assert gate.check("a", "read_file", {"path": "/workspace/x"}).allowed is True
    assert gate.check("a", "read_file", {"path": "/etc/x"}).allowed is False       # not under -> default deny
    assert gate.check("a", "read_file", {"$0": "/workspace/x"}).allowed is False   # positional -> no grant -> deny


# ── argument predicates ───────────────────────────────────────────────────────

@pytest.mark.parametrize("constraint,value,ok", [
    ({"pattern": r"^https://api\.github\.com/"}, "https://api.github.com/x", True),
    ({"pattern": r"^https://api\.github\.com/"}, "https://evil.example/x", False),
    ({"enum": ["a", "b"]}, "b", True),
    ({"enum": ["a", "b"]}, "c", False),
    ({"type": "integer"}, 3, True),
    ({"type": "integer"}, "3", False),
    ({"type": "integer"}, True, False),
    ({"max": 10}, 5, True),
    ({"max": 10}, 11, False),
    ({"max_length": 3}, "abcd", False),
    ({"prefix": "/ok/"}, "/ok/x", True),
])
def test_arg_predicates_via_allow(policies_dir, gate, constraint, value, ok):
    _write(policies_dir, "a", {"default": "deny",
           "rules": [{"tool": "t", "args": {"x": constraint}, "effect": "allow"}]})
    assert gate.check("a", "t", {"x": value}).allowed is ok


def test_path_under_rejects_traversal(policies_dir, gate):
    _write(policies_dir, "a", {"default": "deny",
           "rules": [{"tool": "read", "args": {"p": {"path_under": "/workspace"}}, "effect": "allow"}]})
    assert gate.check("a", "read", {"p": "/workspace/a/b"}).allowed is True
    assert gate.check("a", "read", {"p": "/workspace"}).allowed is True             # root itself
    assert gate.check("a", "read", {"p": "/workspace/../etc/passwd"}).allowed is False
    assert gate.check("a", "read", {"p": "/workspacex/secret"}).allowed is False    # sibling prefix not under


def test_nested_dotted_arg(policies_dir, gate):
    _write(policies_dir, "a", {"default": "allow",
           "rules": [{"tool": "req", "args": {"opts.url": {"prefix": "http://169.254"}}, "effect": "deny", "fallback": "ssrf"}]})
    assert gate.check("a", "req", {"opts": {"url": "http://169.254.169.254/"}}).allowed is False
    assert gate.check("a", "req", {"opts": {"url": "https://ok/"}}).allowed is True


def test_strict_bool_not_int(policies_dir, gate):
    _write(policies_dir, "a", {"default": "deny",
           "rules": [{"tool": "set", "args": {"level": {"enum": [0, 1, 2]}}, "effect": "allow"}]})
    assert gate.check("a", "set", {"level": 1}).allowed is True
    assert gate.check("a", "set", {"level": True}).allowed is False  # bool is not int 1 here


# ── invalid policies fail CLOSED ──────────────────────────────────────────────

@pytest.mark.parametrize("policy", [
    {"default": "denied", "rules": []},                                   # bad default
    {"default": "allow", "rules": [{"tool": "t", "args": {"x": {"pattern": "("}}}]},  # bad regex
    {"default": "allow", "rules": [{"tool": "t", "args": {"x": {"weird": 1}}}]},      # unknown predicate
    {"default": "allow", "rules": "not-a-list"},                          # bad rules
])
def test_invalid_policy_fails_closed(policies_dir, gate, policy):
    _write(policies_dir, "a", policy)
    res = gate.check("a", "t", {"x": "v"})
    assert res.allowed is False and res.rule == "policy-invalid"


def test_corrupt_json_fails_closed(policies_dir, gate):
    (policies_dir / "a.json").write_text("{ not json", encoding="utf-8")
    assert gate.check("a", "anything", {}).allowed is False


# ── freshness / fallback / helpers ────────────────────────────────────────────

def test_mtime_hot_reload_and_delete(policies_dir, gate):
    p = _write(policies_dir, "a", {"default": "allow", "rules": []})
    assert gate.check("a", "send_email", {}).allowed is True
    # tighten + bump mtime so the change is observed without reload()
    p.write_text(json.dumps({"default": "allow",
                 "rules": [{"tool": "send_email", "effect": "deny", "fallback": "no"}]}), encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert gate.check("a", "send_email", {}).allowed is False
    # delete -> reverts to fail-open
    p.unlink()
    assert gate.check("a", "send_email", {}).allowed is True


def test_default_json_fallback(policies_dir, gate):
    _write(policies_dir, "default", {"default": "deny",
           "rules": [{"tool": "ping", "effect": "allow"}]})
    # an agent with no specific file uses default.json
    assert gate.check("anyagent", "ping", {}).allowed is True
    assert gate.check("anyagent", "other", {}).allowed is False


def test_to_args_dict_maps_positional_and_keyword():
    assert to_args_dict(["p0", "p1"], {"k": "v"}) == {"k": "v", "$0": "p0", "$1": "p1"}


def test_example_policy_is_valid_and_enforces(tmp_path):
    example = Path(__file__).resolve().parents[1] / "core" / "config" / "policies" / "_example.json"
    data = json.loads(example.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("rules"), list)
    d = tmp_path / "policies"
    d.mkdir()
    (d / "demo.json").write_text(json.dumps(data), encoding="utf-8")
    g = ToolCallPolicyGate(policies_dirs=[d])
    assert g.check("demo", "send_email", {}).allowed is False
    assert g.check("demo", "read_file", {"path": "/workspace/x"}).allowed is True
    assert g.check("demo", "read_file", {"path": "/workspace/../etc/passwd"}).allowed is False


# ── integration through the real adapter choke point ──────────────────────────

@pytest.fixture
def adapter_with_gate(policies_dir, monkeypatch):
    import services.camel.adapter as adapter_mod
    monkeypatch.setattr(adapter_mod, "_policy_gate", ToolCallPolicyGate(policies_dirs=[policies_dir]))
    return adapter_mod


def test_adapter_blocks_violating_call(policies_dir, adapter_with_gate):
    from services.camel.exceptions import CamelToolDenied
    _write(policies_dir, "agentX", {"default": "allow",
           "rules": [{"tool": "send_email", "effect": "deny", "fallback": "Email disabled."}]})
    ex = adapter_with_gate.make_tool_executor_for_turn(
        agent_id="agentX", conversation_id="c", governance=None, execution_bridge=None)
    with pytest.raises(CamelToolDenied) as ei:
        ex("send_email", [], {"to": "a@b.com"})
    assert ei.value.reason == "Email disabled."
    assert ei.value.policy_name.startswith("progent:")


def test_adapter_positional_deny_still_blocks(policies_dir, adapter_with_gate):
    from services.camel.exceptions import CamelToolDenied
    _write(policies_dir, "agentX", {"default": "allow",
           "rules": [{"tool": "read_file", "args": {"path": {"prefix": "/etc"}}, "effect": "deny", "fallback": "no"}]})
    ex = adapter_with_gate.make_tool_executor_for_turn(
        agent_id="agentX", conversation_id="c", governance=None, execution_bridge=None)
    # positional /etc read: name "path" absent -> deny fires (no bypass)
    with pytest.raises(CamelToolDenied):
        ex("read_file", ["/etc/shadow"], {})


def test_adapter_allows_and_failopen_identical(policies_dir, adapter_with_gate):
    from services.camel.capabilities import CapabilityTaggedResult
    _write(policies_dir, "agentX", {"default": "deny",
           "rules": [{"tool": "web_search", "effect": "allow"}]})
    ex = adapter_with_gate.make_tool_executor_for_turn(
        agent_id="agentX", conversation_id="c", governance=None, execution_bridge=None)
    out = ex("web_search", [], {"q": "hi"})
    assert isinstance(out, CapabilityTaggedResult) and out.value is None
    # agent with NO policy -> identical pre-existing behaviour (allowed, value None)
    ex2 = adapter_with_gate.make_tool_executor_for_turn(
        agent_id="nopolicy", conversation_id="c", governance=None, execution_bridge=None)
    out2 = ex2("send_email", [], {})
    assert isinstance(out2, CapabilityTaggedResult) and out2.value is None
