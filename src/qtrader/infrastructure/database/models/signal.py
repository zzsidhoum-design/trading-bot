"""Signals, predictions, decisions & risk history tables."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qtrader.infrastructure.database.base import Base, utcnow


class SignalModel(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_stock_agent_created", "stock_id", "agent", "created_at"),
        Index("ix_signals_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str | None] = mapped_column(String(8))
    signal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    strength: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    horizon: Mapped[str | None] = mapped_column(String(16))
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PredictionModel(Base):
    __tablename__ = "predictions"
    __table_args__ = (Index("ix_predictions_stock_created", "stock_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[int] = mapped_column(Numeric(10), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    prob_up: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    prob_down: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    prob_trend: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    expected_return: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    expected_volatility: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    features_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionLogModel(Base):
    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_uuid: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    rationale: Mapped[str | None] = mapped_column(Text)
    agent_scores: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskHistoryModel(Base):
    __tablename__ = "risk_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_uuid: Mapped[str | None] = mapped_column(String(36))
    portfolio_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("portfolios.id"))
    stock_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("stocks.id"))
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    position_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    risk_per_trade_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    exposure_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    max_daily_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    daily_pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
