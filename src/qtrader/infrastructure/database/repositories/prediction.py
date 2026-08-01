"""SQLAlchemy repositories for prediction, decision log & model registry."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.entities import DecisionRecord, Prediction, RegisteredModel
from qtrader.domain.ports import DecisionRepository, ModelRepository, PredictionRepository
from qtrader.domain.value_objects import Decision
from qtrader.infrastructure.database.models import (
    DecisionLogModel,
    ModelRegistryModel,
    PredictionModel,
    StockModel,
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SQLAlchemyPredictionRepository(PredictionRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, prediction: Prediction) -> Prediction:
        async with self._session_factory() as session:
            stock_id = await self._stock_id(session, prediction.symbol)
            if stock_id is None:
                raise ValueError(f"unknown symbol {prediction.symbol!r}")
            row = PredictionModel(
                stock_id=stock_id,
                model_name=prediction.model_name,
                model_version=prediction.model_version,
                horizon=prediction.horizon,
                prob_up=prediction.prob_up,
                prob_down=prediction.prob_down,
                prob_trend=prediction.prob_trend,
                confidence=prediction.confidence,
                expected_return=prediction.expected_return,
                expected_volatility=prediction.expected_volatility,
                features_hash=prediction.features_hash,
                created_at=prediction.created_at,
            )
            session.add(row)
            await session.commit()
            return self._to_domain(row, prediction.symbol)

    async def latest_for_symbol(self, symbol: str, limit: int = 20) -> list[Prediction]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(PredictionModel, StockModel.symbol)
                .join(StockModel, StockModel.id == PredictionModel.stock_id)
                .where(StockModel.symbol == symbol)
                .order_by(PredictionModel.created_at.desc())
                .limit(limit)
            )
            return [self._to_domain(row, stock_symbol) for row, stock_symbol in rows]

    @staticmethod
    def _to_domain(row: PredictionModel, symbol: str) -> Prediction:
        return Prediction(
            symbol=symbol,
            model_name=row.model_name,
            model_version=int(row.model_version),
            horizon=row.horizon,
            prob_up=row.prob_up,
            prob_down=row.prob_down,
            prob_trend=row.prob_trend,
            confidence=row.confidence,
            expected_return=row.expected_return,
            expected_volatility=row.expected_volatility,
            features_hash=row.features_hash,
            created_at=_aware(row.created_at),
            prediction_id=row.id,
        )

    @staticmethod
    async def _stock_id(session: AsyncSession, symbol: str) -> int | None:
        stock_id: int | None = await session.scalar(
            select(StockModel.id).where(StockModel.symbol == symbol).limit(1)
        )
        return stock_id


class SQLAlchemyDecisionRepository(DecisionRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, record: DecisionRecord) -> DecisionRecord:
        async with self._session_factory() as session:
            stock_id = await self._stock_id(session, record.symbol)
            if stock_id is None:
                raise ValueError(f"unknown symbol {record.symbol!r}")
            payload = {
                "decision_uuid": record.decision_uuid,
                "stock_id": stock_id,
                "decision": record.decision.value,
                "confidence": record.confidence,
                "rationale": record.rationale,
                "agent_scores": record.agent_scores,
                "created_at": record.created_at,
            }
            stmt = (
                pg_insert(DecisionLogModel)
                .values(payload)
                .on_conflict_do_nothing(constraint="uq_decision_log_decision_uuid")
            )
            await session.execute(stmt)
            await session.commit()
            return record

    async def latest_for_symbol(self, symbol: str, limit: int = 20) -> list[DecisionRecord]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(DecisionLogModel, StockModel.symbol)
                .join(StockModel, StockModel.id == DecisionLogModel.stock_id)
                .where(StockModel.symbol == symbol)
                .order_by(DecisionLogModel.created_at.desc())
                .limit(limit)
            )
            return [self._to_domain(row, stock_symbol) for row, stock_symbol in rows]

    @staticmethod
    def _to_domain(row: DecisionLogModel, symbol: str) -> DecisionRecord:
        return DecisionRecord(
            decision_uuid=row.decision_uuid,
            symbol=symbol,
            decision=Decision(row.decision),
            confidence=row.confidence or Decimal("0"),
            rationale=row.rationale or "",
            agent_scores=row.agent_scores or {},
            created_at=_aware(row.created_at),
            decision_id=row.id,
        )

    @staticmethod
    async def _stock_id(session: AsyncSession, symbol: str) -> int | None:
        stock_id: int | None = await session.scalar(
            select(StockModel.id).where(StockModel.symbol == symbol).limit(1)
        )
        return stock_id


class SQLAlchemyModelRepository(ModelRepository):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_active(self, name: str) -> RegisteredModel | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ModelRegistryModel)
                .where(ModelRegistryModel.name == name, ModelRegistryModel.is_active.is_(True))
                .order_by(ModelRegistryModel.version.desc())
                .limit(1)
            )
            return self._to_domain(row) if row else None

    async def create_version(
        self,
        name: str,
        hyperparams: dict[str, Any],
        training_window: str | None,
        offline_metrics: dict[str, Any],
    ) -> int:
        async with self._session_factory() as session:
            current = await session.scalar(
                select(func.max(ModelRegistryModel.version)).where(ModelRegistryModel.name == name)
            )
            version = (int(current) if current is not None else 0) + 1
            row = ModelRegistryModel(
                name=name,
                version=version,
                hyperparams=hyperparams,
                training_window=training_window,
                offline_metrics=offline_metrics,
                is_active=False,
                status="registered",
                trained_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            return version

    async def promote(self, name: str, version: int) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(ModelRegistryModel)
                .where(ModelRegistryModel.name == name)
                .values(is_active=False)
            )
            await session.execute(
                update(ModelRegistryModel)
                .where(ModelRegistryModel.name == name, ModelRegistryModel.version == version)
                .values(is_active=True)
            )
            await session.commit()

    @staticmethod
    def _to_domain(row: ModelRegistryModel) -> RegisteredModel:
        return RegisteredModel(
            name=row.name,
            version=int(row.version),
            artifact_path=row.artifact_path,
            hyperparams=row.hyperparams or {},
            offline_metrics=row.offline_metrics or {},
            is_active=row.is_active,
            status=row.status,
            trained_at=_aware(row.trained_at) if row.trained_at else None,
            training_window=row.training_window,
            model_id=row.id,
        )
