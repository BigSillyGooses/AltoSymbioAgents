"""
core/api/workflows.py — API surface for explicit workflows.

Facade over ``services.workflow_engine`` + ``services.workflow_templates``.
Workflow runs make multiple model calls, so ``run``/``resume`` execute in a
background thread and stream progress over SSE; the HTTP call returns
immediately with the workflow id.
"""

from __future__ import annotations

import logging
import threading

from services import workflow_templates
from services.workflow_engine import WorkflowEngine, WorkflowError

from ._base import BaseAPI

log = logging.getLogger("altosybioagents.api.workflows")


class WorkflowsAPI(BaseAPI):

    def _engine(self) -> WorkflowEngine:
        chat = getattr(self, "_chat", None)
        hub = getattr(chat, "hub_router", None) if chat else None
        if hub is None:
            raise WorkflowError("workflow engine unavailable: hub router not ready")
        return WorkflowEngine(hub, self._settings)

    # ── Reads ────────────────────────────────────────────────────────────────

    def workflows_list(self, limit: int = 50) -> dict:
        return {"workflows": self._engine().list_workflows(limit)}

    def workflows_get(self, workflow_id: str) -> dict:
        wf = self._engine().get_workflow(workflow_id)
        return wf or {"error": "unknown workflow"}

    def workflows_templates(self) -> dict:
        return {"templates": workflow_templates.list_templates()}

    # ── Mutations ────────────────────────────────────────────────────────────

    def workflows_create(self, name: str, tasks: list) -> dict:
        try:
            wf_id = self._engine().create_workflow(name, tasks)
            return {"ok": True, "workflow_id": wf_id}
        except WorkflowError as exc:
            return {"ok": False, "error": str(exc)}

    def workflows_from_template(
        self, template_id: str, input_text: str, run: bool = False
    ) -> dict:
        try:
            name, tasks = workflow_templates.instantiate(
                template_id, {"input": input_text})
            wf_id = self._engine().create_workflow(name, tasks)
        except (ValueError, WorkflowError) as exc:
            return {"ok": False, "error": str(exc)}
        if run:
            self._run_async(wf_id, resume=False)
        return {"ok": True, "workflow_id": wf_id}

    def workflows_run(self, workflow_id: str) -> dict:
        return self._run_async(workflow_id, resume=False)

    def workflows_resume(self, workflow_id: str) -> dict:
        return self._run_async(workflow_id, resume=True)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _run_async(self, workflow_id: str, *, resume: bool) -> dict:
        try:
            engine = self._engine()
        except WorkflowError as exc:
            return {"ok": False, "error": str(exc)}

        def _go():
            try:
                engine.run(workflow_id, resume=resume)
            except Exception as exc:
                log.warning("workflow %s run failed: %s", workflow_id, exc)

        threading.Thread(target=_go, daemon=True,
                         name=f"workflow-{workflow_id[:8]}").start()
        return {"ok": True, "workflow_id": workflow_id, "status": "running"}
