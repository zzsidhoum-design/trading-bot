"""Backtest run parameters: interval / strategy / execution assumptions.

Revision ID: 0003_backtest_runs
Revises: 0002_orders_brackets
Create Date: 2026-08-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_backtest_runs"
down_revision = "0002_orders_brackets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backtest_runs",
        sa.Column("interval", sa.String(8), nullable=False, server_default="1d"),
    )
    op.add_column(
        "backtest_runs",
        sa.Column("strategy", sa.String(64), nullable=False, server_default="ensemble"),
    )
    op.add_column(
        "backtest_runs",
        sa.Column("commission_bps", sa.Numeric(10, 4), nullable=False, server_default="0"),
    )
    op.add_column(
        "backtest_runs",
        sa.Column("slippage_bps", sa.Numeric(10, 4), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("backtest_runs", "slippage_bps")
    op.drop_column("backtest_runs", "commission_bps")
    op.drop_column("backtest_runs", "strategy")
    op.drop_column("backtest_runs", "interval")
