"""FastAPI dependencies — container access, repositories, auth guard."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from qtrader.config.container import Container
from qtrader.config.container import get_container as _shared_container
from qtrader.config.settings import Settings
from qtrader.domain.ports import (
    EventRepository,
    PortfolioRepository,
    PriceRepository,
    StockRepository,
)


def get_container() -> Container:
    """Process-wide container (engine, redis, repos are singletons)."""
    return _shared_container()


def get_settings(container: Container = Depends(get_container)) -> Settings:
    return container.resolve(Settings)


def get_stock_repository(container: Container = Depends(get_container)) -> StockRepository:
    return container.resolve(StockRepository)


def get_price_repository(container: Container = Depends(get_container)) -> PriceRepository:
    return container.resolve(PriceRepository)


def get_portfolio_repository(container: Container = Depends(get_container)) -> PortfolioRepository:
    return container.resolve(PortfolioRepository)


def get_event_repository(container: Container = Depends(get_container)) -> EventRepository:
    return container.resolve(EventRepository)


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject requests without the configured API key."""
    if settings.api_key == "change-me" or x_api_key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
