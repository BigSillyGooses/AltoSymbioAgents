"""
tests/test_workers_workflows_routes.py — HTTP wiring for the workers and
workflows routers. Confirms the routes are registered, auth-gated, and
serialize the underlying service output.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import workers as workers_routes
from routes import workflows as workflows_routes
from server import BearerAuthMiddleware

TOKEN = "test-token-xyz"


@pytest.fixture
def client():
    from services import workers as workers_svc
    from services import workflow_templates

    fake_api = MagicMock()
    # Wire the facade delegators to the real, DB-free service calls.
    fake_api.workers_list.side_effect = lambda: {"workers": workers_svc.list_workers()}
    fake_api.workflows_templates.side_effect = lambda: {
        "templates": workflow_templates.list_templates()}
    fake_api.workers_run.side_effect = lambda worker, params: {
        "ok": worker in {w["name"] for w in workers_svc.list_workers()},
        "task_id": "fake"}

    container = MagicMock()
    container.api = fake_api

    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware, expected_token=TOKEN)
    app.state.container = container
    app.include_router(workers_routes.router, prefix="/api/workers")
    app.include_router(workflows_routes.router, prefix="/api/workflows")
    return TestClient(app)


def _h():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_workers_list_route(client):
    r = client.get("/api/workers/list", headers=_h())
    assert r.status_code == 200
    names = {w["name"] for w in r.json()["workers"]}
    assert {"reindex", "memory_audit", "trajectory_report"} <= names


def test_workers_list_requires_auth(client):
    assert client.get("/api/workers/list").status_code == 401


def test_workers_run_validates_body(client):
    # Missing required 'worker' field → 422.
    assert client.post("/api/workers/run", json={}, headers=_h()).status_code == 422
    ok = client.post("/api/workers/run", json={"worker": "reindex"}, headers=_h())
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_workflows_templates_route(client):
    r = client.get("/api/workflows/templates", headers=_h())
    assert r.status_code == 200
    ids = {t["id"] for t in r.json()["templates"]}
    assert {"sparc", "ddd", "adr"} <= ids
