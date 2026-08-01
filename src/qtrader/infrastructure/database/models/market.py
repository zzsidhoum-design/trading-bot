"""Fundamentals, earnings & news tables."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qtrader.infrastructure.database.base import Base, utcnow


class FundamentalModel(Base):
    __tablename__ = "fundamentals"
    __table_args__ = (UniqueConstraint("stock_id", "period", name="uq_fundamentals_stock_period"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    report_date: Mapped[date | None] = mapped_column(Date)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    eps: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    pe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    debt_total: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    roe: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    roa: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    gross_margin: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    operating_margin: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    net_margin: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    revenue_growth: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    earnings_growth: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    price_to_book: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))


class EarningModel(Base):
    __tablename__ = "earnings"
    __table_args__ = (
        UniqueConstraint("stock_id", "fiscal_period", name="uq_earnings_stock_fiscal_period"),
        Index("ix_earnings_report_date", "report_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stock_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stocks.id"), nullable=False)
    fiscal_period: Mapped[str] = mapped_column(String(16), nullable=False)
    report_date: Mapped[date | None] = mapped_column(Date)
    eps_actual: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    eps_estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    revenue_actual: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    revenue_estimate: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    surprise_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    is_upcoming: Mapped[bool] = mapped_column(Boolean, default=False)


class NewsModel(Base):
    __tablename__ = "news"
    __table_args__ = (
        Index("ix_news_stock_published", "stock_id", "published_at"),
        Index("ix_news_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stock_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("stocks.id"))
    source: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)))
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    summary: Mapped[str | None] = mapped_column(Text)
    expected_market_impact: Mapped[str | None] = mapped_column(String(8))
    impact_direction: Mapped[int | None] = mapped_column(Numeric(1))
    analysis_confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
