"""Order-level bracket support: stop-loss / take-profit columns on ``orders``.

Revision ID: 0002_orders_brackets
Revises: 0001_initial
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_orders_brackets"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("stop_loss", sa.Numeric(18, 6), nullable=True))
    op.add_column("orders", sa.Column("take_profit", sa.Numeric(18, 6), nullable=True))
    op.alter_column(
        "orders",
        "idempotency_key",
        existing_type=sa.String(36),
        type_=sa.String(128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "orders",
        "idempotency_key",
        existing_type=sa.String(128),
        type_=sa.String(36),
        existing_nullable=False,
    )
    op.drop_column("orders", "take_profit")
    op.drop_column("orders", "stop_loss")
