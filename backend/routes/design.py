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

from fastapi import APIRouter

from core import paths
from core.errors import DomainError
from services import design_assets, design_skills

router = APIRouter()


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
