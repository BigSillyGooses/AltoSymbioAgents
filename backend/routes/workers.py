"""Workers routes — wrap core/api/workers.WorkersAPI."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ._helpers import get_api

router = APIRouter()


class RunWorkerIn(BaseModel):
    worker: str
    params: dict = Field(default_factory=dict)


@router.get("/list")
async def list_workers(request: Request) -> dict:
    return get_api(request).workers_list()


@router.get("/tasks")
async def list_tasks(request: Request, limit: int = 50) -> dict:
    return get_api(request).workers_list_tasks(limit)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    return get_api(request).workers_get_task(task_id)


@router.post("/run")
async def run_worker(body: RunWorkerIn, request: Request) -> dict:
    return get_api(request).workers_run(body.worker, body.params)
