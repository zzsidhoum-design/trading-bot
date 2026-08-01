"""Health & system control router."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from qtrader.config.container import Container
from qtrader.config.settings import Settings
from qtrader.domain.ports import EventRepository
from qtrader.interfaces.api.dependencies import (
    get_container,
    get_event_repository,
    get_settings,
    require_api_key,
)
from qtrader.interfaces.api.schemas import HealthCheck, SystemStatus

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/health", response_model=HealthCheck, dependencies=[Depends(require_api_key)])
async def health(
    container: Container = Depends(get_container),
    settings: Settings = Depends(get_settings),
) -> HealthCheck:
    return HealthCheck(
        database="ok" if await container.database_healthy() else "down",
        cache="ok" if await container.cache_healthy() else "down",
        mode=settings.qtrader_mode.value,
    )


@router.get("/system/status", response_model=SystemStatus, dependencies=[Depends(require_api_key)])
async def system_status(settings: Settings = Depends(get_settings)) -> SystemStatus:
    return SystemStatus(mode=settings.qtrader_mode.value, live_enabled=settings.live_enabled)


@router.get(
    "/system/events",
    dependencies=[Depends(require_api_key)],
)
async def list_events(
    event_type: str | None = None,
    limit: int = 50,
    event_repo: EventRepository = Depends(get_event_repository),
) -> list[dict]:
    events = await event_repo.list_after(None, event_type, limit)
    return [
        {
            "type": e.type_name,
            "uuid": e.event_uuid,
            "occurred_at": e.occurred_at,
            "payload": e.payload(),
        }
        for e in events
    ]
