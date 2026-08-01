"""Universe & price tables."""

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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qtrader.infrastructure.database.base import Base, utcnow


class StockModel(Base):
    __tablename__ = "stocks"
    __table_args__ = (
        UniqueConstraint("symbol", "exchange", name="uq_stocks_symbol_exchange"),
        Index("ix_stocks_sector", "sector"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    sector: Mapped[str | None] = mapped_column(String(64))
    industry: Mapped[str | None] = mapped_column(String(64))
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    shares_outstanding: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PriceModel(Base):
    """OHLCV bars.

    NOTE: production deployment partitions this table by RANGE(ts) monthly.
    The partitioning is applied in a dedicated Alembic migration after the
    base table exists (Postgres `PARTITION BY RANGE` + child partitions).
    """

    __tablename__ = "prices"
    __table_args__ = (
        UniqueConstraint("stock_id", "interval", "ts", name="uq_prices_stock_interval_ts"),
        Index("ix_prices_stock_interval_ts", "stock_id", "interval", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 0), nullable=False)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    source: Mapped[str | None] = mapped_column(String(32))


class IndicatorModel(Base):
    __tablename__ = "indicators"
    __table_args__ = (
        UniqueConstraint("stock_id", "interval", "ts", name="uq_indicators_stock_interval_ts"),
        Index("ix_indicators_stock_interval_ts", "stock_id", "interval", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rsi: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ema_9: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ema_21: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    sma_50: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    sma_200: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    macd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    macd_signal: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    macd_hist: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    atr: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    boll_upper: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    boll_middle: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    boll_lower: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    adx: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    stoch_k: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    stoch_d: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ichimoku_tenkan: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ichimoku_kijun: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ichimoku_senkou_a: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ichimoku_senkou_b: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ichimoku_chikou: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    volume_profile: Mapped[dict | None] = mapped_column(JSONB)
    extras: Mapped[dict | None] = mapped_column(JSONB)
