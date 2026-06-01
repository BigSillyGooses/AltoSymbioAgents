"""
services/workflow_templates.py — methodology workflow templates.

Loads the JSON workflow definitions shipped under
``core/templates/workflows/`` (SPARC, DDD, ADR, …) and instantiates them into
concrete task lists consumable by ``WorkflowEngine.create_workflow``.

Templates are plain data: a name/description plus a list of tasks (each with
name, agent_role, prompt, optional depends_on and condition). Instantiation
substitutes ``{{input}}`` (and any other ``{{var}}``) placeholders in the task
prompts with caller-provided inputs, so a single template drives many runs.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("alto.workflow_templates")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "core" / "templates" / "workflows"
_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _template_files() -> list[Path]:
    if not _TEMPLATE_DIR.is_dir():
        return []
    return sorted(_TEMPLATE_DIR.glob("*.json"))


def list_templates() -> list[dict]:
    """Return the available templates (id, name, description, step names)."""
    out: list[dict] = []
    for f in _template_files():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("skipping bad template %s: %s", f.name, exc)
            continue
        out.append({
            "id": data.get("id", f.stem),
            "name": data.get("name", f.stem),
            "description": data.get("description", ""),
            "steps": [t.get("name", "") for t in data.get("tasks", [])],
        })
    return out


def get_template(template_id: str) -> dict | None:
    for f in _template_files():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("id", f.stem) == template_id:
            return data
    return None


def _substitute(text: str, inputs: dict) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        return str(inputs.get(key, inputs.get("input", m.group(0))))
    return _VAR_RE.sub(repl, text or "")


def instantiate(template_id: str, inputs: dict) -> tuple[str, list[dict]]:
    """Return ``(workflow_name, tasks)`` for a template with vars substituted.

    ``inputs`` must at least contain ``input`` (the topic/task text). Any
    ``{{var}}`` in a task prompt is replaced by ``inputs[var]`` (falling back
    to ``inputs['input']``).
    """
    data = get_template(template_id)
    if data is None:
        raise ValueError(f"unknown workflow template: {template_id}")
    tasks: list[dict] = []
    for t in data.get("tasks", []):
        tasks.append({
            "name": t.get("name"),
            "agent_role": t.get("agent_role", "assistant"),
            "depends_on": list(t.get("depends_on") or []),
            "condition": t.get("condition"),
            "prompt": _substitute(t.get("prompt", ""), inputs),
            "max_attempts": int(t.get("max_attempts", 1)),
        })
    topic = (inputs.get("input") or "").strip()
    name = data.get("name", template_id)
    workflow_name = f"{name}: {topic[:60]}" if topic else name
    return workflow_name, tasks
