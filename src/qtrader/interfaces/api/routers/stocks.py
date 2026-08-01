"""Market data & universe router."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from qtrader.domain.ports import PriceRepository, StockRepository
from qtrader.domain.value_objects import Interval, PriceBar
from qtrader.interfaces.api.dependencies import (
    get_price_repository,
    get_stock_repository,
    require_api_key,
)
from qtrader.interfaces.api.schemas import PriceBarOut, StockOut

router = APIRouter(prefix="/api/v1/stocks", tags=["stocks"])


@router.get(
    "",
    response_model=list[StockOut],
    dependencies=[Depends(require_api_key)],
)
async def search_stocks(
    q: str | None = None,
    sector: str | None = None,
    limit: int = 50,
    offset: int = 0,
    stock_repo: StockRepository = Depends(get_stock_repository),
) -> list[StockOut]:
    stocks = await stock_repo.search(q, sector, min(limit, 200), max(offset, 0))
    return [
        StockOut(
            symbol=s.symbol,
            exchange=s.exchange,
            name=s.name,
            currency=s.currency,
            sector=s.sector,
            industry=s.industry,
            is_active=s.is_active,
        )
        for s in stocks
    ]


@router.get(
    "/{symbol}/price",
    response_model=PriceBarOut,
    dependencies=[Depends(require_api_key)],
)
async def latest_price(
    symbol: str,
    interval: Interval = Interval.M5,
    price_repo: PriceRepository = Depends(get_price_repository),
) -> PriceBarOut:
    bar = await price_repo.latest(symbol.upper(), interval)
    if bar is None:
        raise HTTPException(status_code=404, detail="no price data for symbol")
    return _bar_out(bar)


@router.get(
    "/{symbol}/history",
    response_model=list[PriceBarOut],
    dependencies=[Depends(require_api_key)],
)
async def price_history(
    symbol: str,
    interval: Interval = Interval.D1,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 200,
    price_repo: PriceRepository = Depends(get_price_repository),
) -> list[PriceBarOut]:
    bars = await price_repo.history(symbol.upper(), interval, from_, to, min(limit, 1000))
    return [_bar_out(b) for b in bars]


def _bar_out(bar: PriceBar) -> PriceBarOut:
    return PriceBarOut(
        symbol=bar.symbol,
        interval=bar.interval,
        ts=bar.ts,
        open=str(bar.open),
        high=str(bar.high),
        low=str(bar.low),
        close=str(bar.close),
        volume=str(bar.volume),
    )
