"""All ORM models. Import this module before using ``Base.metadata`` (Alembic)."""

from __future__ import annotations

from qtrader.infrastructure.database.base import Base
from qtrader.infrastructure.database.models.market import EarningModel, FundamentalModel, NewsModel
from qtrader.infrastructure.database.models.ops import (
    AgentMetricModel,
    BacktestRunModel,
    EventRecordModel,
    ModelRegistryModel,
    StrategyPerformanceModel,
    SystemLogModel,
)
from qtrader.infrastructure.database.models.signal import (
    DecisionLogModel,
    PredictionModel,
    RiskHistoryModel,
    SignalModel,
)
from qtrader.infrastructure.database.models.stock import IndicatorModel, PriceModel, StockModel
from qtrader.infrastructure.database.models.trading import (
    OrderModel,
    PortfolioModel,
    PositionModel,
    TradeModel,
)

__all__ = [
    "AgentMetricModel",
    "BacktestRunModel",
    "Base",
    "DecisionLogModel",
    "EarningModel",
    "EventRecordModel",
    "FundamentalModel",
    "IndicatorModel",
    "ModelRegistryModel",
    "NewsModel",
    "OrderModel",
    "PortfolioModel",
    "PositionModel",
    "PredictionModel",
    "PriceModel",
    "RiskHistoryModel",
    "SignalModel",
    "StockModel",
    "StrategyPerformanceModel",
    "SystemLogModel",
    "TradeModel",
]
