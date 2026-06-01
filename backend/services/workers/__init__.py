"""
services/workers/ — background analysis workers.

A small framework for queued, long-running background jobs that operate on
Alto's own data stores (the vector index, the memory tiers, the trajectory
log). Tasks are persisted in the ``worker_tasks`` table; the daemon
(``services.workers.daemon``) polls for pending rows, runs the named worker,
and streams progress over SSE.

Public surface:
  WORKERS          — name -> Worker instance registry
  list_workers()   — metadata for the UI
  enqueue()        — queue a task (returns task_id)
  run_task(id)     — execute one queued task synchronously (used by the daemon
                     and by tests)
  get_task()/list_tasks() — read task state
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import db
import sse_events

from .base import Worker
from .builtin import ReindexWorker, MemoryAuditWorker, TrajectoryReportWorker

log = logging.getLogger("alto.workers")

WORKERS: dict[str, Worker] = {
    w.name: w for w in (
        ReindexWorker(),
        MemoryAuditWorker(),
        TrajectoryReportWorker(),
    )
}

PENDING = "pending"
RUNNING = "running"
DONE = "done"
ERROR = "error"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_workers() -> list[dict]:
    return [{"name": w.name, "description": w.description} for w in WORKERS.values()]


def enqueue(worker: str, params: Optional[dict] = None) -> dict:
    """Queue a worker task. Returns ``{ok, task_id}`` or ``{ok: False, error}``."""
    if worker not in WORKERS:
        return {"ok": False, "error": f"unknown worker: {worker}"}
    task_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO worker_tasks (id, worker, status, params, progress, created_at) "
        "VALUES (?, ?, ?, ?, 0.0, ?)",
        (task_id, worker, PENDING, json.dumps(params or {}), _now()),
    )
    db.commit()
    sse_events.publish("worker_progress", {
        "task_id": task_id, "worker": worker, "status": PENDING, "progress": 0.0,
        "message": "queued",
    })
    return {"ok": True, "task_id": task_id}


def get_task(task_id: str) -> Optional[dict]:
    row = db.fetchone("SELECT * FROM worker_tasks WHERE id = ?", (task_id,))
    return _task_view(row) if row else None


def list_tasks(limit: int = 50) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM worker_tasks ORDER BY created_at DESC LIMIT ?", (limit,))
    return [_task_view(r) for r in rows]


def _task_view(row) -> dict:
    def _loads(v, default):
        try:
            return json.loads(v) if v else default
        except Exception:
            return default
    return {
        "id": row["id"],
        "worker": row["worker"],
        "status": row["status"],
        "params": _loads(row["params"], {}),
        "result": _loads(row["result"], None),
        "error": row["error"],
        "progress": row["progress"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def run_task(task_id: str) -> dict:
    """Execute one queued task synchronously, streaming progress via SSE."""
    row = db.fetchone("SELECT * FROM worker_tasks WHERE id = ?", (task_id,))
    if not row:
        return {"ok": False, "error": "unknown task"}
    worker = WORKERS.get(row["worker"])
    if worker is None:
        return {"ok": False, "error": f"unknown worker: {row['worker']}"}
    try:
        params = json.loads(row["params"] or "{}")
    except Exception:
        params = {}

    # Atomically claim the task so the API's immediate-run thread and the
    # background daemon can never execute the same pending task twice.
    with db.transaction() as conn:
        claimed = conn.execute(
            "UPDATE worker_tasks SET status = ?, started_at = ? "
            "WHERE id = ? AND status = ?",
            (RUNNING, _now(), task_id, PENDING),
        ).rowcount == 1
    if not claimed:
        return {"ok": False, "error": "already claimed"}

    def progress(fraction: float, message: str = "") -> None:
        frac = max(0.0, min(1.0, float(fraction)))
        db.execute("UPDATE worker_tasks SET progress = ? WHERE id = ?",
                   (frac, task_id))
        db.commit()
        sse_events.publish("worker_progress", {
            "task_id": task_id, "worker": row["worker"], "status": RUNNING,
            "progress": frac, "message": message,
        })

    try:
        result = worker.run(params, progress)
        db.execute(
            "UPDATE worker_tasks SET status = ?, result = ?, progress = 1.0, "
            "finished_at = ? WHERE id = ?",
            (DONE, json.dumps(result), _now(), task_id))
        db.commit()
        sse_events.publish("worker_progress", {
            "task_id": task_id, "worker": row["worker"], "status": DONE,
            "progress": 1.0, "message": "done", "result": result,
        })
        return {"ok": True, "result": result}
    except Exception as exc:
        log.warning("worker %s task %s failed: %s", row["worker"], task_id, exc)
        db.execute(
            "UPDATE worker_tasks SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (ERROR, str(exc), _now(), task_id))
        db.commit()
        sse_events.publish("worker_progress", {
            "task_id": task_id, "worker": row["worker"], "status": ERROR,
            "message": str(exc),
        })
        return {"ok": False, "error": str(exc)}


__all__ = [
    "Worker", "WORKERS", "list_workers", "enqueue", "get_task", "list_tasks",
    "run_task", "PENDING", "RUNNING", "DONE", "ERROR",
]
