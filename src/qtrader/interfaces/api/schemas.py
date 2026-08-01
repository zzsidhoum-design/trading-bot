"""API response schemas (Pydantic). Thin DTOs — never ORM models or entities."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HealthCheck(BaseModel):
    status: str = "ok"
    database: str
    cache: str
    broker: str = "not_configured"
    mode: str


class SystemStatus(BaseModel):
    mode: str
    live_enabled: bool
    api_version: str = "v1"
    agents: list[dict] = Field(default_factory=list)


class StockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    exchange: str
    name: str | None = None
    currency: str = "USD"
    sector: str | None = None
    industry: str | None = None
    is_active: bool = True


class PriceBarOut(BaseModel):
    symbol: str
    interval: str
    ts: datetime
    open: str
    high: str
    low: str
    close: str
    volume: str


class PortfolioSummary(BaseModel):
    name: str
    currency: str
    mode: str
    status: str
    initial_capital: str
    current_cash: str


class OrderOut(BaseModel):
    order_id: int | None = None
    idempotency_key: str
    side: str
    order_type: str
    quantity: str
    status: str
    mode: str
    created_at: datetime
    decision_ref: str | None = None
