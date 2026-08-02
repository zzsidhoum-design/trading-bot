"""SQLAlchemy repository implementations."""

from qtrader.infrastructure.database.repositories.analysis import (
    SQLAlchemyFundamentalRepository,
    SQLAlchemyIndicatorRepository,
    SQLAlchemyNewsRepository,
    SQLAlchemySignalRepository,
)
from qtrader.infrastructure.database.repositories.event_outbox import SQLAlchemyEventRepository
from qtrader.infrastructure.database.repositories.ops import (
    SQLAlchemyBacktestRepository,
    SQLAlchemyPerformanceRepository,
    SQLAlchemySystemLogRepository,
)
from qtrader.infrastructure.database.repositories.prediction import (
    SQLAlchemyDecisionRepository,
    SQLAlchemyModelRepository,
    SQLAlchemyPredictionRepository,
)
from qtrader.infrastructure.database.repositories.sqlalchemy import (
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyStockRepository,
)
from qtrader.infrastructure.database.repositories.trading import (
    SQLAlchemyOrderRepository,
    SQLAlchemyPositionRepository,
    SQLAlchemyRiskRepository,
    SQLAlchemyTradeRepository,
)

__all__ = [
    "SQLAlchemyBacktestRepository",
    "SQLAlchemyDecisionRepository",
    "SQLAlchemyEventRepository",
    "SQLAlchemyFundamentalRepository",
    "SQLAlchemyIndicatorRepository",
    "SQLAlchemyModelRepository",
    "SQLAlchemyNewsRepository",
    "SQLAlchemyOrderRepository",
    "SQLAlchemyPerformanceRepository",
    "SQLAlchemyPortfolioRepository",
    "SQLAlchemyPositionRepository",
    "SQLAlchemyPredictionRepository",
    "SQLAlchemyPriceRepository",
    "SQLAlchemyRiskRepository",
    "SQLAlchemySignalRepository",
    "SQLAlchemyStockRepository",
    "SQLAlchemySystemLogRepository",
    "SQLAlchemyTradeRepository",
]
