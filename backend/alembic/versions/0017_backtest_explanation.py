"""store a run's explanation, if one was written

Three columns, all nullable, and nothing existing is altered.

**Nullable because most runs have no explanation.** A run is complete without one -- the
explanation is a convenience laid over a finished result, generated on request. A NOT NULL
column here would mean either a placeholder that reads like an explanation or a run that cannot
be stored until a model has been asked, and both are worse than an honest null.

**The model and the prompt version are stored with the text.** An explanation is prose a model
wrote, and prose is only meaningful beside the request that produced it: the same run explained
under a later prompt may say something else, and a reader comparing two explanations needs to
know whether the run changed or the question did. `rebalance_proposals.rationale` carries no
such provenance because it is written once, inside the run that produced it; this one is
generated afterwards, on a request, which is exactly why it needs to say which request.

Revision ID: 0017_backtest_explanation
Revises: 0016_backtest_run_parameters
Create Date: 2026-10-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_backtest_explanation"
down_revision: str | None = "0016_backtest_run_parameters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("explanation", sa.Text(), nullable=True))
    op.add_column(
        "backtest_runs", sa.Column("explanation_model", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "backtest_runs",
        sa.Column("explanation_prompt_version", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backtest_runs", "explanation_prompt_version")
    op.drop_column("backtest_runs", "explanation_model")
    op.drop_column("backtest_runs", "explanation")
