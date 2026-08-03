"""Market data & universe router."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from qtrader.domain.entities import Stock
from qtrader.domain.exceptions import NoPriceDataError, NotFoundError, ValidationError
from qtrader.domain.ports import (
    IndicatorRepository,
    NewsRepository,
    PredictionRepository,
    PriceRepository,
    SignalRepository,
    StockRepository,
)
from qtrader.domain.value_objects import Interval, PriceBar
from qtrader.interfaces.api.dependencies import (
    get_indicator_repository,
    get_news_repository,
    get_prediction_repository,
    get_price_repository,
    get_signal_repository,
    get_stock_repository,
    require_api_key,
)
from qtrader.interfaces.api.schemas import (
    IndicatorOut,
    NewsOut,
    PriceBarOut,
    SignalOut,
    StockCreate,
    StockOut,
)

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
        raise NoPriceDataError(f"no price data for symbol {symbol.upper()!r}")
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


@router.post(
    "",
    response_model=StockOut,
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def create_stock(
    body: StockCreate,
    stock_repo: StockRepository = Depends(get_stock_repository),
) -> StockOut:
    symbol = body.symbol.strip().upper()
    if not symbol:
        raise ValidationError("symbol must not be empty")
    stock = Stock(
        symbol=symbol,
        exchange=body.exchange or "PAPER",
        name=body.name or symbol,
        sector=body.sector,
        industry=body.industry,
        is_active=True,
    )
    await stock_repo.upsert(stock)
    return StockOut(
        symbol=stock.symbol,
        exchange=stock.exchange,
        name=stock.name,
        currency=stock.currency,
        sector=stock.sector,
        industry=stock.industry,
        is_active=stock.is_active,
    )


@router.get(
    "/{symbol}/indicators",
    response_model=IndicatorOut,
    dependencies=[Depends(require_api_key)],
)
async def latest_indicators(
    symbol: str,
    interval: Interval = Interval.D1,
    indicator_repo: IndicatorRepository = Depends(get_indicator_repository),
) -> IndicatorOut:
    snapshot = await indicator_repo.latest(symbol.upper(), interval)
    if snapshot is None:
        raise NotFoundError(f"no indicator data for symbol {symbol.upper()!r}")
    values: dict[str, Any] = {
        name: str(value)
        for name, value in snapshot.__dict__.items()
        if value is not None
        and name
        not in {"symbol", "interval", "ts", "volume_profile", "extras", "volume_profile_extras"}
    }
    if snapshot.volume_profile is not None:
        values["volume_profile"] = snapshot.volume_profile
    return IndicatorOut(
        symbol=snapshot.symbol,
        interval=snapshot.interval.value,
        ts=snapshot.ts,
        values=values,
    )


@router.get(
    "/{symbol}/news",
    response_model=list[NewsOut],
    dependencies=[Depends(require_api_key)],
)
async def symbol_news(
    symbol: str,
    since: datetime | None = None,
    limit: int = 25,
    news_repo: NewsRepository = Depends(get_news_repository),
) -> list[NewsOut]:
    items = await news_repo.recent(symbol.upper(), since, min(limit, 100))
    return [
        NewsOut(
            title=item.title,
            source=item.source,
            url=item.url,
            published_at=item.published_at,
            sentiment_score=str(item.sentiment_score) if item.sentiment_score is not None else None,
            summary=item.summary,
            expected_market_impact=item.expected_market_impact,
            impact_direction=item.impact_direction,
        )
        for item in items
    ]


@router.get(
    "/{symbol}/signals",
    response_model=list[SignalOut],
    dependencies=[Depends(require_api_key)],
)
async def symbol_signals(
    symbol: str,
    agent: str | None = None,
    signal_repo: SignalRepository = Depends(get_signal_repository),
    prediction_repo: PredictionRepository = Depends(get_prediction_repository),
) -> list[SignalOut]:
    signals = await signal_repo.latest_for_symbol(symbol.upper(), agent)
    out: list[SignalOut] = [
        SignalOut(
            agent=s.agent,
            signal_type=s.signal_type.value,
            score=str(s.score),
            interval=s.interval.value if s.interval is not None else None,
            created_at=s.created_at,
            metadata=s.metadata,
        )
        for s in signals
    ]
    predictions = await prediction_repo.latest_for_symbol(symbol.upper(), limit=5)
    out.extend(
        SignalOut(
            agent=p.model_name,
            signal_type="prediction",
            score=str(p.prob_up) if p.prob_up is not None else "",
            created_at=p.created_at,
            metadata={
                "horizon": p.horizon,
                "model_version": p.model_version,
                "confidence": str(p.confidence) if p.confidence is not None else None,
                "expected_return": str(p.expected_return)
                if p.expected_return is not None
                else None,
            },
        )
        for p in predictions
    )
    return out


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
