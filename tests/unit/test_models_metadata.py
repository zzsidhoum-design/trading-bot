"""Verify the ORM metadata matches the designed schema (table inventory)."""

from __future__ import annotations

from qtrader.infrastructure.database import models  # noqa: F401
from qtrader.infrastructure.database.base import Base

EXPECTED_TABLES = {
    "stocks",
    "prices",
    "indicators",
    "fundamentals",
    "earnings",
    "news",
    "signals",
    "predictions",
    "portfolios",
    "positions",
    "orders",
    "trades",
    "decision_log",
    "risk_history",
    "agent_metrics",
    "strategy_performance",
    "model_registry",
    "backtest_runs",
    "events",
    "system_logs",
}


class TestSchemaInventory:
    def test_all_designed_tables_exist(self) -> None:
        actual = set(Base.metadata.tables)
        missing = EXPECTED_TABLES - actual
        assert not missing, f"missing tables: {missing}"

    def test_no_extra_tables(self) -> None:
        actual = set(Base.metadata.tables)
        extra = actual - EXPECTED_TABLES
        assert not extra, f"unexpected tables: {extra}"

    def test_prices_partitioned_by_ts_index(self) -> None:
        prices = Base.metadata.tables["prices"]
        assert prices.columns["ts"].type.timezone is True  # TIMESTAMPTZ (UTC)
        assert prices.columns["interval"].nullable is False
