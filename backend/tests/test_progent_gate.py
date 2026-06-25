"""
tests/test_progent_gate.py

security.progent_gate — deterministic, per-argument tool-call policy gate
(clean-room implementation of the Progent concept, arXiv:2504.11703). It
complements GovernanceEngine's name/budget checks with argument-level
constraints, enforced at the CaMeL tool-dispatch choke point.

Covers:
  - Fail-open when an agent has no policy (existing agents unaffected) and the
    PROGENT_FAIL_CLOSED override.
  - Rule semantics: allow/deny by tool name, argument constraints, priority
    (first match wins), wildcard tool, and the policy `default` fallback.
  - Argument predicates (pattern / enum / type / numeric / length / prefix).
  - Fresh policy files activate without a restart; to_args_dict mapping.
  - The shipped _example.json sample parses and enforces as documented.
  - Integration through the real services/camel/adapter.py choke point: a
    blocked call raises CamelToolDenied (so the interpreter records a
    blocked_call and the plan keeps running), an allowed call proceeds, and a
    no-policy agent behaves identically to before the gate existed.
"""

import json
from pathlib import Path

import pytest

import security.progent_gate as pg
from security.progent_gate import ToolCallPolicyGate, to_args_dict


# ── helpers / fixtures ────────────────────────────────────────────────────────

def _write_policy(d: Path, agent_id: str, policy: dict) -> None:
    (d / f"{agent_id}.json").write_text(json.dumps(policy), encoding="utf-8")


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
    assert res.allowed is True
    assert res.rule == "fail-open"


def test_no_policy_fail_closed(monkeypatch, gate):
    monkeypatch.setattr(pg, "PROGENT_FAIL_CLOSED", True)
    res = gate.check("ghost", "send_email", {})
    assert res.allowed is False
    assert res.fallback_message


# ── rule semantics ────────────────────────────────────────────────────────────

def test_deny_rule_by_name(policies_dir, gate):
    _write_policy(policies_dir, "a", {
        "default": "allow",
        "rules": [{"tool": "send_email", "effect": "deny", "fallback": "no email"}],
    })
    res = gate.check("a", "send_email", {})
    assert res.allowed is False
    assert res.fallback_message == "no email"


def test_allow_with_arg_prefix_then_default_deny(policies_dir, gate):
    _write_policy(policies_dir, "a", {
        "default": "deny",
        "rules": [{"tool": "read_file", "args": {"path": {"prefix": "/workspace/"}}, "effect": "allow"}],
    })
    assert gate.check("a", "read_file", {"path": "/workspace/x"}).allowed is True
    # outside /workspace: the allow-rule's constraint fails, so it does not apply
    # and the call falls through to the policy default (deny).
    assert gate.check("a", "read_file", {"path": "/etc/passwd"}).allowed is False
    # an unmatched tool also falls through to default deny.
    assert gate.check("a", "whatever", {}).allowed is False


def test_priority_first_match_wins(policies_dir, gate):
    _write_policy(policies_dir, "a", {
        "default": "deny",
        "rules": [
            {"tool": "fs", "args": {"path": {"prefix": "/tmp/"}}, "effect": "deny", "fallback": "no /tmp"},
            {"tool": "fs", "effect": "allow"},
        ],
    })
    assert gate.check("a", "fs", {"path": "/tmp/x"}).allowed is False   # deny rule first
    assert gate.check("a", "fs", {"path": "/var/x"}).allowed is True    # falls to allow rule


def test_wildcard_tool_and_positional_arg(policies_dir, gate):
    _write_policy(policies_dir, "a", {
        "default": "allow",
        "rules": [{"tool": "*", "args": {"$0": {"contains": "rm -rf"}}, "effect": "deny", "fallback": "danger"}],
    })
    assert gate.check("a", "shell", {"$0": "rm -rf /"}).allowed is False
    assert gate.check("a", "shell", {"$0": "ls -la"}).allowed is True


# ── argument predicates ───────────────────────────────────────────────────────

@pytest.mark.parametrize("constraint,value,ok", [
    ({"pattern": r"^https://api\.github\.com/"}, "https://api.github.com/x", True),
    ({"pattern": r"^https://api\.github\.com/"}, "https://evil.example/x", False),
    ({"enum": ["a", "b"]}, "b", True),
    ({"enum": ["a", "b"]}, "c", False),
    ({"type": "integer"}, 3, True),
    ({"type": "integer"}, "3", False),
    ({"type": "integer"}, True, False),      # bool is not an integer
    ({"max": 10}, 5, True),
    ({"max": 10}, 11, False),
    ({"max_length": 3}, "abcd", False),
    ({"prefix": "/ok/"}, "/ok/x", True),
    ({"prefix": "/ok/"}, 123, False),        # type mismatch -> predicate fails, not raises
])
def test_arg_predicates(policies_dir, gate, constraint, value, ok):
    _write_policy(policies_dir, "a", {
        "default": "deny",
        "rules": [{"tool": "t", "args": {"x": constraint}, "effect": "allow"}],
    })
    assert gate.check("a", "t", {"x": value}).allowed is ok


# ── caching / freshness / helpers ─────────────────────────────────────────────

def test_new_policy_picked_up_without_restart(policies_dir, gate):
    assert gate.check("a", "send_email", {}).allowed is True   # no policy yet -> fail-open
    _write_policy(policies_dir, "a", {
        "default": "allow",
        "rules": [{"tool": "send_email", "effect": "deny", "fallback": "no"}],
    })
    # no-policy results are not cached, so the new file takes effect immediately.
    assert gate.check("a", "send_email", {}).allowed is False


def test_to_args_dict_maps_positional_and_keyword():
    assert to_args_dict(["p0", "p1"], {"k": "v"}) == {"k": "v", "$0": "p0", "$1": "p1"}


def test_malformed_policy_fails_open(policies_dir, gate):
    (policies_dir / "a.json").write_text("{ not json", encoding="utf-8")
    assert gate.check("a", "anything", {}).allowed is True


# ── shipped example sample ────────────────────────────────────────────────────

def test_example_policy_is_valid(tmp_path):
    example = Path(__file__).resolve().parents[1] / "core" / "config" / "policies" / "_example.json"
    data = json.loads(example.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("rules"), list)
    d = tmp_path / "policies"
    d.mkdir()
    (d / "demo.json").write_text(json.dumps(data), encoding="utf-8")
    g = ToolCallPolicyGate(policies_dirs=[d])
    assert g.check("demo", "send_email", {}).allowed is False
    assert g.check("demo", "read_file", {"path": "/workspace/x"}).allowed is True
    assert g.check("demo", "read_file", {"path": "/etc/x"}).allowed is False


# ── integration through the real adapter choke point ──────────────────────────

@pytest.fixture
def adapter_with_gate(policies_dir, monkeypatch):
    import services.camel.adapter as adapter_mod
    monkeypatch.setattr(adapter_mod, "_policy_gate", ToolCallPolicyGate(policies_dirs=[policies_dir]))
    return adapter_mod


def test_adapter_blocks_violating_call(policies_dir, adapter_with_gate):
    from services.camel.exceptions import CamelToolDenied
    _write_policy(policies_dir, "agentX", {
        "default": "allow",
        "rules": [{"tool": "send_email", "effect": "deny", "fallback": "Email disabled."}],
    })
    ex = adapter_with_gate.make_tool_executor_for_turn(
        agent_id="agentX", conversation_id="c", governance=None, execution_bridge=None,
    )
    with pytest.raises(CamelToolDenied) as ei:
        ex("send_email", [], {"to": "a@b.com"})
    assert ei.value.reason == "Email disabled."
    assert ei.value.policy_name.startswith("progent:")


def test_adapter_allows_proceeds_and_failopen_is_identical(policies_dir, adapter_with_gate):
    from services.camel.capabilities import CapabilityTaggedResult
    from services.camel.exceptions import CamelToolDenied

    _write_policy(policies_dir, "agentX", {
        "default": "deny",
        "rules": [{"tool": "web_search", "effect": "allow"}],
    })
    ex = adapter_with_gate.make_tool_executor_for_turn(
        agent_id="agentX", conversation_id="c", governance=None, execution_bridge=None,
    )
    # allowed -> proceeds; bridge is stubbed (None) so value is None.
    out = ex("web_search", [], {"q": "hi"})
    assert isinstance(out, CapabilityTaggedResult) and out.value is None
    # default-deny tool -> blocked.
    with pytest.raises(CamelToolDenied):
        ex("other_tool", [], {})

    # agent with NO policy -> identical pre-existing behaviour (allowed, value None).
    ex2 = adapter_with_gate.make_tool_executor_for_turn(
        agent_id="nopolicy", conversation_id="c", governance=None, execution_bridge=None,
    )
    out2 = ex2("send_email", [], {})
    assert isinstance(out2, CapabilityTaggedResult) and out2.value is None
