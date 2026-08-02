"""SystemGate -- graduation gate from backtest to paper/live trading.

Phase 6. A strategy may only trade in PAPER (and eventually LIVE) mode once a
backtest shows it clears every configured threshold (trades, win rate, profit
factor, Sharpe, drawdown). Evaluations are audited into ``system_logs`` so the
gate's history is fully explainable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from qtrader.domain.entities import SystemLog
from qtrader.domain.ports import PerformanceRepository, SystemLogRepository
from qtrader.domain.value_objects import TradingMode

logger = logging.getLogger("qtrader.system_gate")


class GateStatus(StrEnum):
    IDLE = "idle"
    GRADUATED = "graduated"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class GateThresholds:
    """Minimum bar a backtest must clear before graduating to paper trading."""

    min_trades: int = 30
    min_win_rate: float = 0.50
    min_profit_factor: float = 1.2
    min_sharpe: float = 1.0
    max_drawdown: float = 0.25
    min_total_return: float = 0.0


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Result of one graduation evaluation."""

    strategy: str
    mode: TradingMode
    status: GateStatus
    approved: bool
    reasons: list[str] = field(default_factory=list)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SystemGate:
    """Decides whether a strategy may graduate to paper trading."""

    def __init__(
        self,
        thresholds: GateThresholds,
        performance: PerformanceRepository,
        logs: SystemLogRepository,
    ) -> None:
        self._thresholds = thresholds
        self._performance = performance
        self._logs = logs

    @property
    def thresholds(self) -> GateThresholds:
        return self._thresholds

    async def evaluate(self, strategy: str, mode: TradingMode) -> GateDecision:
        """Check the latest BACKTEST results against every threshold."""
        if mode is TradingMode.BACKTEST:
            decision = GateDecision(strategy, mode, GateStatus.GRADUATED, True, [])
        else:
            summary = await self._performance.latest_for_strategy(strategy, TradingMode.BACKTEST)
            reasons: list[str] = []
            if summary is None:
                reasons.append("no backtest results for strategy")
            else:
                if (
                    summary.trades_count is None
                    or summary.trades_count < self._thresholds.min_trades
                ):
                    reasons.append(
                        f"trades {summary.trades_count} < min {self._thresholds.min_trades}"
                    )
                if (
                    summary.win_rate is not None
                    and float(summary.win_rate) < self._thresholds.min_win_rate
                ):
                    reasons.append(
                        f"win rate {summary.win_rate:.2%} < min {self._thresholds.min_win_rate:.0%}"
                    )
                if (
                    summary.profit_factor is not None
                    and float(summary.profit_factor) < self._thresholds.min_profit_factor
                ):
                    pf_msg = (
                        f"profit factor {summary.profit_factor:.2f} < "
                        f"min {self._thresholds.min_profit_factor:.2f}"
                    )
                    reasons.append(pf_msg)
                if (
                    summary.sharpe is not None
                    and float(summary.sharpe) < self._thresholds.min_sharpe
                ):
                    reasons.append(
                        f"sharpe {summary.sharpe:.2f} < min {self._thresholds.min_sharpe:.2f}"
                    )
                if (
                    summary.max_drawdown is not None
                    and float(summary.max_drawdown) < -abs(self._thresholds.max_drawdown)
                ):
                    reasons.append(
                        f"max drawdown {summary.max_drawdown:.2%} exceeds "
                        f"limit {self._thresholds.max_drawdown:.0%}"
                    )
                if (
                    summary.total_return is not None
                    and float(summary.total_return) < self._thresholds.min_total_return
                ):
                    tr_msg = (
                        f"total return {summary.total_return:.2%} < "
                        f"min {self._thresholds.min_total_return:.0%}"
                    )
                    reasons.append(tr_msg)
            approved = not reasons
            status = GateStatus.GRADUATED if approved else GateStatus.DENIED
            decision = GateDecision(strategy, mode, status, approved, reasons)

        await self._log_decision(decision)
        return decision

    async def can_trade(self, strategy: str, mode: TradingMode) -> bool:
        """True when the strategy may execute in ``mode`` under the gate."""
        if mode is TradingMode.BACKTEST:
            return True
        decision = await self.evaluate(strategy, mode)
        return decision.approved

    async def _log_decision(self, decision: GateDecision) -> None:
        level = "INFO" if decision.approved else "WARN"
        context = {
            "strategy": decision.strategy,
            "mode": decision.mode.value,
            "status": decision.status.value,
            "reasons": decision.reasons,
        }
        message = (
            f"gate {decision.status.value} for {decision.strategy} in {decision.mode.value}"
        )
        await self._logs.record(
            SystemLog(level=level, component="system_gate", message=message, context=context)
        )
        if decision.approved:
            logger.info(message)
        else:
            logger.warning("%s: %s", message, "; ".join(decision.reasons) or "no data")


__all__ = ["GateDecision", "GateStatus", "GateThresholds", "SystemGate"]
