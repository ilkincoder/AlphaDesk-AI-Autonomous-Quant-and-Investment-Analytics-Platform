"""add sec_filings.report_date

The period a filing reports on -- the end of a quarter, the end of a fiscal year.

Step 4 left this column out on purpose. For a Form 4 the document's own `periodOfReport` is
authoritative, and that value is already stored, so a second copy from the submissions feed
would have been a second source of truth for the same fact.

A 10-K has no ownership XML. There is no `periodOfReport` to prefer, and the feed's
`reportDate` is the only reporting-period date that exists. Without a column for it, the
disclosure ingestion could not retain a fact the brief asks it to retain.

Deliberately not backfilled. The three Form 4 filings already stored do have a `reportDate`
in the feed, but fetching it again to fill them in would be inventing metadata for existing
rows rather than recording something already held. NULL means "not recorded here", which is
true, and Step 4's own reasoning still stands for those rows.

Revision ID: 0006_sec_filings_report_date
Revises: 0005_form4_columns_nullable
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_sec_filings_report_date"
down_revision: str | None = "0005_form4_columns_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sec_filings", sa.Column("report_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("sec_filings", "report_date")
