"""add conversations and conversation turns

Two tables for the analysis chat: a conversation carries the settled context and any pending
clarification, and a turn is one user message and what came back.

**Purely additive.** Nothing existing is altered and no row is touched. The analysis flow these
tables store is new; the four tools, the stored filings and the portfolio are all untouched.

Three constraints carry real weight and are worth naming:

* `uq_conversation_turns_request` on `(conversation_id, request_id)` is what makes a retry safe.
  A duplicate request cannot create a second turn, and therefore cannot cause a second paid
  analysis.
* `uq_conversation_turns_sequence` on `(conversation_id, sequence)` is what makes the history an
  order rather than a set.
* The two CHECK constraints on `conversations` keep a partial state from existing at all: a
  lease with no deadline could never expire, and a pending clarification with no reference date
  could never be resumed against the date it was asked about.

`status` on a turn is deliberately unconstrained. The run's status vocabulary lives in
`app/agent/run.py` and will grow; a status that needed a migration before it could be recorded
is a status somebody would record wrongly instead.

Revision ID: 0009_conversations
Revises: 0008_form4_scope_rename
Create Date: 2026-09-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_conversations"
down_revision: str | None = "0008_form4_scope_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_symbol", sa.String(length=20), nullable=True),
        sa.Column("settled_start_date", sa.Date(), nullable=True),
        sa.Column("settled_end_date", sa.Date(), nullable=True),
        sa.Column("settled_as_of", sa.Date(), nullable=True),
        sa.Column("pending_question", sa.Text(), nullable=True),
        sa.Column("pending_reference_date", sa.Date(), nullable=True),
        sa.Column("pending_asked_for", sa.Text(), nullable=True),
        sa.Column("processing_turn_id", sa.String(length=32), nullable=True),
        sa.Column("processing_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(processing_turn_id IS NULL) = (processing_deadline IS NULL)",
            name="ck_conversations_lease_is_whole",
        ),
        sa.CheckConstraint(
            "(pending_question IS NULL) = (pending_reference_date IS NULL)",
            name="ck_conversations_pending_is_whole",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False),
        sa.Column("request_symbol", sa.String(length=20), nullable=True),
        sa.Column("request_start_date", sa.Date(), nullable=True),
        sa.Column("request_end_date", sa.Date(), nullable=True),
        sa.Column("request_as_of", sa.Date(), nullable=True),
        sa.Column("reference_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("resolved_symbol", sa.String(length=20), nullable=True),
        sa.Column("resolved_start_date", sa.Date(), nullable=True),
        sa.Column("resolved_end_date", sa.Date(), nullable=True),
        sa.Column("resolved_as_of", sa.Date(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "request_id", name="uq_conversation_turns_request"
        ),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_conversation_turns_sequence"
        ),
    )
    op.create_index(
        "ix_conversation_turns_conversation_id_sequence",
        "conversation_turns",
        ["conversation_id", "sequence"],
    )


def downgrade() -> None:
    # Turns first: they carry the foreign key. The cascade would handle it either way, but an
    # explicit order is one less thing depending on the cascade being configured right.
    op.drop_index(
        "ix_conversation_turns_conversation_id_sequence",
        table_name="conversation_turns",
    )
    op.drop_table("conversation_turns")
    op.drop_table("conversations")
