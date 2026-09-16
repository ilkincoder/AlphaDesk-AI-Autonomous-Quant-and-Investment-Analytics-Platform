"""create portfolios and holdings

Written out explicitly rather than generated. Alembic's autogenerate does not
compare CHECK constraints, so a generated migration could have created these tables
while silently omitting the three CHECKs below.

Revision ID: 0001_portfolios_holdings
Revises:
Create Date: 2026-09-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_portfolios_holdings"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("cash_balance", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.CheckConstraint(
            "cash_balance >= 0", name="ck_portfolios_cash_balance_nonnegative"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_portfolios_name"),
    )

    op.create_table(
        "holdings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("average_buy_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_holdings_quantity_positive"),
        sa.CheckConstraint(
            "average_buy_price > 0", name="ck_holdings_average_buy_price_positive"
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "portfolio_id", "symbol", name="uq_holdings_portfolio_id_symbol"
        ),
    )


def downgrade() -> None:
    op.drop_table("holdings")
    op.drop_table("portfolios")