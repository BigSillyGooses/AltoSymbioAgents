"""
services/workers/daemon.py — background worker dispatcher.

A long-running asyncio task (spawned from the FastAPI lifespan) that polls the
``worker_tasks`` table for pending rows and runs them. Worker bodies are
synchronous and bounded, so each is executed in a thread via
``run_in_executor`` to keep the event loop responsive. The daemon is a no-op
while ``background_workers_enabled`` is off, and exits cleanly on cancellation.
"""

from __future__ import annotations

import asyncio
import logging

import db
from . import run_task, PENDING

log = logging.getLogger("alto.workers.daemon")


class WorkerDaemon:
    def __init__(self, settings, poll_seconds: float = 30.0):
        self._settings = settings
        self._default_poll = poll_seconds

    def _enabled(self) -> bool:
        try:
            return bool(self._settings.get("background_workers_enabled", False))
        except Exception:
            return False

    def _poll_interval(self) -> float:
        try:
            return float(self._settings.get("worker_poll_seconds", self._default_poll))
        except Exception:
            return self._default_poll

    async def run(self) -> None:
        log.info("WorkerDaemon started")
        try:
            while True:
                interval = self._poll_interval()
                if self._enabled():
                    try:
                        await self._drain_pending()
                    except Exception as exc:
                        log.warning("worker drain error: %s", exc)
                await asyncio.sleep(max(1.0, interval))
        except asyncio.CancelledError:
            log.info("WorkerDaemon stopping")
            raise

    async def _drain_pending(self) -> None:
        loop = asyncio.get_running_loop()
        while self._enabled():
            row = db.fetchone(
                "SELECT id FROM worker_tasks WHERE status = ? "
                "ORDER BY created_at LIMIT 1",
                (PENDING,),
            )
            if not row:
                return
            task_id = row["id"]
            # Run the (sync) worker body off the event loop.
            await loop.run_in_executor(None, run_task, task_id)
