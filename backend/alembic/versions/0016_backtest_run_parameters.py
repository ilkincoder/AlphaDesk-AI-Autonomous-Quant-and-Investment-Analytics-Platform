"""generalise a run's parameters to a document

The second strategy (RSI) is configured with three numbers -- a period and two thresholds --
and no two of them are "the fast one" and "the slow one". The crossover's two columns described
one strategy's arithmetic rather than a run's parameters, so they are replaced by one JSONB
document keyed by the names the strategy declares.

**Existing rows are converted, not discarded.** Every stored run was made by the crossover, so
its two columns become `{"fast_period": ..., "slow_period": ...}` -- the same numbers the run
was executed with, in the shape the engine now reads. The values are copied before the columns
are dropped, and the downgrade copies them back the other way, so the pair round-trips.

**The two check constraints go with the columns.** `fast_period >= 2` and
`slow_period > fast_period` were statements about one strategy: RSI's entry threshold of 30
below its exit threshold of 70 would satisfy the letter of the second by accident, and a
parameter set is now validated by the strategy that declares it rather than by the table. The
remaining checks stay -- dates, capital and costs are facts about every run.

Revision ID: 0016_backtest_run_parameters
Revises: 0015_backtest_runs
Create Date: 2026-10-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_backtest_run_parameters"
down_revision: str | None = "0015_backtest_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Added nullable, filled, then made NOT NULL: a column that must never be null can still be
    # introduced to a populated table, but only if it is allowed to be null for exactly as long
    # as it takes to write the values that already exist in the columns beside it.
    op.add_column(
        "backtest_runs",
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        "UPDATE backtest_runs SET parameters = jsonb_build_object("
        "'fast_period', fast_period, 'slow_period', slow_period)"
    )
    op.alter_column("backtest_runs", "parameters", nullable=False)

    op.drop_constraint("ck_backtest_runs_fast_period_min", "backtest_runs", type_="check")
    op.drop_constraint("ck_backtest_runs_fast_before_slow", "backtest_runs", type_="check")
    op.drop_column("backtest_runs", "fast_period")
    op.drop_column("backtest_runs", "slow_period")


def downgrade() -> None:
    # Nullable while they are filled, for the same reason in reverse. A row written by a
    # strategy other than the crossover has neither number, and the crossover's defaults are
    # what such a row would have to be read as -- there is no honest value to put back, which
    # is the whole reason the columns were replaced.
    op.add_column("backtest_runs", sa.Column("fast_period", sa.Integer(), nullable=True))
    op.add_column("backtest_runs", sa.Column("slow_period", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE backtest_runs SET "
        "fast_period = COALESCE((parameters ->> 'fast_period')::integer, 20), "
        "slow_period = COALESCE((parameters ->> 'slow_period')::integer, 50)"
    )
    op.alter_column("backtest_runs", "fast_period", nullable=False)
    op.alter_column("backtest_runs", "slow_period", nullable=False)

    op.create_check_constraint(
        "ck_backtest_runs_fast_period_min", "backtest_runs", "fast_period >= 2"
    )
    op.create_check_constraint(
        "ck_backtest_runs_fast_before_slow", "backtest_runs", "slow_period > fast_period"
    )
    op.drop_column("backtest_runs", "parameters")
