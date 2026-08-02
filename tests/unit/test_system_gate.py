"""Unit tests for the SystemGate graduation state machine."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from qtrader.application.services.system_gate import GateStatus, GateThresholds, SystemGate
from qtrader.domain.entities import PerformanceSummary
from qtrader.domain.value_objects import TradingMode
from tests.unit.fakes_phase6 import FakePerformanceRepository, FakeSystemLogRepository


def _gate(
    performance: FakePerformanceRepository | None = None,
    logs: FakeSystemLogRepository | None = None,
    thresholds: GateThresholds | None = None,
) -> SystemGate:
    return SystemGate(
        thresholds=thresholds or GateThresholds(),
        performance=performance or FakePerformanceRepository(),
        logs=logs or FakeSystemLogRepository(),
    )


def _summary(**overrides) -> PerformanceSummary:
    defaults = dict(
        strategy="ensemble",
        mode=TradingMode.BACKTEST,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 1),
        total_return=Decimal("0.25"),
        sharpe=Decimal("1.5"),
        sortino=Decimal("2.0"),
        max_drawdown=Decimal("-0.10"),
        win_rate=Decimal("0.55"),
        profit_factor=Decimal("1.4"),
        trades_count=60,
        final_equity=Decimal("125000"),
    )
    defaults.update(overrides)
    return PerformanceSummary(**defaults)


@pytest.mark.asyncio
async def test_backtest_mode_always_graduated() -> None:
    logs = FakeSystemLogRepository()
    gate = _gate(logs=logs)
    decision = await gate.evaluate("ensemble", TradingMode.BACKTEST)
    assert decision.approved is True
    assert decision.status is GateStatus.GRADUATED


@pytest.mark.asyncio
async def test_paper_denied_without_backtest_results() -> None:
    logs = FakeSystemLogRepository()
    gate = _gate(logs=logs)
    decision = await gate.evaluate("ensemble", TradingMode.PAPER)
    assert decision.approved is False
    assert decision.status is GateStatus.DENIED
    assert "no backtest results" in decision.reasons[0]
    assert any(e.level == "WARN" for e in logs.entries)


@pytest.mark.asyncio
async def test_paper_graduated_when_thresholds_cleared() -> None:
    perf = FakePerformanceRepository()
    await perf.upsert(_summary())
    logs = FakeSystemLogRepository()
    gate = _gate(performance=perf, logs=logs)
    decision = await gate.evaluate("ensemble", TradingMode.PAPER)
    assert decision.approved is True
    assert decision.status is GateStatus.GRADUATED
    assert any(e.level == "INFO" for e in logs.entries)


@pytest.mark.asyncio
async def test_paper_denied_when_thresholds_fail() -> None:
    perf = FakePerformanceRepository()
    await perf.upsert(
        _summary(
            trades_count=5,
            win_rate=Decimal("0.30"),
            profit_factor=Decimal("0.8"),
            sharpe=Decimal("0.2"),
            max_drawdown=Decimal("-0.50"),
            total_return=Decimal("-0.10"),
        )
    )
    gate = _gate(performance=perf)
    decision = await gate.evaluate("ensemble", TradingMode.PAPER)
    assert decision.approved is False
    joined = " ".join(decision.reasons)
    assert "trades" in joined
    assert "win rate" in joined
    assert "profit factor" in joined
    assert "sharpe" in joined
    assert "max drawdown" in joined
    assert "total return" in joined


@pytest.mark.asyncio
async def test_can_trade_short_circuit() -> None:
    gate = _gate()
    assert await gate.can_trade("ensemble", TradingMode.BACKTEST) is True
    assert await gate.can_trade("ensemble", TradingMode.PAPER) is False


@pytest.mark.asyncio
async def test_can_trade_paper_after_graduation() -> None:
    perf = FakePerformanceRepository()
    await perf.upsert(_summary())
    gate = _gate(performance=perf)
    assert await gate.can_trade("ensemble", TradingMode.PAPER) is True
