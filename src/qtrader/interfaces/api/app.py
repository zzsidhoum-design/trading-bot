"""FastAPI application — composition root for the HTTP/WS interface.

Lifespan owns the process-wide DI container (engine pool, redis, event bus).
Routers resolve dependencies through ``get_container``. The static dashboard
SPA is served from ``/`` via ``StaticFiles`` (mounted last so API routes win).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from qtrader.config.container import get_container, shutdown_container
from qtrader.config.logging import LoggingMiddleware
from qtrader.domain.exceptions import QtraderError
from qtrader.interfaces.api.routers import (
    agents,
    backtest,
    dashboard,
    models,
    portfolio,
    stocks,
    system,
)
from qtrader.interfaces.api.ws import router as ws_router

_DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_container()
    yield
    await shutdown_container()


def _error_response(request: Request, exc: Exception) -> JSONResponse:
    error = cast(QtraderError, exc)
    return JSONResponse(
        status_code=error.http_status,
        content={"error": error.code, "detail": error.message},
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="qtrader API",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
        docs_url="/docs",
    )
    app.add_middleware(LoggingMiddleware)
    app.add_exception_handler(QtraderError, _error_response)
    app.include_router(system.router)
    app.include_router(stocks.router)
    app.include_router(portfolio.router)
    app.include_router(dashboard.router)
    app.include_router(backtest.router)
    app.include_router(models.router)
    app.include_router(agents.router)
    app.include_router(ws_router)
    app.mount("/", StaticFiles(directory=_DASHBOARD_DIR, html=True), name="dashboard")
    return app


app = create_app()
