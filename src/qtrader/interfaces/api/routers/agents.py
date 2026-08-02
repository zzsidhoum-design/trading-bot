"""Agent control router — list agents and run one on demand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from qtrader.application.agents.base import AgentContext
from qtrader.application.agents.registry import default_registry
from qtrader.config.settings import Settings
from qtrader.domain.value_objects import Interval
from qtrader.interfaces.api.dependencies import get_container, get_settings, require_api_key
from qtrader.interfaces.api.schemas import AgentRunRequest, AgentRunResult

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


@router.get(
    "",
    dependencies=[Depends(require_api_key)],
)
async def list_agents(settings: Settings = Depends(get_settings)) -> list[dict]:
    registry = default_registry()
    return [
        {
            "name": name,
            "available": True,
            "mode": settings.qtrader_mode.value,
            "gate_strategy": settings.gate_strategy,
        }
        for name in registry.names
    ]


@router.post(
    "/{name}/run",
    response_model=AgentRunResult,
    dependencies=[Depends(require_api_key)],
)
async def run_agent(
    name: str,
    body: AgentRunRequest = AgentRunRequest(),
    settings: Settings = Depends(get_settings),
    container: Any = Depends(get_container),
) -> AgentRunResult:
    registry = default_registry()
    cls = registry.get(name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {name!r}")
    end = datetime.now(UTC)
    start = end - timedelta(days=body.days)
    ctx = AgentContext(
        symbol=body.symbol.upper(),
        interval=Interval(body.interval),
        start=start,
        end=end,
    )
    try:
        instance = container.resolve(cls)
        await instance.run(ctx)
    except Exception as exc:  # noqa: BLE001
        return AgentRunResult(agent=name, status="error", detail=str(exc))
    return AgentRunResult(agent=name, status="ok", detail=f"ran {name} over {body.days}d window")
