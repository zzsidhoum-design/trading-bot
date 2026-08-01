"""SQLAlchemy repository implementations."""

from qtrader.infrastructure.database.repositories.analysis import (
    SQLAlchemyFundamentalRepository,
    SQLAlchemyIndicatorRepository,
    SQLAlchemyNewsRepository,
    SQLAlchemySignalRepository,
)
from qtrader.infrastructure.database.repositories.event_outbox import SQLAlchemyEventRepository
from qtrader.infrastructure.database.repositories.sqlalchemy import (
    SQLAlchemyPortfolioRepository,
    SQLAlchemyPriceRepository,
    SQLAlchemyStockRepository,
)

__all__ = [
    "SQLAlchemyEventRepository",
    "SQLAlchemyFundamentalRepository",
    "SQLAlchemyIndicatorRepository",
    "SQLAlchemyNewsRepository",
    "SQLAlchemyPortfolioRepository",
    "SQLAlchemyPriceRepository",
    "SQLAlchemySignalRepository",
    "SQLAlchemyStockRepository",
]
