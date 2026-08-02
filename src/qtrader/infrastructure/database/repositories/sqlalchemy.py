"""SQLAlchemy repositories. Map between domain entities and ORM models.

Each repository opens its own session from an injected session factory, so
transaction boundaries stay explicit and the UnitOfWork lives in the caller.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.entities import Portfolio, Stock
from qtrader.domain.ports import PortfolioRepository, PriceRepository, StockRepository
from qtrader.domain.value_objects import Interval, Money, PriceBar, TradingMode
from qtrader.infrastructure.database.models import PortfolioModel, PriceModel, StockModel


class SQLAlchemyStockRepository(StockRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert(self, stock: Stock) -> Stock:
        async with self._session_factory() as session:
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
            await session.commit()
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
        async with self._session_factory() as session:
            query = select(StockModel).where(StockModel.symbol == symbol)
            if exchange is not None:
                query = query.where(StockModel.exchange == exchange)
            row = await session.scalar(query)
            return self._to_domain(row) if row else None

    async def list_active(self) -> list[Stock]:
        async with self._session_factory() as session:
            rows = await session.scalars(select(StockModel).where(StockModel.is_active.is_(True)))
            return [self._to_domain(r) for r in rows]

    async def search(
        self, query: str | None, sector: str | None, limit: int, offset: int
    ) -> list[Stock]:
        async with self._session_factory() as session:
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


class SQLAlchemyPortfolioRepository(PortfolioRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, portfolio: Portfolio) -> Portfolio:
        async with self._session_factory() as session:
            row = PortfolioModel(
                name=portfolio.name,
                currency=portfolio.currency,
                initial_capital=portfolio.initial_capital.amount,
                current_cash=portfolio.current_cash.amount,
                mode=portfolio.mode,
                status=portfolio.status,
            )
            session.add(row)
            await session.commit()
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
        async with self._session_factory() as session:
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
        async with self._session_factory() as session:
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
        async with self._session_factory() as session:
            row = await session.get(PortfolioModel, portfolio.portfolio_id)
            if row is None:
                raise ValueError(f"portfolio {portfolio.portfolio_id} not found")
            row.name = portfolio.name
            row.currency = portfolio.currency
            row.initial_capital = portfolio.initial_capital.amount
            row.current_cash = portfolio.current_cash.amount
            row.mode = portfolio.mode
            row.status = portfolio.status
            await session.commit()
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
                .on_conflict_do_nothing(constraint="uq_prices_stock_interval_ts")
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
            select(StockModel.id).where(StockModel.symbol == symbol).limit(1)
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
