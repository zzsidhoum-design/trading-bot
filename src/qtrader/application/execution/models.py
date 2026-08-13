"""Phase 4 data model — execution assumptions, orders, fills and reports.

The execution layer sits between the Strategy layer and a future live execution
layer. It models spread, slippage, order types (market/limit/stop), latency,
partial fills, rejection, gaps and liquidity constraints — but never fabricates
bid/ask or order-book data. Every microstructure-shaped number is an explicit,
documented assumption carried by :class:`SlippageAssumptions` /
:class:`LiquidityAssumptions` (the "conservative assumptions" the requirements
call for when the source data is OHLCV only).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from qtrader.domain.entities import PerformanceSummary
from qtrader.domain.value_objects import OrderType, TradeSide


class ExecutionScenario(StrEnum):
    """Cost-of-execution regimes used to probe a strategy's robustness."""

    OPTIMISTIC = "optimistic"
    BASELINE = "baseline"
    CONSERVATIVE = "conservative"
    STRESS = "stress"


class ExecutionStatus(StrEnum):
    """Verdict after execution-aware backtesting (gates the validated label)."""

    EXECUTION_REJECTED = "execution_rejected"
    EXECUTION_SENSITIVE = "execution_sensitive"
    EXECUTION_ROBUST = "execution_robust"


@dataclass(frozen=True, slots=True)
class SlippageAssumptions:
    """One scenario's execution-friction assumptions (all explicit, all bps).

    ``base_spread_bps`` is the assumed half-spread captured per fill (the
    primary microstructure assumption; OHLCV cannot observe the real spread).
    ``base_slippage_bps`` is fixed per-fill friction (queueing/latency).
    ``impact_coefficient`` scales the market-impact term which is proportional
    to the order's participation in the symbol's average daily dollar volume.
    ``volatility_multiplier`` scales an ATR%-driven adverse-drift term.
    ``latency_seconds`` widens the volatility term as execution delay grows.
    ``gap_threshold_pct`` decides whether a stop order that gaps through its
    trigger fills at the (worse) opening price or at the trigger price.
    ``max_slippage_bps`` caps the total per-fill slippage.
    """

    scenario: ExecutionScenario
    base_spread_bps: float
    base_slippage_bps: float
    impact_coefficient: float
    volatility_multiplier: float
    latency_seconds: float
    gap_threshold_pct: float
    max_slippage_bps: float
    partial_fill_scale: float = 1.0

    def __post_init__(self) -> None:
        if self.max_slippage_bps <= 0.0:
            raise ValueError("max_slippage_bps must be positive")
        if self.gap_threshold_pct < 0.0:
            raise ValueError("gap_threshold_pct must be >= 0")


def default_slippage_assumptions() -> dict[ExecutionScenario, SlippageAssumptions]:
    """Conservative, configurable friction presets for each scenario.

    These are assumptions — not observed microstructure — because the data
    layer is OHLCV only. Baseline is calibrated to be *at least as expensive*
    as the 10 bps commission / 50 bps slippage the research pipeline already
    charges, so execution-aware results are never rosier than research ones.
    """
    return {
        ExecutionScenario.OPTIMISTIC: SlippageAssumptions(
            scenario=ExecutionScenario.OPTIMISTIC,
            base_spread_bps=0.5,
            base_slippage_bps=0.0,
            impact_coefficient=0.1,
            volatility_multiplier=0.5,
            latency_seconds=1.0,
            gap_threshold_pct=0.01,
            max_slippage_bps=25.0,
        ),
        ExecutionScenario.BASELINE: SlippageAssumptions(
            scenario=ExecutionScenario.BASELINE,
            base_spread_bps=2.0,
            base_slippage_bps=1.0,
            impact_coefficient=0.25,
            volatility_multiplier=1.0,
            latency_seconds=5.0,
            gap_threshold_pct=0.02,
            max_slippage_bps=75.0,
        ),
        ExecutionScenario.CONSERVATIVE: SlippageAssumptions(
            scenario=ExecutionScenario.CONSERVATIVE,
            base_spread_bps=5.0,
            base_slippage_bps=3.0,
            impact_coefficient=0.5,
            volatility_multiplier=1.5,
            latency_seconds=30.0,
            gap_threshold_pct=0.05,
            max_slippage_bps=150.0,
        ),
        ExecutionScenario.STRESS: SlippageAssumptions(
            scenario=ExecutionScenario.STRESS,
            base_spread_bps=10.0,
            base_slippage_bps=8.0,
            impact_coefficient=1.0,
            volatility_multiplier=2.0,
            latency_seconds=300.0,
            gap_threshold_pct=0.10,
            max_slippage_bps=300.0,
        ),
    }


@dataclass(frozen=True, slots=True)
class LiquidityAssumptions:
    """Liquidity constraints enforced per order (no fabricated book data).

    ``max_participation_rate`` caps a single order's fill to this fraction of
    the bar's volume. ``max_notional_pct_adv`` caps order size to this fraction
    of the symbol's average daily dollar volume; exceeding it is an
    "unrealistic trade size" and is rejected and flagged. ``adv_window_bars``
    is the lookback used to estimate average daily volume / dollar volume.
    """

    max_participation_rate: float = 0.10
    max_notional_pct_adv: float = 0.01
    min_avg_volume: Decimal = Decimal("50000")
    min_avg_dollar_volume: Decimal = Decimal("500000")
    adv_window_bars: int = 21

    def __post_init__(self) -> None:
        if not 0.0 < self.max_participation_rate <= 1.0:
            raise ValueError("max_participation_rate must be in (0, 1]")
        if self.max_notional_pct_adv <= 0.0:
            raise ValueError("max_notional_pct_adv must be positive")
        if self.adv_window_bars < 1:
            raise ValueError("adv_window_bars must be >= 1")


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Everything one execution-robustness run needs (settings-driven)."""

    scenarios: tuple[ExecutionScenario, ...] = (
        ExecutionScenario.OPTIMISTIC,
        ExecutionScenario.BASELINE,
        ExecutionScenario.CONSERVATIVE,
        ExecutionScenario.STRESS,
    )
    slippage: Mapping[ExecutionScenario, SlippageAssumptions] = field(
        default_factory=default_slippage_assumptions
    )
    liquidity: LiquidityAssumptions = field(default_factory=LiquidityAssumptions)
    commission_bps: float = 10.0
    min_commission: Decimal | None = None
    min_fill_rate: float = 0.90
    min_net_sharpe: float = 0.0
    max_absolute_sharpe_degradation: float = 0.5
    max_return_degradation: float = 0.5
    max_rejected_rate: float = 0.25
    seed: int = 42

    def __post_init__(self) -> None:
        if not self.scenarios:
            raise ValueError("at least one execution scenario is required")
        missing = [s for s in self.scenarios if s not in self.slippage]
        if missing:
            raise ValueError(
                f"missing slippage assumptions for scenario(s): {missing}"
            )

    def slippage_for(self, scenario: ExecutionScenario) -> SlippageAssumptions:
        return self.slippage[scenario]


@dataclass(frozen=True, slots=True)
class TradingHoursPolicy:
    """When bars are executable (model of the trading-session calendar).

    Defaults to ``always_open=True``: bar timestamps are themselves the traded
    sessions (Yahoo emits session bars only), so every bar is executable. Set
    ``always_open=False`` plus session times to simulate intraday sessions
    explicitly; non-tradable bars simply delay pending orders.
    """

    always_open: bool = True
    timezone: str = "America/New_York"
    open_time: str = "09:30"
    close_time: str = "16:00"

    def tradable(self, ts: datetime) -> bool:
        if self.always_open:
            return True
        hour_min = ts.hour * 60 + ts.minute
        open_min = _minutes(self.open_time)
        close_min = _minutes(self.close_time)
        return ts.weekday() < 5 and open_min <= hour_min < close_min


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


# --------------------------------------------------------------------------- #
# Orders, fills and per-run statistics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ExecutionOrder:
    """One order submitted to the execution simulator."""

    symbol: str
    side: TradeSide
    quantity: int
    order_type: OrderType
    signal_ts: datetime
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    order_id: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionFill:
    """One completed fill (price already includes slippage; fee separate)."""

    symbol: str
    side: TradeSide
    quantity: int
    price: Decimal
    commission: Decimal
    slippage_bps: float
    ts: datetime
    partial: bool


@dataclass(frozen=True, slots=True)
class LiquidityAssessment:
    """Result of a size/liquidity check on one order."""

    approved: bool
    reasons: tuple[str, ...]
    max_fillable: int


@dataclass(slots=True)
class ExecutionStats:
    """Per-run execution accounting (mutable accumulator)."""

    submitted: int = 0
    filled: int = 0
    partial_fills: int = 0
    rejected: int = 0
    canceled: int = 0
    unrealistic_orders: int = 0
    slippage_bps_values: list[float] = field(default_factory=list)
    deviation_bps_values: list[float] = field(default_factory=list)
    total_commission: Decimal = Decimal("0")
    total_slippage: Decimal = Decimal("0")


# --------------------------------------------------------------------------- #
# Per-strategy execution reports
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ExecutionMetrics:
    """The required per-strategy execution metrics for one scenario."""

    scenario: ExecutionScenario
    expected_slippage_bps: float
    avg_execution_deviation_bps: float
    fill_rate: float
    partial_fill_rate: float
    rejected_rate: float
    transaction_costs: Decimal
    turnover: float | None
    net_return: float | None
    net_sharpe: float | None
    net_sortino: float | None
    max_drawdown: float | None
    trades: int
    degradation_return: float | None
    degradation_sharpe: float | None
    liquidity_flags: tuple[str, ...]
    rejection_messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """One scenario's execution-aware outcome (persistable summary)."""

    scenario: ExecutionScenario
    summary: PerformanceSummary
    stats: ExecutionStats
    metrics: ExecutionMetrics


@dataclass(frozen=True, slots=True)
class StrategyExecutionReport:
    """The full execution-robustness verdict for one strategy.

    ``theoretical_*`` come from the reference (research) backtest on the same
    OOS window; ``degradation_*`` are the baseline-scenario gap vs theoretical.
    ``execution_sensitivity`` is the largest absolute Sharpe degradation across
    scenarios — the number that decides SENSITIVE vs ROBUST.
    """

    strategy_id: str
    status: ExecutionStatus
    theoretical_return: float | None
    theoretical_sharpe: float | None
    theoretical_trades: int
    scenarios: tuple[ScenarioResult, ...]
    execution_sensitivity: float | None = None
    worst_scenario: ExecutionScenario | None = None
    notes: str = ""

    def scenario_result(self, scenario: ExecutionScenario) -> ScenarioResult | None:
        return next((s for s in self.scenarios if s.scenario is scenario), None)


@dataclass(frozen=True, slots=True)
class ExecutionRunReport:
    """Aggregate output of one execution-robustness run (required counts)."""

    strategies_tested: int
    execution_rejected: int
    execution_sensitive: int
    execution_robust: int
    by_strategy: dict[str, ExecutionStatus] = field(default_factory=dict)
    notes: str = ""


# --------------------------------------------------------------------------- #
# JSON-safe encoding (for the research validation database)
# --------------------------------------------------------------------------- #


def encode_execution_report(report: StrategyExecutionReport) -> dict[str, Any]:
    return {
        "strategy_id": report.strategy_id,
        "status": report.status.value,
        "theoretical_return": report.theoretical_return,
        "theoretical_sharpe": report.theoretical_sharpe,
        "theoretical_trades": report.theoretical_trades,
        "scenarios": [
            {
                "scenario": sr.scenario.value,
                "summary": _encode_summary(sr.summary),
                "stats": _encode_stats(sr.stats),
                "metrics": _encode_metrics(sr.metrics),
            }
            for sr in report.scenarios
        ],
        "execution_sensitivity": report.execution_sensitivity,
        "worst_scenario": (
            report.worst_scenario.value if report.worst_scenario is not None else None
        ),
        "notes": report.notes,
    }


def decode_execution_report(data: dict[str, Any]) -> StrategyExecutionReport:
    return StrategyExecutionReport(
        strategy_id=str(data["strategy_id"]),
        status=ExecutionStatus(str(data["status"])),
        theoretical_return=data.get("theoretical_return"),
        theoretical_sharpe=data.get("theoretical_sharpe"),
        theoretical_trades=int(data.get("theoretical_trades", 0)),
        scenarios=tuple(
            ScenarioResult(
                scenario=ExecutionScenario(str(item["scenario"])),
                summary=_decode_summary(item["summary"]),
                stats=_decode_stats(item["stats"]),
                metrics=_decode_metrics(item["metrics"]),
            )
            for item in data.get("scenarios", ())
        ),
        execution_sensitivity=data.get("execution_sensitivity"),
        worst_scenario=(
            ExecutionScenario(str(data["worst_scenario"]))
            if data.get("worst_scenario")
            else None
        ),
        notes=str(data.get("notes", "")),
    )


def _encode_stats(stats: ExecutionStats) -> dict[str, Any]:
    return {
        "submitted": stats.submitted,
        "filled": stats.filled,
        "partial_fills": stats.partial_fills,
        "rejected": stats.rejected,
        "canceled": stats.canceled,
        "unrealistic_orders": stats.unrealistic_orders,
        "slippage_bps_values": stats.slippage_bps_values,
        "deviation_bps_values": stats.deviation_bps_values,
        "total_commission": str(stats.total_commission),
        "total_slippage": str(stats.total_slippage),
    }


def _decode_stats(data: dict[str, Any]) -> ExecutionStats:
    stats = ExecutionStats(
        submitted=int(data.get("submitted", 0)),
        filled=int(data.get("filled", 0)),
        partial_fills=int(data.get("partial_fills", 0)),
        rejected=int(data.get("rejected", 0)),
        canceled=int(data.get("canceled", 0)),
        unrealistic_orders=int(data.get("unrealistic_orders", 0)),
        slippage_bps_values=[float(v) for v in data.get("slippage_bps_values", ())],
        deviation_bps_values=[float(v) for v in data.get("deviation_bps_values", ())],
        total_commission=Decimal(data.get("total_commission", "0")),
        total_slippage=Decimal(data.get("total_slippage", "0")),
    )
    return stats


def _encode_metrics(metrics: ExecutionMetrics) -> dict[str, Any]:
    return {
        "scenario": metrics.scenario.value,
        "expected_slippage_bps": metrics.expected_slippage_bps,
        "avg_execution_deviation_bps": metrics.avg_execution_deviation_bps,
        "fill_rate": metrics.fill_rate,
        "partial_fill_rate": metrics.partial_fill_rate,
        "rejected_rate": metrics.rejected_rate,
        "transaction_costs": str(metrics.transaction_costs),
        "turnover": metrics.turnover,
        "net_return": metrics.net_return,
        "net_sharpe": metrics.net_sharpe,
        "net_sortino": metrics.net_sortino,
        "max_drawdown": metrics.max_drawdown,
        "trades": metrics.trades,
        "degradation_return": metrics.degradation_return,
        "degradation_sharpe": metrics.degradation_sharpe,
        "liquidity_flags": list(metrics.liquidity_flags),
        "rejection_messages": list(metrics.rejection_messages),
    }


def _decode_metrics(data: dict[str, Any]) -> ExecutionMetrics:
    return ExecutionMetrics(
        scenario=ExecutionScenario(str(data["scenario"])),
        expected_slippage_bps=float(data.get("expected_slippage_bps", 0.0)),
        avg_execution_deviation_bps=float(data.get("avg_execution_deviation_bps", 0.0)),
        fill_rate=float(data.get("fill_rate", 1.0)),
        partial_fill_rate=float(data.get("partial_fill_rate", 0.0)),
        rejected_rate=float(data.get("rejected_rate", 0.0)),
        transaction_costs=Decimal(data.get("transaction_costs", "0")),
        turnover=data.get("turnover"),
        net_return=data.get("net_return"),
        net_sharpe=data.get("net_sharpe"),
        net_sortino=data.get("net_sortino"),
        max_drawdown=data.get("max_drawdown"),
        trades=int(data.get("trades", 0)),
        degradation_return=data.get("degradation_return"),
        degradation_sharpe=data.get("degradation_sharpe"),
        liquidity_flags=tuple(str(f) for f in data.get("liquidity_flags", ())),
        rejection_messages=tuple(
            str(m) for m in data.get("rejection_messages", ())
        ),
    )


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _encode_summary(summary: PerformanceSummary) -> dict[str, Any]:
    return {
        "strategy": summary.strategy,
        "mode": summary.mode.value,
        "period_start": summary.period_start.isoformat(),
        "period_end": summary.period_end.isoformat(),
        "total_return": _str_or_none(summary.total_return),
        "cagr": _str_or_none(summary.cagr),
        "sharpe": _str_or_none(summary.sharpe),
        "sortino": _str_or_none(summary.sortino),
        "max_drawdown": _str_or_none(summary.max_drawdown),
        "win_rate": _str_or_none(summary.win_rate),
        "profit_factor": _str_or_none(summary.profit_factor),
        "expectancy": _str_or_none(summary.expectancy),
        "avg_win": _str_or_none(summary.avg_win),
        "avg_loss": _str_or_none(summary.avg_loss),
        "turnover": _str_or_none(summary.turnover),
        "total_costs": _str_or_none(summary.total_costs),
        "trades_count": summary.trades_count,
        "final_equity": _str_or_none(summary.final_equity),
    }


def _decode_summary(data: dict[str, Any]) -> PerformanceSummary:
    return PerformanceSummary(
        strategy=str(data["strategy"]),
        mode=data["mode"],
        period_start=date.fromisoformat(data["period_start"]),
        period_end=date.fromisoformat(data["period_end"]),
        total_return=_dec(data.get("total_return")),
        cagr=_dec(data.get("cagr")),
        sharpe=_dec(data.get("sharpe")),
        sortino=_dec(data.get("sortino")),
        max_drawdown=_dec(data.get("max_drawdown")),
        win_rate=_dec(data.get("win_rate")),
        profit_factor=_dec(data.get("profit_factor")),
        expectancy=_dec(data.get("expectancy")),
        avg_win=_dec(data.get("avg_win")),
        avg_loss=_dec(data.get("avg_loss")),
        turnover=_dec(data.get("turnover")),
        total_costs=_dec(data.get("total_costs")),
        trades_count=data.get("trades_count"),
        final_equity=_dec(data.get("final_equity")),
    )


def _str_or_none(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "ExecutionFill",
    "ExecutionMetrics",
    "ExecutionOrder",
    "ExecutionPlan",
    "ExecutionRunReport",
    "ExecutionScenario",
    "ExecutionStats",
    "ExecutionStatus",
    "LiquidityAssessment",
    "LiquidityAssumptions",
    "ScenarioResult",
    "SlippageAssumptions",
    "StrategyExecutionReport",
    "TradingHoursPolicy",
    "decode_execution_report",
    "default_slippage_assumptions",
    "encode_execution_report",
]
