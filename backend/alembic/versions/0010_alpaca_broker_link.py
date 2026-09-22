"""link a portfolio to an Alpaca paper account

Module 2 milestone 1: the portfolio stops being demo-only. A broker link and the broker's
own prices are stored alongside the existing columns.

**Three changes, each with a reason.**

* `portfolios` gains the link -- `broker`, `broker_account_id`, `broker_equity`,
  `last_synced_at` -- written together by one successful sync. The CHECK makes a partial
  link unrepresentable, so nothing downstream has to handle "linked but unpriced".
* `holdings` gains `market_price` and `market_value`, both nullable. NULL means "the
  broker has never priced this row", which is the truth for a demo holding and a different
  fact from a price of zero.
* `ck_portfolios_cash_balance_nonnegative` is **replaced, not dropped**. The constraint it
  becomes asserts the same thing about the same rows it always did -- an unlinked
  portfolio cannot hold a negative cash balance, because that figure is one this
  application chose -- and stops applying once the figure is the broker's, because a
  margin paper account can report a negative balance and refusing to store it would mean
  refusing to synchronise such an account at all.

**No row is touched.** In particular this migration does *not* rename the portfolio. The
name says what the row is, and a row holding the placeholder portfolio is not an Alpaca
account yet; the rename belongs to the first successful sync, which is the moment the row
actually becomes one. A migration runs against a database it knows nothing about, and
would label a fresh install's made-up figures as a real account's.

Every existing table and every existing row outside these two is untouched. Module 1's
tables are not referenced at all.

Revision ID: 0010_alpaca_broker_link
Revises: 0009_conversations
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_alpaca_broker_link"
down_revision: str | None = "0009_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BROKER_LINK_IS_WHOLE = (
    "(broker IS NULL) = (broker_account_id IS NULL) "
    "AND (broker IS NULL) = (broker_equity IS NULL) "
    "AND (broker IS NULL) = (last_synced_at IS NULL)"
)

# The original rule, which still holds for a portfolio no broker has priced.
_CASH_NONNEGATIVE = "cash_balance >= 0"

# The same rule, exempting a row whose cash the broker reported.
_UNLINKED_CASH_NONNEGATIVE = f"broker IS NOT NULL OR {_CASH_NONNEGATIVE}"


def upgrade() -> None:
    op.drop_constraint(
        "ck_portfolios_cash_balance_nonnegative", "portfolios", type_="check"
    )

    op.add_column("portfolios", sa.Column("broker", sa.String(length=32), nullable=True))
    op.add_column(
        "portfolios",
        sa.Column("broker_account_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "portfolios", sa.Column("broker_equity", sa.Numeric(18, 2), nullable=True)
    )
    op.add_column(
        "portfolios",
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_portfolios_broker_link_is_whole", "portfolios", _BROKER_LINK_IS_WHOLE
    )
    op.create_unique_constraint(
        "uq_portfolios_broker_account",
        "portfolios",
        ["broker", "broker_account_id"],
    )
    # Created after `broker` exists, because it is a condition on that column.
    op.create_check_constraint(
        "ck_portfolios_unlinked_cash_balance_nonnegative",
        "portfolios",
        _UNLINKED_CASH_NONNEGATIVE,
    )

    op.add_column("holdings", sa.Column("market_price", sa.Numeric(), nullable=True))
    op.add_column("holdings", sa.Column("market_value", sa.Numeric(), nullable=True))
    op.create_check_constraint(
        "ck_holdings_market_price_nonnegative",
        "holdings",
        "market_price IS NULL OR market_price >= 0",
    )
    op.create_check_constraint(
        "ck_holdings_market_value_nonnegative",
        "holdings",
        "market_value IS NULL OR market_value >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_holdings_market_value_nonnegative", "holdings", type_="check")
    op.drop_constraint("ck_holdings_market_price_nonnegative", "holdings", type_="check")
    op.drop_column("holdings", "market_value")
    op.drop_column("holdings", "market_price")

    # Dropped before `broker`, because it is a condition on that column.
    op.drop_constraint(
        "ck_portfolios_unlinked_cash_balance_nonnegative", "portfolios", type_="check"
    )
    op.drop_constraint("uq_portfolios_broker_account", "portfolios", type_="unique")
    op.drop_constraint(
        "ck_portfolios_broker_link_is_whole", "portfolios", type_="check"
    )
    op.drop_column("portfolios", "last_synced_at")
    op.drop_column("portfolios", "broker_equity")
    op.drop_column("portfolios", "broker_account_id")
    op.drop_column("portfolios", "broker")

    # Restored last, so the constraint cannot reject a row the drops above have left
    # half-updated. A negative cash balance would make this fail loudly rather than
    # silently -- which is the right outcome: the row is about to lose the link that made
    # its figure legitimate, and that needs a person, not a migration that guesses.
    op.create_check_constraint(
        "ck_portfolios_cash_balance_nonnegative", "portfolios", _CASH_NONNEGATIVE
    )
