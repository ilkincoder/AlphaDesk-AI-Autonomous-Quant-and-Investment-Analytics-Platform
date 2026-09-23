"""store rebalance proposals

Module 2's proposal milestone: one table, and nothing existing is altered.

* `rebalance_proposals` is one generated proposal, with the frozen snapshot it was computed
  from beside the result. The snapshot is stored rather than re-derived on read, because a
  proposal that re-read prices when it was displayed would show today's portfolio under
  yesterday's reasoning -- and because the figures a reader checks have to be the figures the
  calculation actually ran against.

**Two uniqueness rules, and both are load-bearing.**

* `request_id` is unique, which is what makes a retry free: the second request finds the first
  one's row instead of paying for a second workflow.
* A partial unique index on `status` where `status = 'generating'` makes a second concurrent run
  impossible at the database level. A check in application code would be a race; this is not.
  Because the predicate pins the column to one value, uniqueness on it is uniqueness of the
  active row.

`processing_deadline` is what keeps a crashed run from blocking the next one for ever. Recovery
is lazy -- the next request that touches the table clears an expired row -- exactly as
`conversations.processing_deadline` is recovered, and for the same reason: no background sweeper,
and nothing is ever replayed automatically.

**There is no column for approval or execution.** This milestone ends at generation, and a column
that nothing could write would be a promise the schema cannot keep.

Revision ID: 0013_rebalance_proposals
Revises: 0012_news_release_checked_at
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_rebalance_proposals"
down_revision: str | None = "0012_news_release_checked_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rebalance_proposals",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("calculation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("assumptions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limitations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.String(length=48), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_rebalance_proposals_request_id"),
    )
    # At most one run in flight. `status = 'generating'` fixes the column to a single value for
    # the index, so a unique index on it admits one such row and no more.
    op.create_index(
        "uq_rebalance_proposals_one_active",
        "rebalance_proposals",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'generating'"),
    )
    op.create_index(
        "ix_rebalance_proposals_created_at", "rebalance_proposals", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_rebalance_proposals_created_at", table_name="rebalance_proposals")
    op.drop_index(
        "uq_rebalance_proposals_one_active", table_name="rebalance_proposals"
    )
    op.drop_table("rebalance_proposals")
