"""FastAPI application — composition root for the HTTP/WS interface.

Lifespan owns the process-wide DI container (engine pool, redis, event bus).
Routers resolve dependencies through ``get_container``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from qtrader.config.container import get_container
from qtrader.interfaces.api.routers import portfolio, stocks, system
from qtrader.interfaces.api.ws import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = get_container()
    yield
    await container.aclose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="qtrader API",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
        docs_url="/docs",
    )
    app.include_router(system.router)
    app.include_router(stocks.router)
    app.include_router(portfolio.router)
    app.include_router(ws_router)
    return app


app = create_app()
