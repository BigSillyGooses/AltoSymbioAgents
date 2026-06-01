"""Web-research routes — wrap core/api/web.WebAPI."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ._helpers import get_api

router = APIRouter()


class FetchIn(BaseModel):
    url: str
    use_stealth: bool = False


class FetchToRagIn(BaseModel):
    url: str
    source: str = ""
    use_stealth: bool = False


@router.post("/fetch")
async def fetch(body: FetchIn, request: Request) -> dict:
    return await get_api(request).web_fetch(body.url, body.use_stealth)


@router.post("/fetch_to_rag")
async def fetch_to_rag(body: FetchToRagIn, request: Request) -> dict:
    return await get_api(request).web_fetch_to_rag(
        body.url, body.source, body.use_stealth,
    )


@router.get("/status")
async def status(request: Request) -> dict:
    return get_api(request).web_status()
