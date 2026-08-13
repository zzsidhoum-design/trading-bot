"""Execution metrics and degradation vs the theoretical (research) backtest.

Pure functions — no I/O. ``compute_execution_metrics`` turns one scenario's
simulator statistics plus the execution-aware performance summary into the
persistable :class:`ExecutionMetrics`; ``classify_execution`` applies the
:class:`ExecutionPlan` gates to decide EXECUTION_REJECTED / EXECUTION_SENSITIVE /
EXECUTION_ROBUST.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from qtrader.application.execution.models import (
    ExecutionMetrics,
    ExecutionPlan,
    ExecutionScenario,
    ExecutionStats,
    ExecutionStatus,
    LiquidityAssessment,
    LiquidityAssumptions,
)
from qtrader.application.services.backtest import ClosedTrade
from qtrader.domain.entities import PerformanceSummary

_ZERO = Decimal("0")


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _float(value: Decimal | float | None) -> float | None:
    if value is None:
        return None
    return float(value)


def compute_execution_metrics(
    *,
    scenario: ExecutionScenario,
    theoretical: PerformanceSummary,
    execution_summary: PerformanceSummary,
    execution_equity_curve: Sequence[tuple[datetime, Decimal]],
    trades: Sequence[ClosedTrade],
    stats: ExecutionStats,
    assessments: dict[str, LiquidityAssessment],
    adv_seen: dict[str, tuple[Decimal, Decimal]],
    liquidity: LiquidityAssumptions,
) -> ExecutionMetrics:
    """Metrics for one scenario, including degradation vs ``theoretical``."""
    submitted = max(stats.submitted, 0)
    filled = max(stats.filled, 0)
    fill_rate = filled / submitted if submitted else 1.0
    partial_fill_rate = stats.partial_fills / filled if filled else 0.0
    rejected_rate = stats.rejected / submitted if submitted else 0.0

    turnover: float | None = None
    avg_equity = (
        sum((eq for _, eq in execution_equity_curve), _ZERO)
        / len(execution_equity_curve)
        if execution_equity_curve
        else _ZERO
    )
    if avg_equity > 0:
        notional = sum(
            (
                trade.quantity * trade.entry_price
                + trade.quantity * trade.exit_price
            )
            for trade in trades
        )
        turnover = float(notional / avg_equity)

    theoretical_return = _float(theoretical.total_return)
    theoretical_sharpe = _float(theoretical.sharpe)
    net_return = _float(execution_summary.total_return)
    net_sharpe = _float(execution_summary.sharpe)

    degradation_return: float | None = None
    if theoretical_return is not None and net_return is not None:
        degradation_return = theoretical_return - net_return
    degradation_sharpe: float | None = None
    if theoretical_sharpe is not None and net_sharpe is not None:
        degradation_sharpe = theoretical_sharpe - net_sharpe

    flags: list[str] = []
    messages: list[str] = []
    if stats.unrealistic_orders > 0:
        flags.append("unrealistic-order-size-rejected")
        messages.append("REJECTED: unrealistic order size vs ADV dollars")
    for symbol, (adv_volume, adv_dollar) in adv_seen.items():
        if adv_volume < liquidity.min_avg_volume:
            flags.append(f"{symbol}:below-min-avg-volume")
            messages.append(
                f"REJECTED: {symbol} average daily volume {adv_volume:,.0f} "
                f"shares below floor {liquidity.min_avg_volume:,.0f}"
            )
        if adv_dollar < liquidity.min_avg_dollar_volume:
            flags.append(f"{symbol}:below-min-avg-dollar-volume")
            messages.append(
                f"REJECTED: {symbol} average daily dollar volume "
                f"${adv_dollar:,.0f} below floor ${liquidity.min_avg_dollar_volume:,.0f}"
            )
    for symbol, assessment in assessments.items():
        if not assessment.approved:
            flags.append(f"{symbol}:{':'.join(assessment.reasons)}")
            for reason in assessment.reasons:
                messages.append(f"REJECTED: {symbol} {reason}")

    return ExecutionMetrics(
        scenario=scenario,
        expected_slippage_bps=_mean(stats.slippage_bps_values),
        avg_execution_deviation_bps=_mean(stats.deviation_bps_values),
        fill_rate=fill_rate,
        partial_fill_rate=partial_fill_rate,
        rejected_rate=rejected_rate,
        transaction_costs=stats.total_commission,
        turnover=turnover,
        net_return=net_return,
        net_sharpe=net_sharpe,
        net_sortino=_float(execution_summary.sortino),
        max_drawdown=_float(execution_summary.max_drawdown),
        trades=len(trades),
        degradation_return=degradation_return,
        degradation_sharpe=degradation_sharpe,
        liquidity_flags=tuple(flags),
        rejection_messages=tuple(messages),
    )


def classify_execution(
    *,
    baseline: ExecutionMetrics,
    worst_degradation_sharpe: float | None,
    worst_degradation_return: float | None,
    plan: ExecutionPlan,
) -> ExecutionStatus:
    """Verdict for one strategy under the plan's gates."""
    if baseline.fill_rate < plan.min_fill_rate:
        return ExecutionStatus.EXECUTION_REJECTED
    if baseline.rejected_rate > plan.max_rejected_rate:
        return ExecutionStatus.EXECUTION_REJECTED
    if (
        baseline.net_sharpe is not None
        and baseline.net_sharpe < plan.min_net_sharpe
    ):
        return ExecutionStatus.EXECUTION_REJECTED
    if worst_degradation_sharpe is not None and (
        worst_degradation_sharpe > plan.max_absolute_sharpe_degradation
    ):
        return ExecutionStatus.EXECUTION_SENSITIVE
    if worst_degradation_return is not None and (
        worst_degradation_return > plan.max_return_degradation
    ):
        return ExecutionStatus.EXECUTION_SENSITIVE
    return ExecutionStatus.EXECUTION_ROBUST


def verdict_message(
    *,
    status: ExecutionStatus,
    baseline: ExecutionMetrics,
    worst_degradation_sharpe: float | None,
    worst_degradation_return: float | None,
    plan: ExecutionPlan,
) -> str:
    """Human-readable verdict reason, including every failing gate.

    Machine-readable flags stay in :attr:`ExecutionMetrics.liquidity_flags`;
    this is the message a reviewer sees (e.g. ``REJECTED: average daily volume
    below 50,000 shares``).
    """
    reasons = list(baseline.rejection_messages)
    if baseline.fill_rate < plan.min_fill_rate:
        reasons.append(
            f"REJECTED: fill rate {baseline.fill_rate:.1%} below {plan.min_fill_rate:.1%}"
        )
    if baseline.rejected_rate > plan.max_rejected_rate:
        reasons.append(
            f"REJECTED: rejected order rate {baseline.rejected_rate:.1%} "
            f"above {plan.max_rejected_rate:.1%}"
        )
    if (
        baseline.net_sharpe is not None
        and baseline.net_sharpe < plan.min_net_sharpe
    ):
        reasons.append(
            f"REJECTED: net Sharpe {baseline.net_sharpe:.2f} below {plan.min_net_sharpe:.2f}"
        )
    if worst_degradation_sharpe is not None and (
        worst_degradation_sharpe > plan.max_absolute_sharpe_degradation
    ):
        reasons.append(
            f"SENSITIVE: worst-case Sharpe degradation {worst_degradation_sharpe:.2f} "
            f"above {plan.max_absolute_sharpe_degradation:.2f}"
        )
    if worst_degradation_return is not None and (
        worst_degradation_return > plan.max_return_degradation
    ):
        reasons.append(
            f"SENSITIVE: worst-case return degradation {worst_degradation_return:.2f} "
            f"above {plan.max_return_degradation:.2f}"
        )
    if reasons:
        return "; ".join(reasons)
    return "EXECUTION ROBUST: fills, costs and worst-case degradation within plan gates"


__all__ = ["classify_execution", "compute_execution_metrics", "verdict_message"]
