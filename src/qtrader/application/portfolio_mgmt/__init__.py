"""Phase 5 — independent Portfolio & Risk Management Engine.

The engine is a standalone, agent-independent layer that gates every proposed
trade before it reaches execution. AI/strategy output enters only as
:class:`ProposedTrade` data; risk controls are authoritative and cannot be
bypassed by position limits, exposure limits, drawdown limits, data-quality
checks, execution constraints or the kill switch.

Pipeline (see also :mod:`qtrader.application.portfolio_mgmt.manager`)::

    Strategy / AI Decision
            ↓
    Portfolio Manager
            ↓
    Risk Engine
            ↓
    Execution Simulator
            ↓
    Execution
"""

from __future__ import annotations

from qtrader.application.portfolio_mgmt.adapters import PortfolioRiskAdapter
from qtrader.application.portfolio_mgmt.allocation import StrategyAllocator
from qtrader.application.portfolio_mgmt.constraints import ConstraintEngine, ConstraintVerdict
from qtrader.application.portfolio_mgmt.correlation import (
    CorrelationProvider,
    average_strategy_correlation,
    concentration_index,
    correlated_exposure,
    portfolio_concentration,
    proposed_correlated_exposure,
    proposed_sector_exposure,
    sector_exposures,
    strategy_correlation,
)
from qtrader.application.portfolio_mgmt.drawdown import (
    DrawdownGuard,
    DrawdownState,
    DrawdownTracker,
    KillSwitch,
)
from qtrader.application.portfolio_mgmt.engine import (
    LiquidityChecker,
    PortfolioRiskEngine,
    make_liquidity_checker,
)
from qtrader.application.portfolio_mgmt.manager import PortfolioManager
from qtrader.application.portfolio_mgmt.metrics import (
    annualized_return,
    average_correlation,
    compute_risk_metrics,
    expected_shortfall,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    value_at_risk,
    volatility,
)
from qtrader.application.portfolio_mgmt.models import (
    AllocationPolicyConfig,
    AllocationReport,
    ClearedOrder,
    DrawdownProtection,
    GateDecision,
    GateVerdict,
    Holding,
    KillSwitchRecord,
    KillSwitchState,
    MonitoringReport,
    PortfolioConstraints,
    PortfolioRiskPlan,
    PortfolioSnapshot,
    PositionSize,
    PositionSizingMethod,
    ProposedTrade,
    RiskEvaluation,
    SizingPolicy,
    StrategyControlState,
    StrategyControlStatus,
    StrategyMonitoringUpdate,
    StrategyScore,
    default_controls,
    quantize_qty,
    snapshot_from_state,
)
from qtrader.application.portfolio_mgmt.sizing import (
    FixedAllocationSizer,
    MaxExposureSizer,
    PositionSizer,
    RiskBudgetSizer,
    SizeInput,
    VolatilitySizer,
    apply_control_weights,
    sizer_for,
)

__all__ = [
    "AllocationPolicyConfig",
    "AllocationReport",
    "ClearedOrder",
    "ConstraintEngine",
    "ConstraintVerdict",
    "CorrelationProvider",
    "DrawdownGuard",
    "DrawdownProtection",
    "DrawdownState",
    "DrawdownTracker",
    "FixedAllocationSizer",
    "GateDecision",
    "GateVerdict",
    "Holding",
    "KillSwitch",
    "KillSwitchRecord",
    "KillSwitchState",
    "LiquidityChecker",
    "MaxExposureSizer",
    "MonitoringReport",
    "PortfolioConstraints",
    "PortfolioManager",
    "PortfolioRiskAdapter",
    "PortfolioRiskEngine",
    "PortfolioRiskPlan",
    "PortfolioSnapshot",
    "PositionSize",
    "PositionSizingMethod",
    "PositionSizer",
    "ProposedTrade",
    "RiskBudgetSizer",
    "RiskEvaluation",
    "SizeInput",
    "SizingPolicy",
    "StrategyAllocator",
    "StrategyControlState",
    "StrategyControlStatus",
    "StrategyMonitoringUpdate",
    "StrategyScore",
    "VolatilitySizer",
    "annualized_return",
    "apply_control_weights",
    "average_correlation",
    "average_strategy_correlation",
    "compute_risk_metrics",
    "concentration_index",
    "correlated_exposure",
    "default_controls",
    "expected_shortfall",
    "make_liquidity_checker",
    "max_drawdown",
    "portfolio_concentration",
    "proposed_correlated_exposure",
    "proposed_sector_exposure",
    "quantize_qty",
    "sector_exposures",
    "sharpe_ratio",
    "sizer_for",
    "snapshot_from_state",
    "sortino_ratio",
    "strategy_correlation",
    "value_at_risk",
    "volatility",
]
