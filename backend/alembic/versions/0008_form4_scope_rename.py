"""rename the Form 4 ingestion scope from nvda_form4 to form4

The scope column names the *kind* of run, not the company: `form4` for prices and insider
filings, `company_context` for the disclosures and financial facts. Only `analyze_insiders`
reads it, to pick out the Form 4 runs and ignore the disclosure ones when reporting how much
insider history has been ingested.

It was written as `nvda_form4` when NVIDIA was the only company that could be ingested. The
moment another company is added that stops being a name and becomes a lie -- Apple's Form 4
runs would be filed under NVIDIA's.

**This updates rows that already exist**, which is worth stating plainly. The alternative was
a scope per company, but a value that varies by company cannot be used to select "the Form 4
runs", which is the only thing the column is for. Nothing but the label changes; no run's
identity, counts or timestamps are touched.

The downgrade puts the old value back, which is only correct while NVIDIA is the sole company
ingested.

Revision ID: 0008_form4_scope_rename
Revises: 0007_document_index_manifest
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_form4_scope_rename"
down_revision: str | None = "0007_document_index_manifest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "nvda_form4"
NEW = "form4"


def upgrade() -> None:
    op.execute(
        sa.text("UPDATE ingestion_runs SET scope = :new WHERE scope = :old").bindparams(
            new=NEW, old=OLD
        )
    )
    op.alter_column(
        "ingestion_runs", "scope", server_default=NEW, existing_type=sa.String(length=32)
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE ingestion_runs SET scope = :old WHERE scope = :new").bindparams(
            new=NEW, old=OLD
        )
    )
    op.alter_column(
        "ingestion_runs", "scope", server_default=OLD, existing_type=sa.String(length=32)
    )
