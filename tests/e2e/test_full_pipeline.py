r"""End-to-end: the full agent pipeline on real infrastructure, mocked external feeds.

Builds the PRODUCTION container (real Postgres/Redis, real outbox event bus,
real repositories, PaperBroker, SystemGate, all agents + bus wiring) and
replaces only the network adapters (market data, news, LLM) with deterministic
in-memory fakes via ``Container(overrides=...)``.

Drives one complete order lifecycle with zero orchestration code:

    DataAgent.backfill -> BackfillCompleted -> scan -> technical/news/
    fundamental/prediction analysis -> chief decision -> risk -> portfolio
    allocation -> execution -> OrderFilled (position + portfolio cash move).

Run: docker compose up -d, then
    $env:QTRADER_RUN_INTEGRATION=1; .\.venv\Scripts\python.exe -m pytest tests/e2e -v
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from qtrader.application.agents.data import DataAgent
from qtrader.application.services.news_analysis import NewsAnalysis
from qtrader.application.services.portfolio_service import PortfolioService
from qtrader.config.container import Container
from qtrader.config.settings import Settings
from qtrader.domain.entities import (
    FundamentalData,
    IndicatorSnapshot,
    NewsItem,
    PerformanceSummary,
    Stock,
)
from qtrader.domain.ports import (
    EventRepository,
    FundamentalRepository,
    IndicatorRepository,
    LLMClient,
    MarketDataProvider,
    NewsProvider,
    OrderRepository,
    PerformanceRepository,
    PortfolioRepository,
    PositionRepository,
    PriceRepository,
    StockRepository,
)
from qtrader.domain.value_objects import Interval, PriceBar, TradingMode
from qtrader.infrastructure.database.models import (
    DecisionLogModel,
    EventRecordModel,
    FundamentalModel,
    IndicatorModel,
    NewsModel,
    OrderModel,
    PortfolioModel,
    PositionModel,
    PredictionModel,
    PriceModel,
    RiskHistoryModel,
    SignalModel,
    StockModel,
    StrategyPerformanceModel,
    SystemLogModel,
    TradeModel,
)

pytestmark = pytest.mark.e2e

SYMBOL = "TSTE2E"
BAR_COUNT = 100
BASE_PRICE = Decimal("100.00")


def _uptrend_bars() -> list[PriceBar]:
    """100 M5 bars climbing 100 -> 110 with steady 1M volume."""
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=5 * BAR_COUNT)
    bars: list[PriceBar] = []
    for i in range(BAR_COUNT):
        close = BASE_PRICE + Decimal(i) * Decimal("0.10")
        bars.append(
            PriceBar(
                symbol=SYMBOL,
                interval=Interval.M5,
                ts=start + timedelta(minutes=5 * i),
                open=close,
                high=close * Decimal("1.01"),
                low=close * Decimal("0.99"),
                close=close,
                volume=Decimal("1000000"),
            )
        )
    return bars


class FakeMarketDataProvider(MarketDataProvider):
    """Deterministic uptrend feed; close() satisfies container teardown."""

    async def fetch_bars(
        self, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> list[PriceBar]:
        return _uptrend_bars()

    async def fetch_quote(self, symbol: str) -> PriceBar:
        return _uptrend_bars()[-1]

    async def close(self) -> None:
        pass


class FakeNewsProvider(NewsProvider):
    async def close(self) -> None:
        pass

    async def fetch_news(
        self, symbol: str | None, since: datetime, limit: int
    ) -> list[NewsItem]:
        now = datetime.now(UTC)
        return [
            NewsItem(
                symbol=symbol,
                source="e2e-feed",
                title="E2E beats expectations and raises guidance",
                url=f"https://e2e.test/{symbol}/1",
                published_at=now - timedelta(minutes=30),
            ),
            NewsItem(
                symbol=symbol,
                source="e2e-feed",
                title="E2E signs major partnership deal",
                url=f"https://e2e.test/{symbol}/2",
                published_at=now - timedelta(minutes=15),
            ),
        ]


class FixedLLMClient(LLMClient):
    """Always-positive news analysis, fully deterministic."""

    async def complete_json(
        self, system_prompt: str, user_prompt: str, schema: type[NewsAnalysis]
    ) -> NewsAnalysis:
        return NewsAnalysis(
            sentiment_score=0.8,
            summary="Strong results and raised guidance.",
            expected_market_impact="HIGH",
            impact_direction=1,
            relevant_symbols=[SYMBOL],
            categories=["earnings"],
            confidence=0.9,
        )


async def _wipe(session_factory: async_sessionmaker) -> None:
    """Delete all E2E artifacts, FK-safe order (children before parents)."""
    async with session_factory() as session:
        await session.execute(delete(TradeModel))
        await session.execute(delete(PositionModel))
        await session.execute(delete(OrderModel))
        await session.execute(delete(RiskHistoryModel))
        await session.execute(delete(SignalModel))
        await session.execute(delete(PredictionModel))
        await session.execute(delete(DecisionLogModel))
        await session.execute(delete(FundamentalModel))
        await session.execute(delete(NewsModel))
        rows = await session.scalars(select(StockModel).where(StockModel.symbol == SYMBOL))
        stock_ids = [r.id for r in rows]
        if stock_ids:
            await session.execute(delete(PriceModel).where(PriceModel.stock_id.in_(stock_ids)))
            await session.execute(
                delete(IndicatorModel).where(IndicatorModel.stock_id.in_(stock_ids))
            )
        await session.execute(delete(StockModel).where(StockModel.symbol == SYMBOL))
        await session.execute(delete(PortfolioModel))
        await session.execute(delete(EventRecordModel))
        await session.execute(delete(StrategyPerformanceModel))
        await session.execute(delete(SystemLogModel))
        await session.commit()


async def _seed(container: Container) -> None:
    """Stock + D1 price/ATR (risk inputs), fresh fundamentals, gate backtest."""
    stocks = container.resolve(StockRepository)
    prices = container.resolve(PriceRepository)
    performance = container.resolve(PerformanceRepository)
    fundamentals = container.resolve(FundamentalRepository)
    await stocks.upsert(
        Stock(symbol=SYMBOL, exchange="XNAS", name="E2E Corp", sector="Technology")
    )
    now = datetime.now(UTC)
    await prices.upsert_bars(
        [
            PriceBar(
                symbol=SYMBOL,
                interval=Interval.D1,
                ts=now - timedelta(hours=1),
                open=BASE_PRICE,
                high=BASE_PRICE * Decimal("1.02"),
                low=BASE_PRICE * Decimal("0.98"),
                close=BASE_PRICE,
                volume=Decimal("2000000"),
            )
        ]
    )
    await container.resolve(IndicatorRepository).save_snapshot(
        IndicatorSnapshot(
            symbol=SYMBOL,
            interval=Interval.D1,
            ts=now,
            rsi=Decimal("62"),
            atr=Decimal("2.00"),
        )
    )
    await fundamentals.upsert(
        FundamentalData(
            symbol=SYMBOL,
            period="quarter",
            report_date=date.today(),
            revenue_growth=Decimal("0.30"),
            earnings_growth=Decimal("0.25"),
            gross_margin=Decimal("0.45"),
            operating_margin=Decimal("0.20"),
            net_margin=Decimal("0.15"),
            roe=Decimal("0.20"),
            roa=Decimal("0.10"),
            pe_ratio=Decimal("15"),
        )
    )
    await performance.upsert(
        PerformanceSummary(
            strategy="ensemble",
            mode=TradingMode.BACKTEST,
            period_start=date.today() - timedelta(days=30),
            period_end=date.today(),
            total_return=Decimal("0.10"),
            sharpe=Decimal("1.5"),
            sortino=Decimal("2.0"),
            max_drawdown=Decimal("-0.08"),
            win_rate=Decimal("0.55"),
            profit_factor=Decimal("1.6"),
            trades_count=30,
            final_equity=Decimal("110000"),
        )
    )


@pytest.mark.asyncio
async def test_full_pipeline_backtest_to_fill() -> None:
    """Scan -> analyze -> decide -> risk -> allocate -> execute -> fill."""
    settings = Settings(_env_file=None, openai_api_key="")
    container = Container(
        settings,
        overrides={
            MarketDataProvider: FakeMarketDataProvider,
            NewsProvider: FakeNewsProvider,
            LLMClient: FixedLLMClient,
        },
    )
    session_factory = container.resolve(async_sessionmaker)
    await _wipe(session_factory)
    try:
        await _seed(container)
        portfolio_service = container.resolve(PortfolioService)
        portfolio = await portfolio_service.default_portfolio()
        portfolio_id = portfolio.portfolio_id
        assert portfolio_id is not None
        assert portfolio.mode is TradingMode.PAPER  # live disabled -> paper gate

        data = container.resolve(DataAgent)
        start = datetime.now(UTC) - timedelta(minutes=5 * BAR_COUNT)
        inserted = await data.backfill(SYMBOL, Interval.M5, start, datetime.now(UTC))
        assert inserted == BAR_COUNT

        events = await container.resolve(EventRepository).list_after(None, None, 500)
        types = {e.type_name for e in events}
        assert "BackfillCompleted" in types
        assert "ScanCompleted" in types
        assert "TechnicalSignalGenerated" in types
        assert "NewsSignalGenerated" in types
        assert "FundamentalSignalGenerated" in types
        assert "PredictionGenerated" in types
        assert "DecisionMade" in types
        assert "RiskApproved" in types
        assert "RiskRejected" not in types
        assert "AllocationProposal" in types
        assert "OrderSubmitted" in types
        assert "OrderFilled" in types

        orders = await container.resolve(OrderRepository).list_by_portfolio(portfolio_id)
        filled = [o for o in orders if o.symbol == SYMBOL and o.status.value == "FILLED"]
        assert filled, "expected at least one FILLED BUY order for TSTE2E"

        positions = await container.resolve(PositionRepository).open_positions(portfolio_id)
        assert len(positions) == 1
        assert positions[0].symbol == SYMBOL
        assert positions[0].quantity > 0

        portfolio_after = await container.resolve(PortfolioRepository).get(portfolio_id)
        assert portfolio_after is not None
        assert portfolio_after.current_cash.amount < portfolio.current_cash.amount
    finally:
        await container.aclose()
        await _wipe(session_factory)
