"""SQLAlchemy repository for the dynamic trading universe."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qtrader.domain.entities import (
    AssetType,
    SymbolChange,
    TradingStatus,
    UniverseMembership,
    UniverseTier,
)
from qtrader.domain.ports import UniverseRepository
from qtrader.infrastructure.database.models.universe import (
    SymbolChangeModel,
    UniverseMembershipModel,
)
from qtrader.infrastructure.database.repositories.base import SessionBoundRepo


class SQLAlchemyUniverseRepository(SessionBoundRepo, UniverseRepository):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        super().__init__(session_factory, session=session)

    async def list_memberships(
        self, status: TradingStatus | None = None
    ) -> list[UniverseMembership]:
        async with self._session_scope() as session:
            stmt = select(UniverseMembershipModel).order_by(UniverseMembershipModel.symbol)
            if status is not None:
                stmt = stmt.where(UniverseMembershipModel.status == status.value)
            rows = await session.scalars(stmt)
            return [self._to_domain(r) for r in rows]

    async def get_membership(self, symbol: str) -> UniverseMembership | None:
        async with self._session_scope() as session:
            row = await session.scalar(
                select(UniverseMembershipModel).where(UniverseMembershipModel.symbol == symbol)
            )
            return self._to_domain(row) if row else None

    async def upsert_membership(self, membership: UniverseMembership) -> UniverseMembership:
        async with self._session_scope() as session:
            row = await session.scalar(
                select(UniverseMembershipModel).where(
                    UniverseMembershipModel.symbol == membership.symbol
                )
            )
            if row is None:
                row = UniverseMembershipModel(
                    symbol=membership.symbol,
                    status=membership.status.value,
                    tier=membership.tier.value if membership.tier else None,
                    added_at=membership.added_at,
                    removed_at=membership.removed_at,
                    last_traded_at=membership.last_traded_at,
                    asset_type=membership.asset_type.value,
                    name=membership.name,
                    reason=membership.reason,
                    extras=membership.metadata,
                )
                session.add(row)
            else:
                row.status = membership.status.value
                row.tier = membership.tier.value if membership.tier else None
                row.added_at = membership.added_at
                row.removed_at = membership.removed_at
                row.last_traded_at = membership.last_traded_at
                row.asset_type = membership.asset_type.value
                row.name = membership.name
                row.reason = membership.reason
                row.extras = membership.metadata
            await self._commit(session)
            await session.refresh(row)
            return self._to_domain(row)

    async def record_symbol_change(self, change: SymbolChange) -> SymbolChange:
        async with self._session_scope() as session:
            row = SymbolChangeModel(
                old_symbol=change.old_symbol,
                new_symbol=change.new_symbol,
                effective_at=change.effective_at,
                reason=change.reason,
            )
            session.add(row)
            await self._commit(session)
            await session.refresh(row)
            return SymbolChange(
                old_symbol=row.old_symbol,
                new_symbol=row.new_symbol,
                effective_at=row.effective_at,
                reason=row.reason,
                change_id=row.id,
            )

    async def list_symbol_changes(self) -> list[SymbolChange]:
        async with self._session_scope() as session:
            rows = await session.scalars(
                select(SymbolChangeModel).order_by(SymbolChangeModel.id)
            )
            return [
                SymbolChange(
                    old_symbol=r.old_symbol,
                    new_symbol=r.new_symbol,
                    effective_at=r.effective_at,
                    reason=r.reason,
                    change_id=r.id,
                )
                for r in rows
            ]

    @staticmethod
    def _to_domain(row: UniverseMembershipModel) -> UniverseMembership:
        return UniverseMembership(
            symbol=row.symbol,
            status=TradingStatus(row.status),
            tier=UniverseTier(row.tier) if row.tier else None,
            added_at=row.added_at,
            removed_at=row.removed_at,
            last_traded_at=row.last_traded_at,
            asset_type=AssetType(row.asset_type) if row.asset_type else AssetType.COMMON_STOCK,
            name=row.name,
            reason=row.reason,
            metadata=dict(row.extras or {}),
            membership_id=row.id,
        )
