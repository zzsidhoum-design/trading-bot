"""Realistic execution simulator and market-microstructure-aware backtesting.

Phase 4: answers whether Phase-3-VALIDATED strategies survive realistic
execution (spread, slippage, order types, latency, partial fills, rejections,
gaps, trading hours, liquidity, transaction costs). Everything here is driven
by explicit, auditable assumptions — there is no live bid/ask feed.
"""

from qtrader.application.execution.backtest import (
    ExecutionAwareBacktestRunner,
    ExecutionBroker,
)
from qtrader.application.execution.costs import TransactionCostModel
from qtrader.application.execution.engine import (
    ExecutionRequest,
    StrategyExecutionEngine,
)
from qtrader.application.execution.liquidity import LiquidityModel
from qtrader.application.execution.metrics import (
    classify_execution,
    compute_execution_metrics,
)
from qtrader.application.execution.models import (
    ExecutionMetrics,
    ExecutionOrder,
    ExecutionPlan,
    ExecutionRunReport,
    ExecutionScenario,
    ExecutionStats,
    ExecutionStatus,
    LiquidityAssessment,
    LiquidityAssumptions,
    ScenarioResult,
    SlippageAssumptions,
    StrategyExecutionReport,
    TradingHoursPolicy,
    default_slippage_assumptions,
    encode_execution_report,
)
from qtrader.application.execution.simulator import ExecutionSimulator
from qtrader.application.execution.slippage import SlippageModel

__all__ = [
    "ExecutionAwareBacktestRunner",
    "ExecutionBroker",
    "ExecutionMetrics",
    "ExecutionOrder",
    "ExecutionPlan",
    "ExecutionRequest",
    "ExecutionRunReport",
    "ExecutionScenario",
    "ExecutionSimulator",
    "ExecutionStats",
    "ExecutionStatus",
    "LiquidityAssessment",
    "LiquidityAssumptions",
    "LiquidityModel",
    "ScenarioResult",
    "SlippageAssumptions",
    "SlippageModel",
    "StrategyExecutionEngine",
    "StrategyExecutionReport",
    "TradingHoursPolicy",
    "TransactionCostModel",
    "classify_execution",
    "compute_execution_metrics",
    "default_slippage_assumptions",
    "encode_execution_report",
]
