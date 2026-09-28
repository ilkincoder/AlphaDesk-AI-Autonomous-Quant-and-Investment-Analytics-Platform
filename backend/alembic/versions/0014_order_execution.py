"""store approvals and Alpaca paper order execution

Module 2's execution milestone: three tables, and nothing existing is altered.

`rebalance_proposals` keeps the shape it had. Its `status` column stays the *run's* life --
`generating` and then one of `proposed`, `no_change`, `unavailable`, `interrupted` -- because the
partial unique index that makes one proposal run at a time is a predicate on that column, and a
decision is a different fact about a different actor. An approved proposal is still a `proposed`
proposal; what changed is that a person decided something about it, and that is recorded here.

**Three tables, because three things can be true independently.**

* `rebalance_decisions` is the durable human decision. One per proposal, enforced by a unique
  constraint rather than by a check the application could race: two clicks, a retry and a
  resume all resolve to the one row that exists. It carries the *resolved* order plan, so what
  was approved is a record rather than something re-derived later from a proposal that could
  have been superseded.
* `execution_batches` is one attempt to execute one approved plan against one account. The
  partial unique index admits one active batch per account, which is what makes "one active
  execution batch" a fact the database keeps rather than a rule the application remembers.
* `order_intents` is one planned order. It is written **before** the broker request, and
  `client_order_id` is unique and derived from the batch and the order's position in it -- so a
  crash, a replay or a restart regenerates the same id and the broker's own record of the
  submission can be found by it.

**`status` on an intent is never a guess.** `accepted`, `filled`, `rejected` and the rest come
from the broker's own report, and `unknown` is what an unanswered submission gets -- an outcome
that is genuinely not known, and that blocks whatever depended on it until it is. There is no
value here meaning "probably filled".

Revision ID: 0014_order_execution
Revises: 0013_rebalance_proposals
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_order_execution"
down_revision: str | None = "0013_rebalance_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rebalance_decisions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("proposal_id", sa.String(length=32), nullable=False),
        # 'approved' or 'rejected'. There is no third value: a decision that was neither would
        # not be a decision.
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        # Chosen by the client so a retry is free, exactly as a proposal's `request_id` is.
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        # The proposal version this binds to: the digest of the frozen snapshot, then the digest
        # of the exact orders. Both are stored, so what was approved can be checked against what
        # the proposal says rather than against what it says today.
        sa.Column("proposal_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=False),
        # The resolved orders, exactly as approved, server-side. Nothing a client sent.
        sa.Column("order_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # What the plan was validated against. NULL on a rejection, which validated nothing.
        sa.Column("broker_account_id", sa.String(length=64), nullable=True),
        sa.Column("cash_at_approval", sa.Numeric(18, 2), nullable=True),
        sa.Column("reserved_cash", sa.Numeric(18, 2), nullable=True),
        sa.Column("price_drift_percent", sa.Numeric(9, 4), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["rebalance_proposals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # One decision per proposal. This is what makes a second click, a conflicting pair of
        # clicks and a resumed workflow all end at the same answer.
        sa.UniqueConstraint("proposal_id", name="uq_rebalance_decisions_proposal_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_rebalance_decisions_idempotency_key"),
    )

    op.create_table(
        "execution_batches",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("proposal_id", sa.String(length=32), nullable=False),
        sa.Column("decision_id", sa.String(length=32), nullable=False),
        sa.Column("portfolio_id", sa.Integer(), nullable=False),
        sa.Column("broker_account_id", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        # pending, submitting, completed, partial, halted.
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cash_at_approval", sa.Numeric(18, 2), nullable=False),
        sa.Column("reserved_cash", sa.Numeric(18, 2), nullable=False),
        sa.Column("price_drift_percent", sa.Numeric(9, 4), nullable=True),
        # Plain-language notes: waiting for sells, a halted remainder, a partial completion.
        sa.Column("notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("halt_reason", sa.Text(), nullable=True),
        # The portfolio sync is reported separately from the execution, because a sync that
        # failed says nothing about whether an order filled.
        sa.Column("sync_status", sa.String(length=16), nullable=False),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["rebalance_proposals.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["rebalance_decisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposal_id", name="uq_execution_batches_proposal_id"),
    )
    # One active batch per account. `status IN ('pending','submitting')` fixes the column to the
    # two values that mean "still working", so uniqueness on it is uniqueness of the live batch.
    op.create_index(
        "uq_execution_batches_one_active",
        "execution_batches",
        ["broker_account_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'submitting')"),
    )
    op.create_index("ix_execution_batches_created_at", "execution_batches", ["created_at"])

    op.create_table(
        "order_intents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=False),
        # Sells come before buys, and this is the order they are submitted in.
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("order_type", sa.String(length=8), nullable=False),
        sa.Column("time_in_force", sa.String(length=4), nullable=False),
        # Derived from the batch and the sequence, so it survives a restart unchanged.
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        # The broker's own word, kept verbatim beside the status this application maps it to.
        sa.Column("broker_status", sa.String(length=24), nullable=True),
        sa.Column("estimated_price", sa.Numeric(18, 4), nullable=False),
        # Copied from the approved plan, so a submission made later can tell this batch's own
        # fills apart from somebody else's trades without re-reading the decision.
        sa.Column("quantity_before", sa.Numeric(18, 6), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("filled_avg_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        # Consecutive reconciliations that found no such order. A single "not found" does not
        # prove an unanswered submission failed, so this counts rather than concludes.
        sa.Column("uncertain_checks", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["batch_id"], ["execution_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # What the broker is asked about. Unique, because two orders sharing one client id would
        # make reconciliation ambiguous in exactly the case it exists for.
        sa.UniqueConstraint("client_order_id", name="uq_order_intents_client_order_id"),
        sa.UniqueConstraint("batch_id", "sequence", name="uq_order_intents_batch_sequence"),
    )
    op.create_index("ix_order_intents_batch_id", "order_intents", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_order_intents_batch_id", table_name="order_intents")
    op.drop_table("order_intents")
    op.drop_index("ix_execution_batches_created_at", table_name="execution_batches")
    op.drop_index("uq_execution_batches_one_active", table_name="execution_batches")
    op.drop_table("execution_batches")
    op.drop_table("rebalance_decisions")
