"""REST & WebSocket routers."""

from __future__ import annotations

from qtrader.interfaces.api.routers import (
    agents,
    backtest,
    dashboard,
    models,
    portfolio,
    stocks,
    system,
)

__all__ = ["agents", "backtest", "dashboard", "models", "portfolio", "stocks", "system"]
