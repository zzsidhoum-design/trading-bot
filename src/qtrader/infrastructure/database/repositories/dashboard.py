"""SQLAlchemy read-side adapter for the dashboard (Phase 7).

Implements :class:`DashboardQueries` with dedicated read queries so dashboard
routes never touch ORM models directly. Entities are mapped the same way as
their write-side repositories (``trading.py`` / ``ops.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.entities import (
    AgentMetric,
    Position,
    RegisteredModel,
    SystemLog,
    Trade,
)
from qtrader.domain.ports import AgentMetricRepository, DashboardQueries
from qtrader.domain.value_objects import (
    Money,
    PositionStatus,
    TradeSide,
    TradingMode,
)
from qtrader.infrastructure.database.models import (
    AgentMetricModel,
    ModelRegistryModel,
    PositionModel,
    StockModel,
    StrategyPerformanceModel,
    SystemLogModel,
    TradeModel,
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SQLAlchemyDashboardRepository(DashboardQueries, AgentMetricRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, metric: AgentMetric) -> AgentMetric:
        async with self._session_factory() as session:
            row = AgentMetricModel(
                agent_name=metric.agent_name,
                metric_name=metric.metric_name,
                value=metric.value,
                window=metric.window,
            )
            session.add(row)
            await session.commit()
            return AgentMetric(
                agent_name=metric.agent_name,
                metric_name=metric.metric_name,
                value=metric.value,
                window=metric.window,
                computed_at=metric.computed_at,
                metric_id=row.id,
            )

    async def positions(self, portfolio_id: int) -> list[Position]:
        async with self._session_factory() as session:
            stmt = (
                select(PositionModel, StockModel.symbol)
                .join(StockModel, PositionModel.stock_id == StockModel.id)
                .where(PositionModel.portfolio_id == portfolio_id)
                .order_by(PositionModel.opened_at.desc())
            )
            rows = await session.execute(stmt)
            return [self._position(row, symbol) for row, symbol in rows]

    async def trades(
        self, portfolio_id: int, since: datetime | None = None, limit: int = 100
    ) -> list[Trade]:
        async with self._session_factory() as session:
            stmt = (
                select(TradeModel, StockModel.symbol)
                .join(StockModel, TradeModel.stock_id == StockModel.id)
                .where(TradeModel.portfolio_id == portfolio_id)
                .order_by(TradeModel.exit_time.desc())
                .limit(min(limit, 500))
            )
            if since is not None:
                stmt = stmt.where(TradeModel.exit_time >= since)
            rows = await session.execute(stmt)
            return [self._trade(row, symbol) for row, symbol in rows]

    async def logs(
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
            return [self._log(row) for row in rows]

    async def agent_metrics(
        self, agent_name: str | None = None, limit: int = 50
    ) -> list[AgentMetric]:
        async with self._session_factory() as session:
            stmt = (
                select(AgentMetricModel)
                .order_by(AgentMetricModel.computed_at.desc())
                .limit(min(limit, 500))
            )
            if agent_name is not None:
                stmt = stmt.where(AgentMetricModel.agent_name == agent_name)
            rows = await session.scalars(stmt)
            return [self._metric(row) for row in rows]

    async def performance(
        self, strategy: str | None = None, mode: TradingMode | None = None, limit: int = 50
    ) -> list[Any]:
        async with self._session_factory() as session:
            stmt = (
                select(StrategyPerformanceModel)
                .order_by(StrategyPerformanceModel.period_end.desc())
                .limit(min(limit, 500))
            )
            if strategy is not None:
                stmt = stmt.where(StrategyPerformanceModel.strategy == strategy)
            if mode is not None:
                stmt = stmt.where(StrategyPerformanceModel.mode == mode.value)
            rows = await session.scalars(stmt)
            return [self._performance(row) for row in rows]

    async def models(self) -> list[RegisteredModel]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(ModelRegistryModel).order_by(
                    ModelRegistryModel.name, ModelRegistryModel.version
                )
            )
            return [
                RegisteredModel(
                    name=row.name,
                    version=row.version,
                    artifact_path=row.artifact_path,
                    hyperparams=row.hyperparams or {},
                    offline_metrics=row.offline_metrics or {},
                    is_active=row.is_active,
                    status=row.status,
                    trained_at=row.trained_at,
                    training_window=row.training_window,
                    model_id=row.id,
                )
                for row in rows
            ]

    @staticmethod
    def _position(row: PositionModel, symbol: str) -> Position:
        return Position(
            portfolio_id=row.portfolio_id,
            stock_id=row.stock_id,
            quantity=int(row.quantity),
            avg_entry_price=Money(row.avg_entry_price),
            status=PositionStatus(row.status),
            stop_loss=Money(row.stop_loss) if row.stop_loss is not None else None,
            take_profit=Money(row.take_profit) if row.take_profit is not None else None,
            realized_pnl=Money(row.realized_pnl) if row.realized_pnl is not None else None,
            opened_at=_utc(row.opened_at),
            closed_at=_utc(row.closed_at) if row.closed_at else None,
            symbol=symbol,
            position_id=row.id,
        )

    @staticmethod
    def _trade(row: TradeModel, symbol: str) -> Trade:
        return Trade(
            portfolio_id=row.portfolio_id,
            stock_id=row.stock_id,
            symbol=symbol,
            strategy=row.strategy,
            side=TradeSide(row.side),
            quantity=row.quantity,
            entry_price=row.entry_price,
            exit_price=row.exit_price,
            pnl=row.pnl,
            pnl_pct=row.pnl_pct,
            fees=row.fees,
            entry_time=_utc(row.entry_time),
            exit_time=_utc(row.exit_time),
            decision_reason=row.decision_reason_json,
            outcome=row.outcome,
            mode=TradingMode(row.mode),
            position_id=row.position_id,
            trade_id=row.id,
        )

    @staticmethod
    def _log(row: SystemLogModel) -> SystemLog:
        return SystemLog(
            level=row.level,
            message=row.message,
            component=row.component,
            context=row.context or {},
            created_at=_utc(row.created_at),
            log_id=row.id,
        )

    @staticmethod
    def _metric(row: AgentMetricModel) -> AgentMetric:
        return AgentMetric(
            agent_name=row.agent_name,
            metric_name=row.metric_name,
            value=row.value,
            window=row.window,
            computed_at=_utc(row.computed_at),
            metric_id=row.id,
        )

    @staticmethod
    def _performance(row: StrategyPerformanceModel) -> Any:
        from qtrader.domain.entities import PerformanceSummary

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
