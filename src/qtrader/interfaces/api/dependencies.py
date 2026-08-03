"""FastAPI dependencies — container access, repositories, auth guard."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import Depends, Header, HTTPException, status

from qtrader.application.services.backtest import BacktestRunner
from qtrader.application.services.dashboard_service import DashboardService
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.application.use_cases.manual_order import ManualOrder
from qtrader.config.container import Container
from qtrader.config.container import get_container as _shared_container
from qtrader.config.settings import Settings
from qtrader.domain.ports import (
    BacktestRepository,
    EventRepository,
    IndicatorRepository,
    ModelRepository,
    NewsRepository,
    OrderRepository,
    PortfolioRepository,
    PredictionRepository,
    PriceRepository,
    RiskRepository,
    SignalRepository,
    StockRepository,
    SystemLogRepository,
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


def get_order_repository(container: Container = Depends(get_container)) -> OrderRepository:
    return container.resolve(OrderRepository)


def get_risk_repository(container: Container = Depends(get_container)) -> RiskRepository:
    return container.resolve(RiskRepository)


def get_event_repository(container: Container = Depends(get_container)) -> EventRepository:
    return container.resolve(EventRepository)


def get_system_log_repository(
    container: Container = Depends(get_container),
) -> SystemLogRepository:
    return container.resolve(SystemLogRepository)


def get_indicator_repository(
    container: Container = Depends(get_container),
) -> IndicatorRepository:
    return container.resolve(IndicatorRepository)


def get_signal_repository(container: Container = Depends(get_container)) -> SignalRepository:
    return container.resolve(SignalRepository)


def get_news_repository(container: Container = Depends(get_container)) -> NewsRepository:
    return container.resolve(NewsRepository)


def get_prediction_repository(
    container: Container = Depends(get_container),
) -> PredictionRepository:
    return container.resolve(PredictionRepository)


def get_backtest_repository(
    container: Container = Depends(get_container),
) -> BacktestRepository:
    return container.resolve(BacktestRepository)


def get_backtest_runner(container: Container = Depends(get_container)) -> BacktestRunner:
    return container.resolve(BacktestRunner)


def get_model_repository(container: Container = Depends(get_container)) -> ModelRepository:
    return container.resolve(ModelRepository)


def get_dashboard_service(
    container: Container = Depends(get_container),
) -> DashboardService:
    return container.resolve(DashboardService)


def get_portfolio_service(
    container: Container = Depends(get_container),
) -> PortfolioService:
    return container.resolve(PortfolioService)


def get_manual_order(container: Container = Depends(get_container)) -> ManualOrder:
    return container.resolve(ManualOrder)


EnqueueJob = Callable[[str], Awaitable[str | None]]


def get_enqueue_job(settings: Settings = Depends(get_settings)) -> EnqueueJob:
    """Provider for the ``/system/run`` control: enqueues an arq worker job.

    Override in tests with a fake to assert the requested cycle name without
    requiring a live Redis/worker.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    async def _enqueue(job_name: str) -> str | None:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        try:
            job = await pool.enqueue_job(job_name)
        finally:
            await pool.aclose()
        return job.job_id if job is not None else None

    return _enqueue


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject requests without the configured API key.

    Uses a constant-time comparison so a remote attacker cannot measure how
    many leading characters of the key they guessed correctly.
    """
    if settings.api_key == "change-me" or x_api_key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
    if not secrets.compare_digest(x_api_key.encode(), settings.api_key.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
