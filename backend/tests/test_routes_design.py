"""tests/test_routes_design.py — HTTP tests for /api/design routes.

Exercises the auth middleware + the typed-error envelope against the REAL
vendored catalog via FastAPI's TestClient.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.errors import install_error_handlers
from routes import design as design_routes
from server import BearerAuthMiddleware

TOKEN = "test-token-design"


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(BearerAuthMiddleware, expected_token=TOKEN)
    install_error_handlers(app)
    app.include_router(design_routes.router, prefix="/api/design")
    return TestClient(app)


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


class TestListSystems:
    def test_returns_catalog_without_bodies(self, client):
        resp = client.get("/api/design/systems", headers=_headers())
        assert resp.status_code == 200
        systems = resp.json()["systems"]
        assert len(systems) > 100
        linear = next(s for s in systems if s["id"] == "linear-app")
        assert linear["title"] == "Linear"
        assert linear["category"] == "Productivity & SaaS"
        assert "#5e6ad2" in linear["swatches"]
        # The list payload deliberately omits the heavy raw body.
        assert "body" not in linear

    def test_requires_auth(self, client):
        resp = client.get("/api/design/systems")
        assert resp.status_code == 401


class TestGetSystem:
    def test_returns_body_for_real_id(self, client):
        resp = client.get("/api/design/systems/linear-app", headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "linear-app"
        assert body["title"] == "Linear"
        assert body["body"].startswith("# Design System Inspired by Linear")

    def test_unknown_id_is_typed_404(self, client):
        resp = client.get("/api/design/systems/no-such-brand", headers=_headers())
        assert resp.status_code == 404
        assert resp.json()["error_type"] == "design_system_not_found"


class TestSkills:
    def test_list_skills(self, client):
        resp = client.get("/api/design/skills", headers=_headers())
        assert resp.status_code == 200
        skills = resp.json()["skills"]
        assert len(skills) > 40
        wp = next(s for s in skills if s["id"] == "web-prototype")
        assert wp["mode"] == "prototype"

    def test_get_skill_returns_body_and_assets(self, client):
        resp = client.get("/api/design/skills/web-prototype", headers=_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "web-prototype"
        assert body["body"].startswith("# Web Prototype Skill")
        assert "### assets/template.html" in body["assets"]

    def test_unknown_skill_is_typed_404(self, client):
        resp = client.get("/api/design/skills/no-such-skill", headers=_headers())
        assert resp.status_code == 404
        assert resp.json()["error_type"] == "design_skill_not_found"
