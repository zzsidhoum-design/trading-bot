"""Metrics, model registry, backtest runs, event outbox & system logs."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qtrader.infrastructure.database.base import Base, utcnow


class AgentMetricModel(Base):
    __tablename__ = "agent_metrics"
    __table_args__ = (
        Index("ix_agent_metrics_name_metric_time", "agent_name", "metric_name", "computed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(32), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    window: Mapped[str] = mapped_column(String(16), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StrategyPerformanceModel(Base):
    __tablename__ = "strategy_performance"
    __table_args__ = (
        UniqueConstraint(
            "strategy", "mode", "period_start", "period_end", name="uq_strategy_perf_period"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    total_return: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    sharpe: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    sortino: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    trades_count: Mapped[int | None] = mapped_column(BigInteger)
    final_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))


class ModelRegistryModel(Base):
    __tablename__ = "model_registry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Numeric(10), nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String(512))
    hyperparams: Mapped[dict | None] = mapped_column(JSONB)
    offline_metrics: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="training")
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    training_window: Mapped[str | None] = mapped_column(String(64))


class BacktestRunModel(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    universe: Mapped[dict | None] = mapped_column(JSONB)
    start: Mapped[date] = mapped_column(Date, nullable=False)
    end: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    final_capital: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventRecordModel(Base):
    """Outbox / audit journal — one row per published domain event."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_type_occurred", "type", "occurred_at"),
        Index("ix_events_processed", "processed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_uuid: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class SystemLogModel(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(8), nullable=False)
    component: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
