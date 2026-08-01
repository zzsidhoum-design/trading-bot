"""SQLAlchemy repository implementations."""

from qtrader.infrastructure.database.repositories.analysis import (
    SQLAlchemyFundamentalRepository,
    SQLAlchemyIndicatorRepository,
    SQLAlchemyNewsRepository,
    SQLAlchemySignalRepository,
)
from qtrader.infrastructure.database.repositories.event_outbox import SQLAlchemyEventRepository
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

__all__ = [
    "SQLAlchemyDecisionRepository",
    "SQLAlchemyEventRepository",
    "SQLAlchemyFundamentalRepository",
    "SQLAlchemyIndicatorRepository",
    "SQLAlchemyModelRepository",
    "SQLAlchemyNewsRepository",
    "SQLAlchemyPortfolioRepository",
    "SQLAlchemyPredictionRepository",
    "SQLAlchemyPriceRepository",
    "SQLAlchemySignalRepository",
    "SQLAlchemyStockRepository",
]
