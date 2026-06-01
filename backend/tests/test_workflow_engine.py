"""
tests/test_workflow_engine.py — explicit workflow DAG execution.

Uses a fake HubRouter that returns scripted outputs so the engine's ordering,
conditional branching, checkpointing and resume logic are tested without real
model calls.
"""

from __future__ import annotations

import pytest

from models import WorkerResult


class FakeHub:
    """Records invocations and returns scripted outputs keyed by the user
    message content (substring match)."""

    def __init__(self, scripts=None, fail_on=None):
        self.scripts = scripts or {}
        self.fail_on = fail_on or set()
        self.calls = []

    def invoke(self, decision, system, messages, max_tokens=2048, agent_role="workflow", **kw):
        content = messages[0]["content"]
        self.calls.append(content)
        for key, out in self.scripts.items():
            if key in content:
                if key in self.fail_on:
                    return WorkerResult(text="", backend="local", model_name="m",
                                        had_error=True)
                return WorkerResult(text=out, backend="local", model_name="m")
        return WorkerResult(text=f"output for: {content[:30]}", backend="local",
                            model_name="m")


def _engine(in_memory_db, hub, settings):
    from services.workflow_engine import WorkflowEngine
    events = []
    eng = WorkflowEngine(hub, settings, on_event=lambda e, p: events.append((e, p)))
    return eng, events


def test_create_and_run_linear(in_memory_db, settings):
    hub = FakeHub()
    eng, events = _engine(in_memory_db, hub, settings)
    wf_id = eng.create_workflow("linear", [
        {"name": "a", "agent_role": "analyst", "prompt": "do a"},
        {"name": "b", "agent_role": "coder", "prompt": "do b", "depends_on": ["a"]},
    ])
    result = eng.run(wf_id)
    assert result["status"] == "completed"
    states = {t["name"]: t["status"] for t in result["tasks"]}
    assert states == {"a": "completed", "b": "completed"}
    # b ran after a and saw a's output in its prompt context.
    assert any("Inputs from previous steps" in c for c in hub.calls)
    assert any(e[0] == "workflow_finished" for e in events)


def test_topological_order_respected(in_memory_db, settings):
    hub = FakeHub()
    eng, _ = _engine(in_memory_db, hub, settings)
    # c depends on b depends on a, declared out of order.
    wf_id = eng.create_workflow("ordered", [
        {"name": "c", "prompt": "c", "depends_on": ["b"]},
        {"name": "a", "prompt": "a"},
        {"name": "b", "prompt": "b", "depends_on": ["a"]},
    ])
    eng.run(wf_id)
    order = [c.split("\n")[0] for c in hub.calls]
    assert order.index("a") < order.index("b") < order.index("c")


def test_conditional_branch_skips(in_memory_db, settings):
    # review outputs FAIL → the gated 'build' task must be skipped.
    hub = FakeHub(scripts={"review": "Looks bad. GATE: FAIL"})
    eng, _ = _engine(in_memory_db, hub, settings)
    wf_id = eng.create_workflow("gated", [
        {"name": "review", "prompt": "review this"},
        {"name": "build", "prompt": "build it", "depends_on": ["review"],
         "condition": {"when": {"task": "review", "contains": "GATE: PASS"}}},
    ])
    result = eng.run(wf_id)
    states = {t["name"]: t["status"] for t in result["tasks"]}
    assert states["review"] == "completed"
    assert states["build"] == "skipped"


def test_conditional_branch_runs_when_pass(in_memory_db, settings):
    hub = FakeHub(scripts={"review": "All good. GATE: PASS"})
    eng, _ = _engine(in_memory_db, hub, settings)
    wf_id = eng.create_workflow("gated", [
        {"name": "review", "prompt": "review this"},
        {"name": "build", "prompt": "build it", "depends_on": ["review"],
         "condition": {"when": {"task": "review", "contains": "GATE: PASS"}}},
    ])
    result = eng.run(wf_id)
    states = {t["name"]: t["status"] for t in result["tasks"]}
    assert states["build"] == "completed"


def test_failure_rolls_back_and_resume_continues(in_memory_db, settings):
    import db
    # First run: task 'b' fails.
    hub = FakeHub(scripts={"do b": "b-out"}, fail_on={"do b"})
    eng, _ = _engine(in_memory_db, hub, settings)
    wf_id = eng.create_workflow("resumable", [
        {"name": "a", "prompt": "do a"},
        {"name": "b", "prompt": "do b", "depends_on": ["a"]},
        {"name": "c", "prompt": "do c", "depends_on": ["b"]},
    ])
    result = eng.run(wf_id)
    assert result["status"] == "failed"
    states = {t["name"]: t["status"] for t in result["tasks"]}
    assert states["a"] == "completed"
    assert states["b"] == "failed"
    assert states["c"] == "pending"  # never reached
    # A rolled_back checkpoint was written for b.
    ck = db.fetchall("SELECT state FROM workflow_checkpoints WHERE workflow_id = ?", (wf_id,))
    assert any(c["state"] == "rolled_back" for c in ck)

    # Fix the failure and resume: a is skipped (already done), b+c run.
    hub2 = FakeHub(scripts={"do b": "b-out"})
    eng2, _ = _engine(in_memory_db, hub2, settings)
    result2 = eng2.resume(wf_id)
    assert result2["status"] == "completed"
    states2 = {t["name"]: t["status"] for t in result2["tasks"]}
    assert states2 == {"a": "completed", "b": "completed", "c": "completed"}
    # 'a' was not re-run on resume.
    assert not any(c.startswith("do a") for c in hub2.calls)


def test_cycle_detected(in_memory_db, settings):
    from services.workflow_engine import WorkflowError
    hub = FakeHub()
    eng, _ = _engine(in_memory_db, hub, settings)
    # Manually craft a cycle by editing depends_on after creation.
    import db, json
    wf_id = eng.create_workflow("cyclic", [
        {"name": "a", "prompt": "a", "depends_on": ["b"]},
        {"name": "b", "prompt": "b"},
    ])
    rows = db.fetchall("SELECT id, name FROM tasks WHERE workflow_id = ?", (wf_id,))
    ids = {r["name"]: r["id"] for r in rows}
    db.execute("UPDATE tasks SET depends_on = ? WHERE id = ?",
               (json.dumps([ids["a"]]), ids["b"]))
    db.commit()
    with pytest.raises(WorkflowError):
        eng.run(wf_id)
