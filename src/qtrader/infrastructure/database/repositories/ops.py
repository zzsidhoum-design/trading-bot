"""SQLAlchemy repositories for backtests, strategy performance and system logs."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.entities import BacktestRun, PerformanceSummary, SystemLog
from qtrader.domain.ports import (
    BacktestRepository,
    PerformanceRepository,
    SystemLogRepository,
)
from qtrader.domain.value_objects import Interval, Money, TradingMode
from qtrader.infrastructure.database.models import (
    BacktestRunModel,
    StrategyPerformanceModel,
    SystemLogModel,
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _summary_to_dict(summary: PerformanceSummary) -> dict[str, Any]:
    def num(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    return {
        "strategy": summary.strategy,
        "mode": summary.mode.value,
        "period_start": summary.period_start.isoformat(),
        "period_end": summary.period_end.isoformat(),
        "total_return": num(summary.total_return),
        "cagr": num(summary.cagr),
        "sharpe": num(summary.sharpe),
        "sortino": num(summary.sortino),
        "max_drawdown": num(summary.max_drawdown),
        "win_rate": num(summary.win_rate),
        "profit_factor": num(summary.profit_factor),
        "expectancy": num(summary.expectancy),
        "avg_win": num(summary.avg_win),
        "avg_loss": num(summary.avg_loss),
        "turnover": num(summary.turnover),
        "total_costs": num(summary.total_costs),
        "trades_count": summary.trades_count,
        "final_equity": num(summary.final_equity),
    }


def _summary_from_dict(data: dict[str, Any] | None) -> PerformanceSummary | None:
    if not data:
        return None

    def num(key: str) -> Decimal | None:
        value = data.get(key)
        return Decimal(value) if value is not None else None

    return PerformanceSummary(
        strategy=str(data.get("strategy", "")),
        mode=TradingMode(str(data.get("mode", TradingMode.BACKTEST.value))),
        period_start=date.fromisoformat(str(data.get("period_start", date.today().isoformat()))),
        period_end=date.fromisoformat(str(data.get("period_end", date.today().isoformat()))),
        total_return=num("total_return"),
        cagr=num("cagr"),
        sharpe=num("sharpe"),
        sortino=num("sortino"),
        max_drawdown=num("max_drawdown"),
        win_rate=num("win_rate"),
        profit_factor=num("profit_factor"),
        expectancy=num("expectancy"),
        avg_win=num("avg_win"),
        avg_loss=num("avg_loss"),
        turnover=num("turnover"),
        total_costs=num("total_costs"),
        trades_count=data.get("trades_count"),
        final_equity=num("final_equity"),
    )


class SQLAlchemyBacktestRepository(BacktestRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, run: BacktestRun) -> BacktestRun:
        async with self._session_factory() as session:
            row = BacktestRunModel(
                name=run.name,
                universe=run.universe,
                start=run.start,
                end=run.end,
                initial_capital=run.initial_capital.amount,
                interval=run.interval.value,
                strategy=run.strategy,
                commission_bps=run.commission_bps,
                slippage_bps=run.slippage_bps,
                status=run.status,
            )
            session.add(row)
            await session.commit()
            return replace(run, run_id=row.id)

    async def save(self, run: BacktestRun) -> BacktestRun:
        assert run.run_id is not None
        async with self._session_factory() as session:
            row = await session.get(BacktestRunModel, run.run_id)
            if row is None:
                raise ValueError(f"backtest run {run.run_id} not found")
            row.status = run.status
            row.final_capital = run.final_capital.amount if run.final_capital else None
            row.metrics = _summary_to_dict(run.metrics) if run.metrics else None
            await session.commit()
            return run

    async def get(self, run_id: int) -> BacktestRun | None:
        async with self._session_factory() as session:
            row = await session.get(BacktestRunModel, run_id)
            return self._to_domain(row) if row else None

    async def latest(self, name: str | None = None, limit: int = 5) -> list[BacktestRun]:
        async with self._session_factory() as session:
            stmt = (
                select(BacktestRunModel)
                .order_by(BacktestRunModel.created_at.desc())
                .limit(limit)
            )
            if name is not None:
                stmt = stmt.where(BacktestRunModel.name == name)
            rows = await session.scalars(stmt)
            return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_domain(row: BacktestRunModel) -> BacktestRun:
        return BacktestRun(
            name=row.name,
            universe=row.universe or [],
            start=row.start,
            end=row.end,
            initial_capital=Money(row.initial_capital),
            interval=Interval(row.interval),
            strategy=row.strategy,
            commission_bps=row.commission_bps,
            slippage_bps=row.slippage_bps,
            final_capital=Money(row.final_capital) if row.final_capital is not None else None,
            metrics=_summary_from_dict(row.metrics),
            status=row.status,
            created_at=_utc(row.created_at),
            run_id=row.id,
        )


class SQLAlchemyPerformanceRepository(PerformanceRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert(self, summary: PerformanceSummary) -> PerformanceSummary:
        async with self._session_factory() as session:
            stmt = select(StrategyPerformanceModel).where(
                StrategyPerformanceModel.strategy == summary.strategy,
                StrategyPerformanceModel.mode == summary.mode.value,
                StrategyPerformanceModel.period_start == summary.period_start,
                StrategyPerformanceModel.period_end == summary.period_end,
            )
            row = await session.scalar(stmt)
            if row is None:
                row = StrategyPerformanceModel(
                    strategy=summary.strategy,
                    mode=summary.mode.value,
                    period_start=summary.period_start,
                    period_end=summary.period_end,
                )
                session.add(row)
            self._apply(row, summary)
            await session.commit()
            return summary

    async def latest_for_strategy(
        self, strategy: str, mode: TradingMode
    ) -> PerformanceSummary | None:
        async with self._session_factory() as session:
            stmt = (
                select(StrategyPerformanceModel)
                .where(
                    StrategyPerformanceModel.strategy == strategy,
                    StrategyPerformanceModel.mode == mode.value,
                )
                .order_by(
                    StrategyPerformanceModel.period_end.desc(),
                    StrategyPerformanceModel.id.desc(),
                )
                .limit(1)
            )
            row = await session.scalar(stmt)
            return self._to_domain(row) if row else None

    @staticmethod
    def _apply(row: StrategyPerformanceModel, summary: PerformanceSummary) -> None:
        row.total_return = summary.total_return
        row.sharpe = summary.sharpe
        row.sortino = summary.sortino
        row.max_drawdown = summary.max_drawdown
        row.win_rate = summary.win_rate
        row.profit_factor = summary.profit_factor
        row.trades_count = summary.trades_count
        row.final_equity = summary.final_equity

    @staticmethod
    def _to_domain(row: StrategyPerformanceModel) -> PerformanceSummary:
        return PerformanceSummary(
            strategy=row.strategy,
            mode=TradingMode(row.mode),
            period_start=row.period_start,
            period_end=row.period_end,
            total_return=row.total_return,
            sharpe=row.sharpe,
            sortino=row.sortino,
            max_drawdown=row.max_drawdown,
            win_rate=row.win_rate,
            profit_factor=row.profit_factor,
            trades_count=row.trades_count,
            final_equity=row.final_equity,
        )


class SQLAlchemySystemLogRepository(SystemLogRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, entry: SystemLog) -> SystemLog:
        async with self._session_factory() as session:
            row = SystemLogModel(
                level=entry.level,
                component=entry.component,
                message=entry.message,
                context=entry.context,
            )
            session.add(row)
            await session.commit()
            return replace(entry, log_id=row.id)

    async def recent(
        self, level: str | None = None, component: str | None = None, limit: int = 50
    ) -> list[SystemLog]:
        async with self._session_factory() as session:
            stmt = (
                select(SystemLogModel)
                .order_by(SystemLogModel.created_at.desc())
                .limit(min(limit, 500))
            )
            if level is not None:
                stmt = stmt.where(SystemLogModel.level == level.upper())
            if component is not None:
                stmt = stmt.where(SystemLogModel.component == component)
            rows = await session.scalars(stmt)
            return [
                SystemLog(
                    level=row.level,
                    component=row.component,
                    message=row.message,
                    context=row.context or {},
                    created_at=row.created_at,
                    log_id=row.id,
                )
                for row in rows
            ]


__all__ = [
    "SQLAlchemyBacktestRepository",
    "SQLAlchemyPerformanceRepository",
    "SQLAlchemySystemLogRepository",
]
