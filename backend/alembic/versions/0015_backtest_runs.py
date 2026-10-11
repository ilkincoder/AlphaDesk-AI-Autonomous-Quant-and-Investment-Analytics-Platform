"""store backtest runs

Module 3's first milestone: one table, and nothing existing is altered.

**One table, because one run is one record.** A backtest is a deterministic calculation over a
fixed set of bars, so there is no state machine to decompose: the parameters, the data it read
and the result it produced all belong to the same row and are all final the moment it is
written.

**Completed runs only.** A run that failed wrote nothing. There is no partial figure worth
keeping -- a stored run is a record of an answer, and a row with no answer in it is a worse
record than no row. That is also why there is no `status` column: every row here finished.

**`input_snapshot` is the reason this table is not a set of foreign keys.** `daily_prices` holds
the same sessions under a unique key that permits an upsert, so a row there is a current value
rather than a historical one; a provider revising its split history would rewrite it. Storing
the bars themselves -- JSONB, warm-up included -- is what makes a stored result reproducible
instead of merely dated. A digest would not do: it proves the numbers changed, not what they
were.

Revision ID: 0015_backtest_runs
Revises: 0014_order_execution
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_backtest_runs"
down_revision: str | None = "0014_order_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        # The client's own id for the request, so a retry resolves to the run it already made.
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # --- the strategy, pinned: a run is only reproducible against the rules that ran ---
        sa.Column("strategy_id", sa.String(length=48), nullable=False),
        sa.Column("strategy_version", sa.String(length=16), nullable=False),
        # --- the resolved listing, not the letters that were typed ---
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("instrument_name", sa.String(length=200), nullable=True),
        sa.Column("instrument_type", sa.String(length=48), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        # --- the test window and its parameters ---
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("initial_capital", sa.Numeric(18, 2), nullable=False),
        sa.Column("fast_period", sa.Integer(), nullable=False),
        sa.Column("slow_period", sa.Integer(), nullable=False),
        # --- the cost model, in the units the request stated ---
        sa.Column("commission_per_share", sa.Numeric(18, 6), nullable=False),
        sa.Column("slippage_percent", sa.Numeric(9, 4), nullable=False),
        # --- where the bars came from, and what they were ---
        #
        # The provider's own adjustment mode is stored beside this application's two-word
        # vocabulary: "splits" rather than "dividends" is what makes every figure here a price
        # return, and that is a fact about the row, not about the code that wrote it.
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("adjustment_basis", sa.String(length=16), nullable=False),
        sa.Column("provider_adjust_mode", sa.String(length=16), nullable=False),
        sa.Column("data_retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_first_date", sa.Date(), nullable=False),
        sa.Column("data_last_date", sa.Date(), nullable=False),
        sa.Column("data_sessions", sa.Integer(), nullable=False),
        # The exact bars consumed, warm-up included. See the module docstring.
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # Both curves, the ledger, the metrics and the prose. See the module docstring.
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_backtest_runs_request_id"),
        sa.CheckConstraint("fast_period >= 2", name="ck_backtest_runs_fast_period_min"),
        sa.CheckConstraint(
            "slow_period > fast_period", name="ck_backtest_runs_fast_before_slow"
        ),
        sa.CheckConstraint("start_date < end_date", name="ck_backtest_runs_dates_ordered"),
        sa.CheckConstraint("initial_capital > 0", name="ck_backtest_runs_capital_positive"),
        sa.CheckConstraint(
            "commission_per_share >= 0", name="ck_backtest_runs_commission_nonnegative"
        ),
        sa.CheckConstraint(
            "slippage_percent >= 0", name="ck_backtest_runs_slippage_nonnegative"
        ),
        sa.CheckConstraint(
            "adjustment_basis IN ('raw', 'adjusted')",
            name="ck_backtest_runs_adjustment_basis",
        ),
    )
    op.create_index("ix_backtest_runs_created_at", "backtest_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_created_at", table_name="backtest_runs")
    op.drop_table("backtest_runs")
