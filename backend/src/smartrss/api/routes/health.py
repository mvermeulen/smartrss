"""Unauthenticated liveness/readiness endpoint (Host-exempt)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine

from smartrss import __version__
from smartrss.api.deps import get_engine
from smartrss.api.schemas import HealthResponse
from smartrss.db.engine import check_db_health

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(
    response: Response,
    engine: AsyncEngine = Depends(get_engine),
) -> HealthResponse:
    db_ok = await check_db_health(engine)
    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="degraded", db="error", version=__version__)
    return HealthResponse(status="ok", db="ok", version=__version__)
