"""SQLAlchemy repositories. Map between domain entities and ORM models.

Each repository opens its own session from an injected session factory, so
transaction boundaries stay explicit and the UnitOfWork lives in the caller.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, bindparam, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.entities import Portfolio, Stock
from qtrader.domain.ports import (
    DataQualityRepository,
    PortfolioRepository,
    PriceRepository,
    StockRepository,
)
from qtrader.domain.value_objects import Interval, Money, PriceBar, TradingMode
from qtrader.infrastructure.database.models import PortfolioModel, PriceModel, StockModel
from qtrader.infrastructure.database.repositories.base import SessionBoundRepo


class SQLAlchemyStockRepository(SessionBoundRepo, StockRepository):
    async def upsert(self, stock: Stock) -> Stock:
        async with self._session_scope() as session:
            result = await session.execute(
                select(StockModel).where(
                    StockModel.symbol == stock.symbol, StockModel.exchange == stock.exchange
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = StockModel(
                    symbol=stock.symbol,
                    exchange=stock.exchange,
                    name=stock.name,
                    currency=stock.currency,
                    sector=stock.sector,
                    industry=stock.industry,
                    is_active=stock.is_active,
                )
                session.add(row)
            else:
                row.name = stock.name or row.name
                row.sector = stock.sector or row.sector
                row.industry = stock.industry or row.industry
                row.is_active = stock.is_active
            await session.flush()
            await self._commit(session)
            return Stock(
                symbol=row.symbol,
                exchange=row.exchange,
                name=row.name,
                currency=row.currency,
                sector=row.sector,
                industry=row.industry,
                is_active=row.is_active,
                stock_id=row.id,
            )

    async def get_by_symbol(self, symbol: str, exchange: str | None = None) -> Stock | None:
        async with self._session_scope() as session:
            query = select(StockModel).where(StockModel.symbol == symbol)
            if exchange is not None:
                query = query.where(StockModel.exchange == exchange)
            row = await session.scalar(query)
            return self._to_domain(row) if row else None

    async def list_active(self) -> list[Stock]:
        async with self._session_scope() as session:
            rows = await session.scalars(select(StockModel).where(StockModel.is_active.is_(True)))
            return [self._to_domain(r) for r in rows]

    async def search(
        self, query: str | None, sector: str | None, limit: int, offset: int
    ) -> list[Stock]:
        async with self._session_scope() as session:
            stmt = select(StockModel).where(StockModel.is_active.is_(True))
            if query:
                stmt = stmt.where(StockModel.symbol.ilike(f"%{query}%"))
            if sector:
                stmt = stmt.where(StockModel.sector == sector)
            rows = await session.scalars(stmt.limit(limit).offset(offset))
            return [self._to_domain(r) for r in rows]

    @staticmethod
    def _to_domain(row: StockModel) -> Stock:
        return Stock(
            symbol=row.symbol,
            exchange=row.exchange,
            name=row.name,
            currency=row.currency,
            sector=row.sector,
            industry=row.industry,
            is_active=row.is_active,
            stock_id=row.id,
        )


class SQLAlchemyPortfolioRepository(SessionBoundRepo, PortfolioRepository):
    async def create(self, portfolio: Portfolio) -> Portfolio:
        async with self._session_scope() as session:
            row = PortfolioModel(
                name=portfolio.name,
                currency=portfolio.currency,
                initial_capital=portfolio.initial_capital.amount,
                current_cash=portfolio.current_cash.amount,
                mode=portfolio.mode,
                status=portfolio.status,
            )
            session.add(row)
            await session.flush()
            await self._commit(session)
            return Portfolio(
                name=row.name,
                currency=row.currency,
                initial_capital=Money(row.initial_capital),
                current_cash=Money(row.current_cash),
                mode=TradingMode(row.mode),
                status=row.status,
                portfolio_id=row.id,
            )

    async def get(self, portfolio_id: int) -> Portfolio | None:
        async with self._session_scope() as session:
            row = await session.get(PortfolioModel, portfolio_id)
            if row is None:
                return None
            return Portfolio(
                name=row.name,
                currency=row.currency,
                initial_capital=Money(row.initial_capital),
                current_cash=Money(row.current_cash),
                mode=TradingMode(row.mode),
                status=row.status,
                portfolio_id=row.id,
            )

    async def first(self) -> Portfolio | None:
        async with self._session_scope() as session:
            row = await session.scalar(
                select(PortfolioModel).order_by(PortfolioModel.id).limit(1)
            )
            if row is None:
                return None
            return Portfolio(
                name=row.name,
                currency=row.currency,
                initial_capital=Money(row.initial_capital),
                current_cash=Money(row.current_cash),
                mode=TradingMode(row.mode),
                status=row.status,
                portfolio_id=row.id,
            )

    async def save(self, portfolio: Portfolio) -> Portfolio:
        assert portfolio.portfolio_id is not None
        async with self._session_scope() as session:
            row = await session.get(PortfolioModel, portfolio.portfolio_id)
            if row is None:
                raise ValueError(f"portfolio {portfolio.portfolio_id} not found")
            row.name = portfolio.name
            row.currency = portfolio.currency
            row.initial_capital = portfolio.initial_capital.amount
            row.current_cash = portfolio.current_cash.amount
            row.mode = portfolio.mode
            row.status = portfolio.status
            await self._commit(session)
            return portfolio


class SQLAlchemyPriceRepository(PriceRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert_bars(self, bars: list[PriceBar]) -> int:
        if not bars:
            return 0
        async with self._session_factory() as session:
            symbols = {b.symbol for b in bars}
            rows = await session.scalars(
                select(StockModel).where(StockModel.symbol.in_(symbols))
            )
            stock_ids: dict[str, int] = {r.symbol: r.id for r in rows}
            missing = symbols - set(stock_ids)
            if missing:
                insert_stocks = (
                    pg_insert(StockModel)
                    .values(
                        [
                            {"symbol": s, "exchange": "YAHOO", "name": s}
                            for s in sorted(missing)
                        ]
                    )
                    .on_conflict_do_nothing(constraint="uq_stocks_symbol_exchange")
                    .returning(StockModel.id, StockModel.symbol)
                )
                created = (await session.execute(insert_stocks)).all()
                stock_ids.update({sym: sid for sid, sym in created})
                if len(created) < len(missing):
                    rest = (await session.scalars(
                        select(StockModel).where(StockModel.symbol.in_(missing))
                    )).all()
                    stock_ids.update({r.symbol: r.id for r in rest})
            payload = [
                {
                    "stock_id": stock_ids[b.symbol],
                    "interval": b.interval,
                    "ts": b.ts,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars
                if b.symbol in stock_ids
            ]
            if not payload:
                return 0
            stmt = (
                pg_insert(PriceModel)
                .values(payload)
                .on_conflict_do_update(
                    constraint="uq_prices_stock_interval_ts",
                    set_={
                        "open": pg_insert(PriceModel).excluded.open,
                        "high": pg_insert(PriceModel).excluded.high,
                        "low": pg_insert(PriceModel).excluded.low,
                        "close": pg_insert(PriceModel).excluded.close,
                        "volume": pg_insert(PriceModel).excluded.volume,
                    },
                )
            )
            result = cast(CursorResult[Any], await session.execute(stmt))
            await session.commit()
            return result.rowcount or 0

    async def latest(self, symbol: str, interval: Interval) -> PriceBar | None:
        async with self._session_factory() as session:
            stock_id = await self._stock_id(session, symbol)
            if stock_id is None:
                return None
            row = await session.scalar(
                select(PriceModel)
                .where(PriceModel.stock_id == stock_id, PriceModel.interval == interval)
                .order_by(PriceModel.ts.desc())
                .limit(1)
            )
            return self._to_bar(row, symbol) if row else None

    async def history(
        self,
        symbol: str,
        interval: Interval,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> list[PriceBar]:
        async with self._session_factory() as session:
            stock_id = await self._stock_id(session, symbol)
            if stock_id is None:
                return []
            stmt = (
                select(PriceModel)
                .where(PriceModel.stock_id == stock_id, PriceModel.interval == interval)
                .order_by(PriceModel.ts)
            )
            if start is not None:
                stmt = stmt.where(PriceModel.ts >= start)
            if end is not None:
                stmt = stmt.where(PriceModel.ts <= end)
            rows = await session.scalars(stmt.limit(limit))
            return [self._to_bar(r, symbol) for r in rows]

    @staticmethod
    async def _stock_id(session: AsyncSession, symbol: str) -> int | None:
        row = await session.scalar(
            select(StockModel.id)
            .where(StockModel.symbol == symbol)
            .order_by(StockModel.is_active.desc(), StockModel.id)
            .limit(1)
        )
        return row

    @staticmethod
    def _to_bar(row: PriceModel, symbol: str) -> PriceBar:
        return PriceBar(
            symbol=symbol,
            interval=Interval(row.interval),
            ts=row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=UTC),
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
        )


_ET = "America/New_York"
_SESSION_WINDOW = "'09:30'::time <= (ts AT TIME ZONE 'America/New_York')::time"
_SESSION_WINDOW += " AND (ts AT TIME ZONE 'America/New_York')::time <= '16:00'::time"


class SQLAlchemyDataQualityRepository(DataQualityRepository):
    """Read-only aggregates over the persisted price universe."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def price_audit(self, *, watchlist: list[str]) -> dict[str, Any]:
        async with self._session_factory() as session:
            result: dict[str, Any] = {}
            result["intervals"] = [
                {
                    "interval": r.interval,
                    "rows": r.rows,
                    "symbols": r.symbols,
                    "first_ts": r.first_ts,
                    "last_ts": r.last_ts,
                }
                for r in await session.execute(
                    text(
                        "SELECT interval, COUNT(*) AS rows, COUNT(DISTINCT stock_id) AS symbols,"
                        " MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM prices"
                        " GROUP BY interval ORDER BY interval"
                    )
                )
            ]
            result["duplicates"] = await self._count(
                session,
                "SELECT COUNT(*) FROM (SELECT 1 FROM prices GROUP BY stock_id, interval, ts"
                " HAVING COUNT(*) > 1) d",
            )
            result["invalid_ohlc"] = await self._count(
                session,
                "SELECT COUNT(*) FROM prices WHERE high < low"
                " OR high < LEAST(open, close) OR low > GREATEST(open, close)",
            )
            result["non_positive"] = await self._count(
                session,
                "SELECT COUNT(*) FROM prices"
                " WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0",
            )
            result["zero_volume"] = await self._count(
                session, "SELECT COUNT(*) FROM prices WHERE volume = 0"
            )
            result["misaligned_intraday"] = await self._count(
                session,
                "SELECT COUNT(*) FROM prices WHERE interval IN ('1m', '5m', '15m', '1h')"
                " AND (EXTRACT(SECOND FROM ts) <> 0"
                " OR (interval = '5m' AND EXTRACT(MINUTE FROM ts)::int % 5 <> 0)"
                " OR (interval = '15m' AND EXTRACT(MINUTE FROM ts)::int % 15 <> 0)"
                " OR (interval = '1h' AND EXTRACT(MINUTE FROM ts) <> 0))",
            )
            result["weekend_d1"] = await self._count(
                session,
                f"SELECT COUNT(*) FROM prices WHERE interval = '1d'"
                f" AND EXTRACT(DOW FROM ts AT TIME ZONE '{_ET}') IN (0, 6)",
            )
            result["off_session_intraday"] = await self._count(
                session,
                f"SELECT COUNT(*) FROM prices WHERE interval <> '1d' AND NOT ({_SESSION_WINDOW})",
            )
            result["future_bars"] = await self._count(
                session,
                "SELECT COUNT(*) FROM prices WHERE ts > :cutoff",
                {"cutoff": datetime.now(UTC) + timedelta(minutes=2)},
            )

            symbols = list(dict.fromkeys(s.upper() for s in watchlist))
            if symbols:
                result["freshness"] = [
                    {
                        "symbol": r.symbol,
                        "interval": r.interval,
                        "last_ts": r.last_ts,
                        "age_seconds": (
                            datetime.now(UTC) - _as_utc(r.last_ts)
                        ).total_seconds(),
                    }
                    for r in await session.execute(
                        text(
                            "SELECT s.symbol, p.interval, MAX(p.ts) AS last_ts"
                            " FROM prices p JOIN stocks s ON s.id = p.stock_id"
                            " WHERE p.interval IN ('1d', '5m') AND s.symbol IN :symbols"
                            " GROUP BY s.symbol, p.interval"
                        ).bindparams(bindparam("symbols", expanding=True)),
                        {"symbols": symbols},
                    )
                ]
                m5_start = datetime.now(UTC) - timedelta(days=14)
                result["m5_per_day"] = [
                    {"symbol": r.symbol, "day": r.day, "bars": r.bars}
                    for r in await session.execute(
                        text(
                            f"SELECT s.symbol,"
                            f" (p.ts AT TIME ZONE '{_ET}')::date AS day, COUNT(*) AS bars"
                            " FROM prices p JOIN stocks s ON s.id = p.stock_id"
                            " WHERE p.interval = '5m' AND s.symbol IN :symbols"
                            " AND p.ts >= :start GROUP BY s.symbol, day"
                        ).bindparams(bindparam("symbols", expanding=True)),
                        {"symbols": symbols, "start": m5_start},
                    )
                ]
                result["d1_per_day"] = [
                    {"symbol": r.symbol, "day": r.day}
                    for r in await session.execute(
                        text(
                            f"SELECT s.symbol, (p.ts AT TIME ZONE '{_ET}')::date AS day"
                            " FROM prices p JOIN stocks s ON s.id = p.stock_id"
                            " WHERE p.interval = '1d' AND s.symbol IN :symbols"
                            " AND p.ts >= :start GROUP BY s.symbol, day"
                        ).bindparams(bindparam("symbols", expanding=True)),
                        {
                            "symbols": symbols,
                            "start": datetime.now(UTC) - timedelta(days=60),
                        },
                    )
                ]
                result["d1_m5_diff"] = await self._d1_m5_diff(session, symbols)
            else:
                result["freshness"] = []
                result["m5_per_day"] = []
                result["d1_per_day"] = []
                result["d1_m5_diff"] = None
            return result

    @staticmethod
    async def _count(
        session: AsyncSession, sql: str, params: dict[str, Any] | None = None
    ) -> int:
        row = await session.execute(text(sql), params or {})
        return int(row.scalar_one())

    @staticmethod
    async def _d1_m5_diff(
        session: AsyncSession, symbols: list[str]
    ) -> dict[str, Any] | None:
        row = (
            await session.execute(
                text(
                    f"WITH d1 AS (SELECT p.stock_id, s.symbol,"
                    f" (p.ts AT TIME ZONE '{_ET}')::date AS day, p.close AS close"
                    " FROM prices p JOIN stocks s ON s.id = p.stock_id"
                    " WHERE p.interval = '1d' AND s.symbol IN :symbols AND p.ts >= :start),"
                    " m5 AS (SELECT DISTINCT ON (p.stock_id,"
                    f" (p.ts AT TIME ZONE '{_ET}')::date)"
                    f" p.stock_id, (p.ts AT TIME ZONE '{_ET}')::date AS day, p.close AS close"
                    " FROM prices p WHERE p.interval = '5m' AND p.ts >= :start"
                    f" ORDER BY p.stock_id, (p.ts AT TIME ZONE '{_ET}')::date, p.ts DESC)"
                    " SELECT d1.symbol, d1.day, d1.close AS d1_close,"
                    " m5.close AS m5_close,"
                    " ABS(d1.close - m5.close) / NULLIF(d1.close, 0) * 100.0 AS diff_pct"
                    " FROM d1 JOIN m5 ON m5.stock_id = d1.stock_id AND m5.day = d1.day"
                    " WHERE d1.close > 0"
                    " ORDER BY diff_pct DESC NULLS LAST LIMIT 1"
                ).bindparams(bindparam("symbols", expanding=True)),
                {
                    "symbols": symbols,
                    "start": datetime.now(UTC) - timedelta(days=15),
                },
            )
        ).first()
        if row is None:
            return None
        return {
            "symbol": row.symbol,
            "day": row.day,
            "d1_close": float(row.d1_close),
            "m5_close": float(row.m5_close),
            "diff_pct": float(row.diff_pct) if row.diff_pct is not None else None,
        }


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
