"""routes/design.py — Design Studio asset catalog.

Read-only endpoints over the vendored Open Design assets, mounted at
``/api/design``:

  GET /api/design/systems        → list design systems (id/title/category/
                                    summary/swatches/surface), no bodies
  GET /api/design/systems/{id}   → one system, including the raw DESIGN.md body

The listing intentionally omits the (large) Markdown ``body`` so the picker
fetch stays small; the detail route returns it for callers that want to show
the full token reference. Assets are loaded from ``paths.design_assets_dir()``;
the loader degrades to an empty list when the tree is absent.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel, Field

import db as _db
from core import paths
from core.errors import DomainError
from services import design_assets, design_skills

router = APIRouter()

# Bound the saved-artifact payload so the library can't be used to stash
# arbitrarily large blobs in the SQLite DB. A self-contained HTML page is
# comfortably under this; matches the spirit of prompt_templates' BODY_MAX.
_CONTENT_MAX = 500_000
_TITLE_MAX = 200


@router.get("/systems")
async def list_systems() -> dict:
    systems = design_assets.list_design_systems(paths.design_assets_dir())
    # Drop the heavy raw body from the list payload — the picker only needs
    # the descriptor; the body is available via the per-id detail route.
    summaries = [{k: v for k, v in s.items() if k != "body"} for s in systems]
    return {"systems": summaries}


@router.get("/systems/{system_id}")
async def get_system(system_id: str) -> dict:
    root = paths.design_assets_dir()
    body = design_assets.read_design_system(root, system_id)
    if body is None:
        raise DomainError.design_system_not_found(system_id)
    meta = next(
        (s for s in design_assets.list_design_systems(root) if s["id"] == system_id),
        None,
    )
    return {
        "id": system_id,
        "title": meta["title"] if meta else system_id,
        "category": meta["category"] if meta else "Uncategorized",
        "summary": meta["summary"] if meta else "",
        "swatches": meta["swatches"] if meta else [],
        "surface": meta["surface"] if meta else "web",
        "body": body,
    }


@router.get("/skills")
async def list_skills() -> dict:
    skills = design_skills.list_skills(paths.design_assets_dir())
    return {"skills": skills}


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str) -> dict:
    skill = design_skills.read_skill(paths.design_assets_dir(), skill_id)
    if skill is None:
        raise DomainError.design_skill_not_found(skill_id)
    return skill


# ── Saved artifact library (Phase 3) ─────────────────────────────────────────


class ArtifactSaveIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=_TITLE_MAX)
    identifier: str = Field("", max_length=_TITLE_MAX)
    content: str = Field(..., min_length=1, max_length=_CONTENT_MAX)
    design_system: str | None = None
    skill: str | None = None


def _artifact_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "identifier": row["identifier"],
        "design_system": row["design_system"],
        "skill": row["skill"],
        "created_at": row["created_at"],
    }


def _artifact_full(row: sqlite3.Row) -> dict:
    return {**_artifact_summary(row), "content": row["content"]}


def _fetch_artifact_or_404(artifact_id: str) -> sqlite3.Row:
    row = _db.fetchone(
        "SELECT id, title, identifier, content, design_system, skill, created_at "
        "FROM design_artifacts WHERE id = ?",
        (artifact_id,),
    )
    if row is None:
        raise DomainError.design_artifact_not_found(artifact_id)
    return row


@router.get("/artifacts")
async def list_artifacts() -> dict:
    # Summaries only (no heavy HTML body) — the gallery opens full content
    # lazily via the per-id route.
    rows = _db.fetchall(
        "SELECT id, title, identifier, content, design_system, skill, created_at "
        "FROM design_artifacts ORDER BY created_at DESC"
    )
    return {"artifacts": [_artifact_summary(r) for r in rows]}


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str) -> dict:
    return _artifact_full(_fetch_artifact_or_404(artifact_id))


@router.post("/artifacts")
async def save_artifact(body: ArtifactSaveIn) -> dict:
    artifact_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with _db.transaction() as conn:
        conn.execute(
            "INSERT INTO design_artifacts "
            "(id, title, identifier, content, design_system, skill, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                body.title.strip(),
                body.identifier.strip(),
                body.content,
                (body.design_system or "").strip() or None,
                (body.skill or "").strip() or None,
                now,
            ),
        )
    return _artifact_full(_fetch_artifact_or_404(artifact_id))


@router.delete("/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str) -> dict:
    _fetch_artifact_or_404(artifact_id)
    with _db.transaction() as conn:
        conn.execute("DELETE FROM design_artifacts WHERE id = ?", (artifact_id,))
    return {"ok": True, "id": artifact_id}
