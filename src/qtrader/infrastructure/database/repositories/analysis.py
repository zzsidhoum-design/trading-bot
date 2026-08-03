"""SQLAlchemy repositories for analysis outputs (signals, indicators, news, fundamentals)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.entities import FundamentalData, IndicatorSnapshot, NewsItem, Signal
from qtrader.domain.ports import (
    FundamentalRepository,
    IndicatorRepository,
    NewsRepository,
    SignalRepository,
)
from qtrader.domain.value_objects import Interval, SignalType
from qtrader.infrastructure.database.models import (
    FundamentalModel,
    IndicatorModel,
    NewsModel,
    SignalModel,
    StockModel,
)


class SQLAlchemySignalRepository(SignalRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, signal: Signal) -> Signal:
        async with self._session_factory() as session:
            stock_id = await self._stock_id(session, signal.symbol)
            if stock_id is None:
                raise ValueError(f"unknown symbol {signal.symbol!r}")
            row = SignalModel(
                stock_id=stock_id,
                agent=signal.agent,
                interval=signal.interval.value if signal.interval else None,
                signal_type=signal.signal_type,
                score=signal.score,
                strength=signal.strength,
                horizon=signal.horizon,
                metadata_json=signal.metadata,
                created_at=signal.created_at,
            )
            session.add(row)
            await session.commit()
            return Signal(
                symbol=signal.symbol,
                agent=row.agent,
                signal_type=signal.signal_type,
                score=row.score,
                interval=Interval(row.interval) if row.interval else None,
                strength=row.strength,
                horizon=row.horizon,
                metadata=row.metadata_json or {},
                created_at=row.created_at,
                signal_id=row.id,
            )

    async def latest_for_symbol(self, symbol: str, agent: str | None = None) -> list[Signal]:
        async with self._session_factory() as session:
            stmt = (
                select(SignalModel, StockModel.symbol)
                .join(StockModel, StockModel.id == SignalModel.stock_id)
                .where(StockModel.symbol == symbol)
                .order_by(SignalModel.created_at.desc())
            )
            if agent is not None:
                stmt = stmt.where(SignalModel.agent == agent)
            rows = await session.execute(stmt.limit(50))
            return [
                Signal(
                    symbol=stock_symbol,
                    agent=row.agent,
                    signal_type=SignalType(row.signal_type),
                    score=row.score,
                    interval=Interval(row.interval) if row.interval else None,
                    strength=row.strength,
                    horizon=row.horizon,
                    metadata=row.metadata_json or {},
                    created_at=row.created_at,
                    signal_id=row.id,
                )
                for row, stock_symbol in rows
            ]

    @staticmethod
    async def _stock_id(session: AsyncSession, symbol: str) -> int | None:
        stock_id: int | None = await session.scalar(
            select(StockModel.id).where(StockModel.symbol == symbol).limit(1)
        )
        return stock_id


class SQLAlchemyIndicatorRepository(IndicatorRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_snapshot(self, snapshot: IndicatorSnapshot) -> None:
        async with self._session_factory() as session:
            stock_id = await self._stock_id(session, snapshot.symbol)
            if stock_id is None:
                raise ValueError(f"unknown symbol {snapshot.symbol!r}")
            payload = self._payload(stock_id, snapshot)
            stmt = (
                pg_insert(IndicatorModel)
                .values(payload)
                .on_conflict_do_update(
                    constraint="uq_indicators_stock_interval_ts",
                    set_={
                        key: value
                        for key, value in payload.items()
                        if key not in ("stock_id", "interval", "ts")
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def latest(self, symbol: str, interval: Interval) -> IndicatorSnapshot | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(IndicatorModel)
                .join(StockModel, StockModel.id == IndicatorModel.stock_id)
                .where(
                    StockModel.symbol == symbol,
                    IndicatorModel.interval == interval.value,
                )
                .order_by(IndicatorModel.ts.desc())
                .limit(1)
            )
            return self._to_domain(row, symbol) if row else None

    @staticmethod
    def _payload(stock_id: int, snapshot: IndicatorSnapshot) -> dict[str, Any]:
        return {
            "stock_id": stock_id,
            "interval": snapshot.interval.value,
            "ts": snapshot.ts,
            "rsi": snapshot.rsi,
            "ema_9": snapshot.ema_9,
            "ema_21": snapshot.ema_21,
            "sma_50": snapshot.sma_50,
            "sma_200": snapshot.sma_200,
            "macd": snapshot.macd,
            "macd_signal": snapshot.macd_signal,
            "macd_hist": snapshot.macd_hist,
            "atr": snapshot.atr,
            "vwap": snapshot.vwap,
            "boll_upper": snapshot.boll_upper,
            "boll_middle": snapshot.boll_middle,
            "boll_lower": snapshot.boll_lower,
            "adx": snapshot.adx,
            "stoch_k": snapshot.stoch_k,
            "stoch_d": snapshot.stoch_d,
            "ichimoku_tenkan": snapshot.ichimoku_tenkan,
            "ichimoku_kijun": snapshot.ichimoku_kijun,
            "ichimoku_senkou_a": snapshot.ichimoku_senkou_a,
            "ichimoku_senkou_b": snapshot.ichimoku_senkou_b,
            "ichimoku_chikou": snapshot.ichimoku_chikou,
            "volume_profile": snapshot.volume_profile,
            "extras": snapshot.extras,
        }

    @staticmethod
    def _to_domain(row: IndicatorModel, symbol: str = "") -> IndicatorSnapshot:
        return IndicatorSnapshot(
            symbol=symbol,
            interval=Interval(row.interval),
            ts=row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=UTC),
            rsi=row.rsi,
            ema_9=row.ema_9,
            ema_21=row.ema_21,
            sma_50=row.sma_50,
            sma_200=row.sma_200,
            macd=row.macd,
            macd_signal=row.macd_signal,
            macd_hist=row.macd_hist,
            atr=row.atr,
            vwap=row.vwap,
            boll_upper=row.boll_upper,
            boll_middle=row.boll_middle,
            boll_lower=row.boll_lower,
            adx=row.adx,
            stoch_k=row.stoch_k,
            stoch_d=row.stoch_d,
            ichimoku_tenkan=row.ichimoku_tenkan,
            ichimoku_kijun=row.ichimoku_kijun,
            ichimoku_senkou_a=row.ichimoku_senkou_a,
            ichimoku_senkou_b=row.ichimoku_senkou_b,
            ichimoku_chikou=row.ichimoku_chikou,
            volume_profile=row.volume_profile,
            extras=row.extras or {},
        )

    @staticmethod
    async def _stock_id(session: AsyncSession, symbol: str) -> int | None:
        stock_id: int | None = await session.scalar(
            select(StockModel.id).where(StockModel.symbol == symbol).limit(1)
        )
        return stock_id


class SQLAlchemyNewsRepository(NewsRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert(self, items: list[NewsItem]) -> int:
        if not items:
            return 0
        async with self._session_factory() as session:
            symbols = {i.symbol for i in items if i.symbol}
            stock_ids: dict[str, int] = {}
            if symbols:
                rows = await session.scalars(
                    select(StockModel).where(StockModel.symbol.in_(symbols))
                )
                stock_ids = {r.symbol: r.id for r in rows}
            payload = []
            for item in items:
                payload.append(
                    {
                        "stock_id": stock_ids.get(item.symbol) if item.symbol else None,
                        "source": item.source,
                        "title": item.title,
                        "url": item.url,
                        "published_at": item.published_at,
                        "content": item.content,
                        "categories": item.categories,
                        "sentiment_score": item.sentiment_score,
                        "summary": item.summary,
                        "expected_market_impact": item.expected_market_impact,
                        "impact_direction": item.impact_direction,
                        "analysis_confidence": item.analysis_confidence,
                        "analyzed_at": item.analyzed_at,
                        "metadata_json": item.metadata,
                    }
                )
            excluded = pg_insert(NewsModel).excluded
            stmt = (
                pg_insert(NewsModel)
                .values(payload)
                .on_conflict_do_update(
                    constraint="uq_news_url",
                    set_={
                        "stock_id": excluded.stock_id,
                        "sentiment_score": excluded.sentiment_score,
                        "summary": excluded.summary,
                        "expected_market_impact": excluded.expected_market_impact,
                        "impact_direction": excluded.impact_direction,
                        "analysis_confidence": excluded.analysis_confidence,
                        "analyzed_at": excluded.analyzed_at,
                    },
                )
            )
            result = cast(CursorResult[Any], await session.execute(stmt))
            await session.commit()
            return result.rowcount or 0

    async def recent(
        self, symbol: str | None, since: datetime | None, limit: int
    ) -> list[NewsItem]:
        async with self._session_factory() as session:
            stmt = select(NewsModel)
            if since is not None:
                stmt = stmt.where(NewsModel.published_at >= since)
            if symbol is not None:
                stmt = stmt.join(StockModel, StockModel.id == NewsModel.stock_id).where(
                    StockModel.symbol == symbol
                )
            rows = await session.scalars(stmt.order_by(NewsModel.published_at.desc()).limit(limit))
            return [self._to_domain(r) for r in rows]

    @staticmethod
    def _to_domain(row: NewsModel) -> NewsItem:
        return NewsItem(
            symbol=None,
            source=row.source,
            title=row.title,
            url=row.url,
            published_at=row.published_at,
            content=row.content,
            categories=row.categories,
            sentiment_score=row.sentiment_score,
            summary=row.summary,
            expected_market_impact=row.expected_market_impact,
            impact_direction=row.impact_direction,
            analysis_confidence=row.analysis_confidence,
            analyzed_at=row.analyzed_at,
            metadata=row.metadata_json or {},
            news_id=row.id,
        )


class SQLAlchemyFundamentalRepository(FundamentalRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert(self, data: FundamentalData) -> FundamentalData:
        async with self._session_factory() as session:
            stock_id = await self._stock_id(session, data.symbol)
            if stock_id is None:
                raise ValueError(f"unknown symbol {data.symbol!r}")
            payload = {
                "stock_id": stock_id,
                "period": data.period,
                "report_date": data.report_date,
                "revenue": data.revenue,
                "eps": data.eps,
                "pe_ratio": data.pe_ratio,
                "debt_total": data.debt_total,
                "cash_flow": data.cash_flow,
                "roe": data.roe,
                "roa": data.roa,
                "gross_margin": data.gross_margin,
                "operating_margin": data.operating_margin,
                "net_margin": data.net_margin,
                "revenue_growth": data.revenue_growth,
                "earnings_growth": data.earnings_growth,
                "price_to_book": data.price_to_book,
            }
            stmt = (
                pg_insert(FundamentalModel)
                .values(payload)
                .on_conflict_do_update(
                    constraint="uq_fundamentals_stock_period",
                    set_={
                        key: value
                        for key, value in payload.items()
                        if key not in ("stock_id", "period")
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()
            return data

    async def latest(self, symbol: str) -> FundamentalData | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(FundamentalModel)
                .join(StockModel, StockModel.id == FundamentalModel.stock_id)
                .where(StockModel.symbol == symbol)
                .order_by(FundamentalModel.period.desc())
                .limit(1)
            )
            return self._to_domain(row, symbol) if row else None

    @staticmethod
    def _to_domain(row: FundamentalModel, symbol: str = "") -> FundamentalData:
        return FundamentalData(
            symbol=symbol,
            period=row.period,
            report_date=row.report_date,
            revenue=row.revenue,
            eps=row.eps,
            pe_ratio=row.pe_ratio,
            debt_total=row.debt_total,
            cash_flow=row.cash_flow,
            roe=row.roe,
            roa=row.roa,
            gross_margin=row.gross_margin,
            operating_margin=row.operating_margin,
            net_margin=row.net_margin,
            revenue_growth=row.revenue_growth,
            earnings_growth=row.earnings_growth,
            price_to_book=row.price_to_book,
            fundamental_id=row.id,
        )

    @staticmethod
    async def _stock_id(session: AsyncSession, symbol: str) -> int | None:
        stock_id: int | None = await session.scalar(
            select(StockModel.id).where(StockModel.symbol == symbol).limit(1)
        )
        return stock_id
