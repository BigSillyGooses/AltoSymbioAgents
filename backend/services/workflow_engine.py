"""
services/workflow_engine.py — explicit user-defined workflow execution.

Alto already ships an *implicit* team pipeline (services/pipeline.py) that
decomposes a single message into sub-tasks and runs them through a saga of
``workflow_checkpoints``. This module adds the *explicit* counterpart: a
persisted, user-defined DAG of tasks (the ``workflows`` + ``tasks`` tables)
that can be created, run, watched, branched, and resumed.

It deliberately reuses existing infrastructure rather than duplicating it:
  * ``workflows`` / ``tasks`` tables (tasks.depends_on is the DAG)
  * ``workflow_checkpoints`` saga rows (provisional → committed / rolled_back)
  * ``HubRouter.invoke`` as the single model-call boundary
  * ``sse_events.publish`` for live progress

Conditional edges
------------------
A task may carry a ``condition`` in its ``input_data`` JSON::

    {"when": {"task": "review", "contains": "PASS"}}
    {"when": {"task": "review", "not_contains": "FAIL"}}

The task runs only if the referenced upstream task's output satisfies the
predicate; otherwise it is marked ``skipped`` (and its own dependents see no
output from it). This is what powers methodology quality-gates (e.g. SPARC's
"only continue if the spec review passed").
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import db
import sse_events
from models import RoutingDecision
from services import prompt_library

log = logging.getLogger("alto.workflow_engine")

# Task lifecycle states (stored in tasks.status).
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"

# Checkpoint states (must match services/pipeline.py constants).
CK_PROVISIONAL = "provisional"
CK_COMMITTED = "committed"
CK_ROLLED_BACK = "rolled_back"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowError(Exception):
    pass


class WorkflowEngine:
    """Create, run, and resume explicit workflow DAGs."""

    def __init__(self, hub_router, settings, on_event: Optional[Callable] = None):
        self._hub = hub_router
        self._settings = settings
        # on_event(event_name, payload) — defaults to SSE publish.
        self._on_event = on_event or (lambda e, p: sse_events.publish(e, p))

    # ── Creation ─────────────────────────────────────────────────────────────

    def create_workflow(
        self, name: str, tasks: list[dict], workflow_id: Optional[str] = None
    ) -> str:
        """Persist a workflow and its task DAG.

        ``tasks`` is a list of dicts with keys:
          name (str), agent_role (str), prompt (str),
          depends_on (list[str] of task *names*), condition (dict | None).

        depends_on names are resolved to task ids before storage so the
        ``tasks.depends_on`` column holds a JSON list of ids (matching the
        existing schema contract).
        """
        if not tasks:
            raise WorkflowError("a workflow needs at least one task")
        wf_id = workflow_id or str(uuid.uuid4())
        now = _now()

        # Normalise names and pre-allocate ids so depends_on (declared by task
        # name) can be resolved to task ids before insertion.
        norm: list[dict] = []
        name_to_id: dict[str, str] = {}
        for i, t in enumerate(tasks):
            tname = (t.get("name") or f"step_{i + 1}").strip()
            if tname in name_to_id:
                raise WorkflowError(f"duplicate task name: {tname}")
            name_to_id[tname] = str(uuid.uuid4())
            norm.append({**t, "name": tname})

        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO workflows (id, name, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (wf_id, name, PENDING, now, now),
            )
            for t in norm:
                tname = t["name"]
                dep_ids = [name_to_id[d] for d in (t.get("depends_on") or [])
                           if d in name_to_id]
                input_data = {
                    "prompt": t.get("prompt", ""),
                    "condition": t.get("condition"),
                    "task_name": tname,
                }
                conn.execute(
                    "INSERT INTO tasks (id, workflow_id, name, agent_role, status, "
                    "depends_on, input_data, output_data, attempt_count, "
                    "max_attempts, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, '{}', 0, ?, ?, ?)",
                    (
                        name_to_id[tname], wf_id, tname,
                        t.get("agent_role", "assistant"),
                        PENDING, json.dumps(dep_ids), json.dumps(input_data),
                        int(t.get("max_attempts", 1)), now, now,
                    ),
                )
        return wf_id

    # ── Inspection ───────────────────────────────────────────────────────────

    def get_workflow(self, workflow_id: str) -> Optional[dict]:
        wf = db.fetchone("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
        if not wf:
            return None
        tasks = db.fetchall(
            "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at",
            (workflow_id,),
        )
        checkpoints = db.fetchall(
            "SELECT * FROM workflow_checkpoints WHERE workflow_id = ? "
            "ORDER BY step_index",
            (workflow_id,),
        )
        return {
            "id": wf["id"],
            "name": wf["name"],
            "status": wf["status"],
            "created_at": wf["created_at"],
            "updated_at": wf["updated_at"],
            "tasks": [self._task_view(t) for t in tasks],
            "checkpoints": [dict(c) for c in checkpoints],
        }

    def list_workflows(self, limit: int = 50) -> list[dict]:
        rows = db.fetchall(
            "SELECT id, name, status, created_at, updated_at FROM workflows "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    @staticmethod
    def _task_view(t) -> dict:
        try:
            input_data = json.loads(t["input_data"] or "{}")
        except Exception:
            input_data = {}
        try:
            output_data = json.loads(t["output_data"] or "{}")
        except Exception:
            output_data = {}
        try:
            depends_on = json.loads(t["depends_on"] or "[]")
        except Exception:
            depends_on = []
        return {
            "id": t["id"],
            "name": t["name"],
            "agent_role": t["agent_role"],
            "status": t["status"],
            "depends_on": depends_on,
            "condition": input_data.get("condition"),
            "prompt": input_data.get("prompt", ""),
            "output": output_data.get("text", ""),
            "error": t["error_message"],
        }

    # ── Execution ────────────────────────────────────────────────────────────

    def run(self, workflow_id: str, *, resume: bool = False) -> dict:
        """Execute (or resume) a workflow to completion. Returns final state."""
        wf = db.fetchone("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
        if not wf:
            raise WorkflowError(f"unknown workflow: {workflow_id}")

        tasks = [dict(t) for t in db.fetchall(
            "SELECT * FROM tasks WHERE workflow_id = ?", (workflow_id,))]
        order = self._topo_order(tasks)
        by_id = {t["id"]: t for t in tasks}

        self._set_workflow_status(workflow_id, RUNNING)
        self._emit("workflow_started", {"workflow_id": workflow_id,
                                        "name": wf["name"], "resume": resume})

        # Context: task_id -> output text (seed from already-completed tasks).
        context: dict[str, str] = {}
        for t in tasks:
            if t["status"] == COMPLETED:
                context[t["id"]] = self._output_text(t)

        failed = False
        for step_index, task_id in enumerate(order):
            task = by_id[task_id]
            if resume and task["status"] == COMPLETED:
                self._emit("workflow_step", {
                    "workflow_id": workflow_id, "task_id": task_id,
                    "name": task["name"], "state": COMPLETED, "skipped": True,
                })
                continue

            # Conditional edge evaluation.
            cond = self._condition(task)
            if cond is not None and not self._eval_condition(cond, by_id, context):
                self._set_task_status(task_id, SKIPPED)
                self._emit("workflow_step", {
                    "workflow_id": workflow_id, "task_id": task_id,
                    "name": task["name"], "state": SKIPPED,
                })
                continue

            ck_id = self._open_checkpoint(workflow_id, step_index, task)
            self._set_task_status(task_id, RUNNING)
            self._emit("workflow_step", {
                "workflow_id": workflow_id, "task_id": task_id,
                "name": task["name"], "state": RUNNING})

            try:
                output = self._run_task(task, by_id, context)
                context[task_id] = output
                self._store_output(task_id, output)
                self._set_task_status(task_id, COMPLETED)
                self._commit_checkpoint(ck_id, output)
                self._emit("workflow_step", {
                    "workflow_id": workflow_id, "task_id": task_id,
                    "name": task["name"], "state": COMPLETED,
                    "output_preview": output[:200]})
            except Exception as exc:
                log.warning("workflow %s task %s failed: %s",
                            workflow_id, task_id, exc)
                self._set_task_status(task_id, FAILED, error=str(exc))
                self._rollback_checkpoint(ck_id, str(exc))
                self._emit("workflow_step", {
                    "workflow_id": workflow_id, "task_id": task_id,
                    "name": task["name"], "state": FAILED, "error": str(exc)})
                failed = True
                break

        final_status = FAILED if failed else COMPLETED
        self._set_workflow_status(workflow_id, final_status)
        self._emit("workflow_finished", {
            "workflow_id": workflow_id, "status": final_status})
        return self.get_workflow(workflow_id)

    def resume(self, workflow_id: str) -> dict:
        return self.run(workflow_id, resume=True)

    # ── Task execution ───────────────────────────────────────────────────────

    def _run_task(self, task: dict, by_id: dict, context: dict) -> str:
        """Dispatch one task through HubRouter.invoke and return its output."""
        input_data = self._input_data(task)
        prompt = input_data.get("prompt", "") or f"Complete the task: {task['name']}"
        agent_role = task.get("agent_role") or "assistant"

        # System prompt from the role (falls back to the seeded default).
        try:
            system = prompt_library.get_active_prompt(agent_role)
        except Exception:
            system = "You are a helpful AI assistant."

        # Compose upstream outputs into the user message so each step sees the
        # artifacts of its dependencies.
        upstream = self._upstream_context(task, by_id, context)
        user_content = prompt
        if upstream:
            user_content = f"{prompt}\n\n## Inputs from previous steps\n{upstream}"

        decision = self._build_decision(agent_role)
        worker = self._hub.invoke(
            decision, system,
            [{"role": "user", "content": user_content}],
            max_tokens=int(self._settings.get("workflow_max_tokens", 2048) or 2048),
            agent_role="workflow",
        )
        text = (getattr(worker, "text", "") or "").strip()
        if getattr(worker, "had_error", False) or not text:
            raise WorkflowError(text or "worker produced no output")
        return text

    def _build_decision(self, agent_role: str) -> RoutingDecision:
        """Hub-direct decision: Claude when a key is configured, else local."""
        has_claude = bool((self._settings.get("claude_api_key", "") or "").strip())
        backend = "claude" if has_claude else "local"
        return RoutingDecision(
            agent_id="", backend=backend, score=1.0,
            reasoning=f"workflow step ({agent_role})",
            used_fallback=False, skill_matched="",
        )

    def _upstream_context(self, task: dict, by_id: dict, context: dict) -> str:
        parts = []
        for dep_id in self._depends_on(task):
            dep = by_id.get(dep_id)
            if dep and dep_id in context:
                parts.append(f"### {dep['name']}\n{context[dep_id]}")
        return "\n\n".join(parts)

    # ── Conditions ───────────────────────────────────────────────────────────

    def _eval_condition(self, cond: dict, by_id: dict, context: dict) -> bool:
        """Evaluate a ``{"when": {...}}`` predicate against upstream output."""
        when = cond.get("when") if isinstance(cond, dict) else None
        if not isinstance(when, dict):
            return True
        ref_name = when.get("task")
        # Resolve referenced task by name → id → output text.
        ref_id = None
        for tid, t in by_id.items():
            if t["name"] == ref_name:
                ref_id = tid
                break
        if ref_id is None:
            return True  # unknown reference → don't block
        text = (context.get(ref_id) or "").lower()
        if "contains" in when:
            return str(when["contains"]).lower() in text
        if "not_contains" in when:
            return str(when["not_contains"]).lower() not in text
        if "equals" in when:
            return text.strip() == str(when["equals"]).lower().strip()
        return True

    # ── Topological ordering ─────────────────────────────────────────────────

    def _topo_order(self, tasks: list[dict]) -> list[str]:
        """Kahn's algorithm over depends_on. Raises on cycles."""
        ids = {t["id"] for t in tasks}
        deps: dict[str, set] = {}
        for t in tasks:
            deps[t["id"]] = {d for d in self._depends_on(t) if d in ids}
        order: list[str] = []
        # Stable: preserve creation order among ready nodes.
        ready = [t["id"] for t in tasks if not deps[t["id"]]]
        ready_set = set(ready)
        while ready:
            nid = ready.pop(0)
            ready_set.discard(nid)
            order.append(nid)
            for t in tasks:
                tid = t["id"]
                if tid in order or tid in ready_set:
                    continue
                if nid in deps[tid]:
                    deps[tid].discard(nid)
                if not deps[tid] and tid not in order:
                    ready.append(tid)
                    ready_set.add(tid)
        if len(order) != len(tasks):
            raise WorkflowError("workflow has a dependency cycle")
        return order

    # ── Small accessors ──────────────────────────────────────────────────────

    @staticmethod
    def _input_data(task: dict) -> dict:
        try:
            return json.loads(task.get("input_data") or "{}")
        except Exception:
            return {}

    def _condition(self, task: dict) -> Optional[dict]:
        return self._input_data(task).get("condition")

    @staticmethod
    def _depends_on(task: dict) -> list[str]:
        try:
            return json.loads(task.get("depends_on") or "[]")
        except Exception:
            return []

    @staticmethod
    def _output_text(task: dict) -> str:
        try:
            return json.loads(task.get("output_data") or "{}").get("text", "")
        except Exception:
            return ""

    # ── Persistence helpers ──────────────────────────────────────────────────

    def _set_workflow_status(self, workflow_id: str, status: str) -> None:
        db.execute("UPDATE workflows SET status = ?, updated_at = ? WHERE id = ?",
                   (status, _now(), workflow_id))
        db.commit()

    def _set_task_status(self, task_id: str, status: str, error: str = None) -> None:
        db.execute(
            "UPDATE tasks SET status = ?, error_message = ?, updated_at = ?, "
            "attempt_count = attempt_count + 1 WHERE id = ?",
            (status, error, _now(), task_id))
        db.commit()

    def _store_output(self, task_id: str, text: str) -> None:
        db.execute("UPDATE tasks SET output_data = ?, updated_at = ? WHERE id = ?",
                   (json.dumps({"text": text}), _now(), task_id))
        db.commit()

    def _open_checkpoint(self, workflow_id: str, step_index: int, task: dict) -> str:
        ck_id = str(uuid.uuid4())
        try:
            db.execute(
                "INSERT INTO workflow_checkpoints "
                "(checkpoint_id, workflow_id, step_index, task_id, agent_id, "
                " agent_name, state, success_criteria, retry_count, max_retries, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (ck_id, workflow_id, step_index, task["id"],
                 task.get("agent_role", ""), task["name"], CK_PROVISIONAL,
                 task["name"], int(task.get("max_attempts", 1)), _now()))
            db.commit()
        except Exception as exc:
            log.debug("checkpoint open failed (non-fatal): %s", exc)
            return ""
        return ck_id

    def _commit_checkpoint(self, ck_id: str, output: str) -> None:
        if not ck_id:
            return
        now = _now()
        try:
            db.execute(
                "UPDATE workflow_checkpoints SET state = ?, artifact_summary = ?, "
                "validation_passed = 1, validated_at = ?, committed_at = ? "
                "WHERE checkpoint_id = ?",
                (CK_COMMITTED, output[:500], now, now, ck_id))
            db.commit()
        except Exception as exc:
            log.debug("checkpoint commit failed (non-fatal): %s", exc)

    def _rollback_checkpoint(self, ck_id: str, reason: str) -> None:
        if not ck_id:
            return
        now = _now()
        try:
            db.execute(
                "UPDATE workflow_checkpoints SET state = ?, validation_passed = 0, "
                "failure_reason = ?, validated_at = ?, rolled_back_at = ? "
                "WHERE checkpoint_id = ?",
                (CK_ROLLED_BACK, reason[:500], now, now, ck_id))
            db.commit()
        except Exception as exc:
            log.debug("checkpoint rollback failed (non-fatal): %s", exc)

    def _emit(self, event: str, payload: dict) -> None:
        try:
            self._on_event(event, payload)
        except Exception as exc:
            log.debug("workflow emit failed: %s", exc)
