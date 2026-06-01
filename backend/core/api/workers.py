"""
core/api/workers.py — API surface for background workers.

Thin facade over ``services.workers``. Explicit "run now" requests are executed
in a background thread so the HTTP call returns immediately and progress
streams over SSE; autonomous/scheduled execution is handled by the
WorkerDaemon (spawned from the FastAPI lifespan).
"""

from __future__ import annotations

import logging
import threading

from services import workers as workers_svc

from ._base import BaseAPI

log = logging.getLogger("altosybioagents.api.workers")


class WorkersAPI(BaseAPI):

    def workers_list(self) -> dict:
        return {"workers": workers_svc.list_workers()}

    def workers_list_tasks(self, limit: int = 50) -> dict:
        return {"tasks": workers_svc.list_tasks(limit)}

    def workers_get_task(self, task_id: str) -> dict:
        task = workers_svc.get_task(task_id)
        return task or {"error": "unknown task"}

    def workers_run(self, worker: str, params: dict | None = None) -> dict:
        """Queue a worker task and run it immediately in the background."""
        queued = workers_svc.enqueue(worker, params or {})
        if not queued.get("ok"):
            return queued
        task_id = queued["task_id"]
        threading.Thread(
            target=workers_svc.run_task, args=(task_id,),
            daemon=True, name=f"worker-{worker}",
        ).start()
        return {"ok": True, "task_id": task_id}
