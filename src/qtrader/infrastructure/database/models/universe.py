"""Dynamic trading universe tables: membership + symbol-change history."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from qtrader.infrastructure.database.base import Base, utcnow


class UniverseMembershipModel(Base):
    """One row per symbol in the dynamic universe.

    ``symbol`` is unique — the row is the current state; lifecycle transitions
    are observable through ``status``, ``added_at`` and ``removed_at``. The
    point-in-time reconstruction reads ``added_at``/``removed_at`` so backtests
    only ever see symbols that were listed at the time.
    """

    __tablename__ = "universe_memberships"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_universe_memberships_symbol"),
        Index("ix_universe_memberships_status", "status"),
        Index("ix_universe_memberships_removed_at", "removed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    tier: Mapped[str | None] = mapped_column(String(1))
    added_at: Mapped[date | None] = mapped_column(Date)
    removed_at: Mapped[date | None] = mapped_column(Date)
    last_traded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    asset_type: Mapped[str] = mapped_column(String(24), nullable=False, default="common_stock")
    name: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(String(255))
    extras: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SymbolChangeModel(Base):
    """Audit trail of ticker renames (old -> new)."""

    __tablename__ = "universe_symbol_changes"
    __table_args__ = (
        Index("ix_universe_symbol_changes_old", "old_symbol"),
        Index("ix_universe_symbol_changes_new", "new_symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    old_symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    new_symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    effective_at: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
