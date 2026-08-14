"""Phase 5 data model — the independent Portfolio & Risk Management Engine.

The engine sits between strategy decisions and execution:

    Strategy / AI Decision
            ↓
    Portfolio Manager
            ↓
    Risk Engine
            ↓
    Execution Simulator
            ↓
    Execution

Everything in this module is pure data. There are no agent imports and no I/O:
the risk controls are authoritative, and AI recommendations enter the engine
only as plain ``ProposedTrade`` records (inputs, never decision-makers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from qtrader.domain.value_objects import TradeSide

_QTY_QUANT = Decimal("0.0001")
_MONEY_QUANT = Decimal("0.01")


def _now() -> datetime:
    return datetime.now(UTC)


class GateVerdict(StrEnum):
    """What the Risk Engine decided about a proposed trade."""

    APPROVE = "approve"
    MODIFY = "modify"
    REJECT = "reject"


class PositionSizingMethod(StrEnum):
    """Configurable position-sizing methods (the strategy signal never decides
    the size alone)."""

    FIXED_ALLOCATION = "fixed_allocation"
    VOLATILITY = "volatility"
    RISK_BUDGET = "risk_budget"
    MAX_EXPOSURE = "max_exposure"


class StrategyControlStatus(StrEnum):
    """Lifecycle status of a strategy under the failure controls.

    Temporary underperformance degrades status (MONITORED/REDUCED/SUSPENDED);
    a strategy is never permanently deleted because of a rough patch. Status
    moves back toward ACTIVE after a cooldown with no new breach.
    """

    ACTIVE = "active"
    MONITORED = "monitored"
    REDUCED = "reduced"
    SUSPENDED = "suspended"


class KillSwitchState(StrEnum):
    """Independent emergency shutdown state. Only an explicit operator action
    re-arms the switch; no code path auto-rearms it."""

    ARMED = "armed"
    TRIPPED = "tripped"


@dataclass(frozen=True, slots=True)
class Holding:
    """One open position as seen by the portfolio layer."""

    symbol: str
    market_value: Decimal
    weight_pct: float
    sector: str | None = None
    quantity: Decimal = Decimal(0)
    entry_price: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Point-in-time portfolio state handed to the Risk Engine."""

    equity: Decimal
    cash: Decimal
    gross_exposure_pct: float
    positions: tuple[Holding, ...] = ()
    positions_count: int = 0
    turnover_30d_pct: float = 0.0
    daily_pnl_pct: float = 0.0
    drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    avg_asset_correlation: float = 0.0
    leverage_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class ProposedTrade:
    """One strategy/AI recommendation ready for the Risk Engine gate.

    This is the only shape in which AI/strategy output may enter the engine.
    The engine treats it as data and may approve, modify or reject it.
    """

    strategy_id: str
    symbol: str
    side: TradeSide
    reference_price: Decimal
    quantity: Decimal
    sector: str | None = None
    atr_pct: float | None = None
    annualized_vol_pct: float | None = None
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    signal_ts: datetime | None = None
    confidence: float | None = None
    correlation_to_portfolio: float | None = None

    @property
    def notional(self) -> Decimal:
        return _qty(self.quantity) * _money(self.reference_price)


@dataclass(frozen=True, slots=True)
class PositionSize:
    """The Risk Engine's approved size for a proposed trade."""

    symbol: str
    quantity: Decimal
    notional: Decimal
    weight_pct: float
    method: PositionSizingMethod
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The authoritative outcome of one risk-gate evaluation."""

    verdict: GateVerdict
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    approved_quantity: Decimal | None = None
    modifications: tuple[str, ...] = ()
    position_size: PositionSize | None = None
    evaluated_exposure_pct: float | None = None


@dataclass(frozen=True, slots=True)
class PortfolioConstraints:
    """Configurable portfolio-level limits. All percentages are fractions."""

    max_position_weight_pct: float = 0.25
    max_portfolio_exposure_pct: float = 0.80
    max_sector_exposure_pct: float = 0.40
    max_correlated_exposure_pct: float = 0.50
    correlation_threshold: float = 0.70
    max_positions: int = 10
    max_turnover_pct: float = 0.50
    max_leverage_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class SizingPolicy:
    """Configurable position-sizing policy. The strategy signal never dictates
    size; one of these methods does."""

    method: PositionSizingMethod = PositionSizingMethod.FIXED_ALLOCATION
    fixed_allocation_pct: float = 0.20
    vol_target_pct: float = 0.10
    max_annualized_vol_pct: float = 0.40
    risk_per_trade_pct: float = 0.01
    max_weight_pct: float = 0.25
    annualization: float = 252.0

    def __post_init__(self) -> None:
        if not 0.0 < self.max_weight_pct <= 1.0:
            raise ValueError("max_weight_pct must be in (0, 1]")
        if self.annualization <= 0:
            raise ValueError("annualization must be positive")


@dataclass(frozen=True, slots=True)
class DrawdownProtection:
    """Configurable drawdown/daily-loss/consecutive-loss controls.

    ``monitor_drawdown_pct`` / ``reduce_drawdown_pct`` are the thresholds at
    which a strategy's status is demoted to MONITORED / REDUCED; breaching
    ``max_strategy_drawdown_pct`` suspends it (never deletes). ``suspension_
    cooldown_days`` is the minimum time before an automatic rearm.
    """

    max_strategy_drawdown_pct: float = 0.25
    max_portfolio_drawdown_pct: float = 0.20
    max_daily_loss_pct: float = 0.03
    max_consecutive_losses: int = 5
    monitor_drawdown_pct: float = 0.15
    reduce_drawdown_pct: float = 0.20
    suspension_cooldown_days: int = 30
    monitored_weight_factor: float = 0.75
    reduced_weight_factor: float = 0.50


@dataclass(frozen=True, slots=True)
class AllocationPolicyConfig:
    """Knobs for the risk-aware strategy allocator.

    The allocator scores strategies on risk, drawdown, volatility, correlation,
    out-of-sample performance, execution robustness and (optionally) the current
    market regime — never on historical returns alone.
    """

    sharpe_weight: float = 1.0
    sortino_weight: float = 0.5
    oos_return_weight: float = 0.5
    execution_weight: float = 1.0
    drawdown_weight: float = 1.0
    volatility_weight: float = 0.5
    correlation_weight: float = 1.0
    regime_weight: float = 0.0
    min_weight_pct: float = 0.05
    max_weight_pct: float = 0.50
    risk_free_rate: float = 0.0
    periods_per_year: float = 252.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_weight_pct < self.max_weight_pct <= 1.0:
            raise ValueError("require 0 <= min_weight_pct < max_weight_pct <= 1")


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    """Risk-adjusted evaluation of a return series (all percentages fractions)."""

    expected_return_pct: float
    volatility_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    var_95_pct: float
    expected_shortfall_pct: float
    avg_correlation: float
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StrategyScore:
    """One strategy's allocation result with its documented rationale."""

    strategy_id: str
    weight_pct: float
    score: float
    sharpe: float | None
    sortino: float | None
    max_drawdown_pct: float | None
    volatility_pct: float | None
    execution_quality: float
    avg_strategy_correlation: float
    rationale: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AllocationReport:
    """Risk-aware capital allocation across validated strategies."""

    strategies: tuple[StrategyScore, ...]
    total_weight_pct: float
    risk: RiskEvaluation | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StrategyControlState:
    """Live control status of one strategy under the failure controls."""

    strategy_id: str
    status: StrategyControlStatus = StrategyControlStatus.ACTIVE
    reasons: tuple[str, ...] = ()
    updated_at: datetime = field(default_factory=_now)
    suspended_until: date | None = None

    def weight_factor(self, protection: DrawdownProtection) -> float:
        if self.status is StrategyControlStatus.MONITORED:
            return protection.monitored_weight_factor
        if self.status is StrategyControlStatus.REDUCED:
            return protection.reduced_weight_factor
        if self.status is StrategyControlStatus.SUSPENDED:
            return 0.0
        return 1.0


@dataclass(frozen=True, slots=True)
class KillSwitchRecord:
    """State of the independent emergency shutdown."""

    state: KillSwitchState = KillSwitchState.ARMED
    triggered_at: datetime | None = None
    triggered_by: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StrategyMonitoringUpdate:
    """One strategy's status transition produced by the monitoring pass."""

    strategy_id: str
    previous: StrategyControlStatus
    current: StrategyControlStatus
    reasons: tuple[str, ...] = ()
    suspended_until: date | None = None


@dataclass(frozen=True, slots=True)
class MonitoringReport:
    """Output of a full monitoring pass."""

    updates: tuple[StrategyMonitoringUpdate, ...]
    kill_switch: KillSwitchRecord
    portfolio_breaches: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClearedOrder:
    """A risk-cleared order ready for the Execution Simulator.

    ``source`` is the strategy/AI proposal that requested it; the risk engine
    and portfolio manager decided the final ``quantity``.
    """

    strategy_id: str
    symbol: str
    side: TradeSide
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    signal_ts: datetime | None = None
    decision: GateDecision | None = None


def _qty(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(_QTY_QUANT)


def _money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(_MONEY_QUANT)


def quantize_qty(value: Decimal) -> Decimal:
    return value.quantize(_QTY_QUANT)


def snapshot_from_state(
    *,
    equity: Decimal | int | float,
    cash: Decimal | int | float,
    gross_exposure_pct: float,
    positions: tuple[Holding, ...] = (),
    positions_count: int | None = None,
    turnover_30d_pct: float = 0.0,
    daily_pnl_pct: float = 0.0,
    drawdown_pct: float = 0.0,
    consecutive_losses: int = 0,
    avg_asset_correlation: float = 0.0,
    leverage_pct: float = 0.0,
) -> PortfolioSnapshot:
    """Build a snapshot with sane defaults (equity/cash normalised to Money)."""
    return PortfolioSnapshot(
        equity=_money(equity),
        cash=_money(cash),
        gross_exposure_pct=gross_exposure_pct,
        positions=positions,
        positions_count=len(positions) if positions_count is None else positions_count,
        turnover_30d_pct=turnover_30d_pct,
        daily_pnl_pct=daily_pnl_pct,
        drawdown_pct=drawdown_pct,
        consecutive_losses=consecutive_losses,
        avg_asset_correlation=avg_asset_correlation,
        leverage_pct=leverage_pct,
    )


def default_controls() -> tuple[PortfolioConstraints, DrawdownProtection, SizingPolicy]:
    """Conservative default controls (all configurable via settings)."""
    return PortfolioConstraints(), DrawdownProtection(), SizingPolicy()


@dataclass(frozen=True, slots=True)
class PortfolioRiskPlan:
    """Everything one risk-engine run needs (settings-driven)."""

    constraints: PortfolioConstraints = field(default_factory=PortfolioConstraints)
    drawdown_protection: DrawdownProtection = field(default_factory=DrawdownProtection)
    sizing: SizingPolicy = field(default_factory=SizingPolicy)
    allocation: AllocationPolicyConfig = field(default_factory=AllocationPolicyConfig)


__all__ = [
    "AllocationPolicyConfig",
    "AllocationReport",
    "ClearedOrder",
    "DrawdownProtection",
    "GateDecision",
    "GateVerdict",
    "Holding",
    "KillSwitchRecord",
    "KillSwitchState",
    "MonitoringReport",
    "PortfolioConstraints",
    "PortfolioRiskPlan",
    "PortfolioSnapshot",
    "PositionSize",
    "PositionSizingMethod",
    "ProposedTrade",
    "RiskEvaluation",
    "SizingPolicy",
    "StrategyControlState",
    "StrategyControlStatus",
    "StrategyMonitoringUpdate",
    "StrategyScore",
    "default_controls",
    "quantize_qty",
    "snapshot_from_state",
]
