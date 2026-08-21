from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from apps.platform_api.app.api.schemas import HealthResponse
from python_packages.platform_infra.db import engine
from python_packages.platform_infra.redis import redis_client

router = APIRouter()


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok", service="deadlock-platform-api")


@router.get("/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    async with engine().connect() as connection:
        await connection.execute(text("SELECT 1"))
    client = redis_client()
    try:
        await client.ping()
    finally:
        await client.aclose()
    return HealthResponse(status="ok", service="deadlock-platform-api")
