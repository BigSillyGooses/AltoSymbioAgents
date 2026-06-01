"""Workflows routes — wrap core/api/workflows.WorkflowsAPI."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ._helpers import get_api

router = APIRouter()


class TaskIn(BaseModel):
    name: str
    agent_role: str = "assistant"
    prompt: str = ""
    depends_on: list[str] = Field(default_factory=list)
    condition: Optional[dict] = None
    max_attempts: int = 1


class CreateWorkflowIn(BaseModel):
    name: str
    tasks: list[TaskIn]


class FromTemplateIn(BaseModel):
    template_id: str
    input: str
    run: bool = False


@router.get("/list")
async def list_workflows(request: Request, limit: int = 50) -> dict:
    return get_api(request).workflows_list(limit)


@router.get("/templates")
async def list_templates(request: Request) -> dict:
    return get_api(request).workflows_templates()


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request) -> dict:
    return get_api(request).workflows_get(workflow_id)


@router.post("/create")
async def create_workflow(body: CreateWorkflowIn, request: Request) -> dict:
    tasks: list[dict[str, Any]] = [t.model_dump() for t in body.tasks]
    return get_api(request).workflows_create(body.name, tasks)


@router.post("/from_template")
async def from_template(body: FromTemplateIn, request: Request) -> dict:
    return get_api(request).workflows_from_template(body.template_id, body.input, body.run)


@router.post("/{workflow_id}/run")
async def run_workflow(workflow_id: str, request: Request) -> dict:
    return get_api(request).workflows_run(workflow_id)


@router.post("/{workflow_id}/resume")
async def resume_workflow(workflow_id: str, request: Request) -> dict:
    return get_api(request).workflows_resume(workflow_id)
