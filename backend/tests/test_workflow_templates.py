"""
tests/test_workflow_templates.py — methodology templates + instantiation.
"""

from __future__ import annotations

import pytest


def test_list_templates_present():
    from services import workflow_templates
    ids = {t["id"] for t in workflow_templates.list_templates()}
    assert {"sparc", "ddd", "adr"} <= ids
    sparc = next(t for t in workflow_templates.list_templates() if t["id"] == "sparc")
    assert "specification" in sparc["steps"]


def test_get_template_unknown():
    from services import workflow_templates
    assert workflow_templates.get_template("nope") is None


def test_instantiate_substitutes_input():
    from services import workflow_templates
    name, tasks = workflow_templates.instantiate(
        "sparc", {"input": "build a URL shortener"})
    assert "URL shortener" in name
    spec = next(t for t in tasks if t["name"] == "specification")
    assert "build a URL shortener" in spec["prompt"]
    assert "{{input}}" not in spec["prompt"]


def test_instantiate_preserves_dependencies_and_conditions():
    from services import workflow_templates
    _, tasks = workflow_templates.instantiate("sparc", {"input": "x"})
    by_name = {t["name"]: t for t in tasks}
    assert by_name["pseudocode"]["depends_on"] == ["spec_review"]
    assert by_name["pseudocode"]["condition"]["when"]["task"] == "spec_review"


def test_instantiate_unknown_raises():
    from services import workflow_templates
    with pytest.raises(ValueError):
        workflow_templates.instantiate("missing", {"input": "x"})


def test_template_instantiates_into_runnable_workflow(in_memory_db, settings):
    """A template must produce a valid, topologically-sortable workflow."""
    from services import workflow_templates
    from services.workflow_engine import WorkflowEngine

    name, tasks = workflow_templates.instantiate("adr", {"input": "pick a database"})
    eng = WorkflowEngine(hub_router=None, settings=settings)
    wf_id = eng.create_workflow(name, tasks)
    wf = eng.get_workflow(wf_id)
    assert wf is not None
    assert len(wf["tasks"]) == len(tasks)
    # Topological order must succeed (no cycles).
    order = eng._topo_order([dict(t) for t in __import__("db").fetchall(
        "SELECT * FROM tasks WHERE workflow_id = ?", (wf_id,))])
    assert len(order) == len(tasks)
