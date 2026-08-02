"""Health & system control router."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from qtrader.config.container import Container
from qtrader.config.settings import Settings
from qtrader.domain.ports import EventRepository
from qtrader.domain.value_objects import TradingMode
from qtrader.interfaces.api.dependencies import (
    get_container,
    get_event_repository,
    get_settings,
    require_api_key,
)
from qtrader.interfaces.api.schemas import HealthCheck, ModeToggle, SystemStatus

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
    from qtrader.application.agents.registry import default_registry

    return SystemStatus(
        mode=settings.qtrader_mode.value,
        live_enabled=settings.live_enabled,
        agents=default_registry().names,
    )


@router.post(
    "/system/mode",
    response_model=SystemStatus,
    dependencies=[Depends(require_api_key)],
)
async def toggle_mode(
    body: ModeToggle,
    settings: Settings = Depends(get_settings),
) -> SystemStatus:
    try:
        mode = TradingMode(body.mode)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid mode") from None
    if mode is TradingMode.LIVE and not settings.live_enabled:
        raise HTTPException(
            status_code=403,
            detail="live mode requires ENABLE_LIVE_TRADING=true",
        )
    settings.qtrader_mode = mode
    return SystemStatus(mode=settings.qtrader_mode.value, live_enabled=settings.live_enabled)


@router.get(
    "/system/events",
    dependencies=[Depends(require_api_key)],
)
async def list_events(
    event_type: str | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 50,
    event_repo: EventRepository = Depends(get_event_repository),
) -> list[dict]:
    events = await event_repo.list_after(None, event_type, limit)
    if from_ is not None:
        events = [e for e in events if e.occurred_at >= from_]
    if to is not None:
        events = [e for e in events if e.occurred_at <= to]
    return [
        {
            "type": e.type_name,
            "uuid": e.event_uuid,
            "occurred_at": e.occurred_at,
            "payload": e.payload(),
        }
        for e in events
    ]
