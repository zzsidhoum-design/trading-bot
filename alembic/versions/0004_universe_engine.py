"""Dynamic trading universe tables.

Adds ``universe_memberships`` (current membership + listing/removal dates for
point-in-time reconstruction) and ``universe_symbol_changes`` (ticker rename
audit trail).

Revision ID: 0004_universe_engine
Revises: 0003_backtest_runs
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_universe_engine"
down_revision = "0003_backtest_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "universe_memberships",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("tier", sa.String(length=1), nullable=True),
        sa.Column("added_at", sa.Date(), nullable=True),
        sa.Column("removed_at", sa.Date(), nullable=True),
        sa.Column("last_traded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "asset_type",
            sa.String(length=24),
            nullable=False,
            server_default="common_stock",
        ),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("extras", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", name="uq_universe_memberships_symbol"),
    )
    op.create_index(
        "ix_universe_memberships_status", "universe_memberships", ["status"], unique=False
    )
    op.create_index(
        "ix_universe_memberships_removed_at",
        "universe_memberships",
        ["removed_at"],
        unique=False,
    )

    op.create_table(
        "universe_symbol_changes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("old_symbol", sa.String(length=16), nullable=False),
        sa.Column("new_symbol", sa.String(length=16), nullable=False),
        sa.Column("effective_at", sa.Date(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_universe_symbol_changes_old", "universe_symbol_changes", ["old_symbol"], unique=False
    )
    op.create_index(
        "ix_universe_symbol_changes_new", "universe_symbol_changes", ["new_symbol"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_universe_symbol_changes_new", table_name="universe_symbol_changes")
    op.drop_index("ix_universe_symbol_changes_old", table_name="universe_symbol_changes")
    op.drop_table("universe_symbol_changes")
    op.drop_index("ix_universe_memberships_removed_at", table_name="universe_memberships")
    op.drop_index("ix_universe_memberships_status", table_name="universe_memberships")
    op.drop_table("universe_memberships")
