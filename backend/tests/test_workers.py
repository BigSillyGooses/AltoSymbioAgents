"""
tests/test_workers.py — background workers framework + built-in workers.

Exercises enqueue → run_task transitions and each built-in worker against real
in-memory data (no placeholders).
"""

from __future__ import annotations

import pytest


def test_list_workers():
    from services import workers
    names = {w["name"] for w in workers.list_workers()}
    assert {"reindex", "memory_audit", "trajectory_report"} <= names


def test_enqueue_unknown_worker(in_memory_db):
    from services import workers
    r = workers.enqueue("does_not_exist")
    assert r["ok"] is False


def test_enqueue_and_run_reindex(in_memory_db):
    from services import workers
    q = workers.enqueue("reindex")
    assert q["ok"]
    task_id = q["task_id"]
    assert workers.get_task(task_id)["status"] == "pending"

    out = workers.run_task(task_id)
    assert out["ok"]
    task = workers.get_task(task_id)
    assert task["status"] == "done"
    assert task["progress"] == 1.0
    assert "indexed" in task["result"]


def test_memory_audit_counts(in_memory_db):
    import db
    from datetime import datetime, timezone
    from services import workers
    now = datetime.now(timezone.utc).isoformat()
    # Seed a conversation (session_facts FK-references it), plus memory rows.
    db.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) "
        "VALUES (?,?,?,?)", ("c", "t", now, now))
    db.execute(
        "INSERT INTO memory_entries (id, session_id, content, category, created_at, "
        "last_accessed, embedding_status) VALUES (?,?,?,?,?,?, 'clean')",
        ("m1", "s", "remember the api budget", "fact", now, now))
    db.execute(
        "INSERT INTO session_facts (id, conversation_id, fact, source, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("f1", "c", "user likes dark mode", "chat", "active", now))
    db.commit()

    q = workers.enqueue("memory_audit", {"days": 30})
    workers.run_task(q["task_id"])
    result = workers.get_task(q["task_id"])["result"]
    assert result["memory_entries"] >= 1
    assert result["session_facts"] >= 1
    assert result["stale_days"] == 30


def test_trajectory_report_success_rates(in_memory_db):
    import db
    from datetime import datetime, timezone
    from services import workers
    now = datetime.now(timezone.utc).isoformat()

    def add(agent, verdict, had_error):
        import uuid
        db.execute(
            "INSERT INTO trajectories (id, conversation_id, turn_id, task_text, "
            "agent_id, skill_matched, backend, model_name, routing_score, "
            "route_reasoning, quality_verdict, had_error, response_empty, "
            "tokens_in, tokens_out, embedding_status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'dirty', ?)",
            (str(uuid.uuid4()), "c", "t", "task", agent, "s", "claude", "m", 0.7,
             "r", verdict, 1 if had_error else 0, 0, 1, 1, now))
    add("agent-x", "success", False)
    add("agent-x", "success", False)
    add("agent-x", "2.3", True)
    db.commit()

    q = workers.enqueue("trajectory_report")
    workers.run_task(q["task_id"])
    result = workers.get_task(q["task_id"])["result"]
    assert result["total_trajectories"] == 3
    agent_x = next(a for a in result["agents"] if a["agent"] == "agent-x")
    assert agent_x["total"] == 3
    assert agent_x["successes"] == 2
    assert agent_x["success_rate"] == round(2 / 3, 3)


def test_run_task_records_error(in_memory_db, monkeypatch):
    from services import workers
    # Make the reindex worker blow up to exercise the error path.
    monkeypatch.setattr(
        workers.WORKERS["reindex"], "run",
        lambda params, progress: (_ for _ in ()).throw(RuntimeError("boom")))
    q = workers.enqueue("reindex")
    out = workers.run_task(q["task_id"])
    assert out["ok"] is False
    task = workers.get_task(q["task_id"])
    assert task["status"] == "error"
    assert "boom" in task["error"]


def test_run_task_already_claimed_is_skipped(in_memory_db):
    """If the row is no longer 'pending' (another runner claimed it), run_task
    bails without re-executing — closing the API-thread vs daemon race."""
    import db
    from services import workers
    q = workers.enqueue("reindex")
    task_id = q["task_id"]
    # Simulate the daemon (or the immediate-run thread) having claimed it.
    db.execute("UPDATE worker_tasks SET status = 'running' WHERE id = ?", (task_id,))
    db.commit()

    out = workers.run_task(task_id)
    assert out["ok"] is False
    assert "claimed" in out["error"]
    # The second runner must not have flipped it to done.
    assert workers.get_task(task_id)["status"] == "running"
