"""Shared fixtures for the Portfolio & Risk Management Engine tests
(not collected by pytest)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from qtrader.application.research.strategy.specs import (
    Condition,
    EntryRule,
    Operator,
    StrategySpec,
)
from qtrader.application.research.validation.records import (
    FinalStatus,
    StageResult,
    ValidationRecord,
    ValidationStage,
)
from qtrader.domain.entities import PerformanceSummary
from qtrader.domain.value_objects import TradingMode


def make_validation_record(
    strategy_id: str,
    *,
    sharpe: Decimal = Decimal("1.0"),
    sortino: Decimal = Decimal("1.5"),
    total_return: Decimal = Decimal("0.20"),
    max_drawdown: Decimal = Decimal("0.15"),
    final_status: FinalStatus = FinalStatus.EXECUTION_ROBUST,
    stage: ValidationStage = ValidationStage.EXECUTION_ROBUST,
) -> ValidationRecord:
    """Build a validation record with an OOS performance summary."""
    spec = StrategySpec(
        id=strategy_id,
        name=strategy_id,
        entry=EntryRule(
            conditions=(Condition(feature="close", op=Operator.GT, value=50.0),)
        ),
    )
    summary = PerformanceSummary(
        strategy=strategy_id,
        mode=TradingMode.BACKTEST,
        period_start=date(2023, 1, 1),
        period_end=date(2024, 1, 1),
        total_return=total_return,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown,
    )
    return ValidationRecord(
        spec=spec,
        stage=stage,
        final_status=final_status,
        oos_result=StageResult(label="oos", summary=summary),
    )


def with_final_status(
    record: ValidationRecord,
    status: FinalStatus,
) -> ValidationRecord:
    return replace(record, final_status=status)
