"""Health & system control router."""

from __future__ import annotations

import time
from contextlib import suppress
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from qtrader.config.container import Container
from qtrader.config.settings import Settings
from qtrader.domain.exceptions import ValidationError
from qtrader.domain.ports import EventRepository, SystemLogRepository
from qtrader.domain.value_objects import TradingMode
from qtrader.interfaces.api.dependencies import (
    EnqueueJob,
    get_container,
    get_enqueue_job,
    get_event_repository,
    get_settings,
    get_system_log_repository,
    require_api_key,
)
from qtrader.interfaces.api.schemas import (
    CircuitBreakerSnapshot,
    HealthCheck,
    ModeToggle,
    SystemLogOut,
    SystemMetrics,
    SystemStatus,
)

router = APIRouter(prefix="/api/v1", tags=["system"])

_PROCESS_START = time.monotonic()


@router.get("/health", response_model=HealthCheck, dependencies=[Depends(require_api_key)])
async def health(
    container: Container = Depends(get_container),
    settings: Settings = Depends(get_settings),
) -> HealthCheck:
    return HealthCheck(
        database="ok" if await container.database_healthy() else "down",
        cache="ok" if await container.cache_healthy() else "down",
        worker="ok" if await container.worker_healthy() else "down",
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


@router.get(
    "/system/resilience",
    response_model=list[CircuitBreakerSnapshot],
    dependencies=[Depends(require_api_key)],
)
async def resilience(container: Container = Depends(get_container)) -> list[CircuitBreakerSnapshot]:
    return [CircuitBreakerSnapshot.model_validate(s) for s in container.circuit_breakers()]


@router.get(
    "/system/metrics",
    response_model=SystemMetrics,
    dependencies=[Depends(require_api_key)],
)
async def system_metrics(
    container: Container = Depends(get_container),
    settings: Settings = Depends(get_settings),
    event_repo: EventRepository = Depends(get_event_repository),
) -> SystemMetrics:
    """Process-level metrics snapshot for monitoring/alerting."""
    events_by_type: dict[str, int] = {}
    with suppress(Exception):
        events_by_type = await event_repo.count_by_type(1000)
    return SystemMetrics(
        uptime_seconds=time.monotonic() - _PROCESS_START,
        mode=settings.qtrader_mode.value,
        live_enabled=settings.live_enabled,
        database="ok" if await container.database_healthy() else "down",
        cache="ok" if await container.cache_healthy() else "down",
        worker="ok" if await container.worker_healthy() else "down",
        events_by_type=events_by_type,
        circuit_breakers=[
            CircuitBreakerSnapshot.model_validate(s) for s in container.circuit_breakers()
        ],
    )


@router.get(
    "/system/logs",
    response_model=list[SystemLogOut],
    dependencies=[Depends(require_api_key)],
)
async def system_logs(
    level: str | None = None,
    component: str | None = None,
    limit: int = 50,
    logs: SystemLogRepository = Depends(get_system_log_repository),
) -> list[SystemLogOut]:
    """Recent audit/journal entries (gate decisions, backtest runs)."""
    entries = await logs.recent(level, component, limit)
    return [
        SystemLogOut(
            log_id=log.log_id,
            level=log.level,
            component=log.component,
            message=log.message,
            context=log.context,
            created_at=log.created_at,
        )
        for log in entries
    ]


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
        raise ValidationError("invalid mode") from None
    if mode is TradingMode.LIVE and not settings.live_enabled:
        raise ValidationError("live mode requires ENABLE_LIVE_TRADING=true")
    settings.qtrader_mode = mode
    return SystemStatus(mode=settings.qtrader_mode.value, live_enabled=settings.live_enabled)


_SAFE_CYCLES = {"backfill", "scan_cycle", "execute_cycle", "train_cycle", "backtest_cycle"}


class RunCycle(BaseModel):
    mode: Literal["backfill", "scan_cycle", "execute_cycle", "train_cycle", "backtest_cycle"] = (
        "scan_cycle"
    )
    job_id: str | None = None


@router.post(
    "/system/run",
    response_model=RunCycle,
    dependencies=[Depends(require_api_key)],
)
async def run_cycle(
    body: RunCycle,
    settings: Settings = Depends(get_settings),
    enqueue: EnqueueJob = Depends(get_enqueue_job),
) -> RunCycle:
    """Enqueue a worker cycle to run now (rather than on its cron schedule)."""
    if body.mode not in _SAFE_CYCLES:
        raise ValidationError(f"unsafe cycle: {body.mode}")
    job_id = await enqueue(body.mode)
    if job_id is None:
        raise ValidationError("worker queue unavailable")
    return RunCycle(mode=body.mode, job_id=job_id)


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
