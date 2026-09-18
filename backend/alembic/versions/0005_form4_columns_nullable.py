"""make the Form 4-specific columns on sec_filings nullable

A design problem that only became visible while implementing the disclosure ingestion.

A 10-K, a 10-Q and an 8-K need rows in `sec_filings`, because that is where shared filing
metadata lives and `filing_documents.filing_id` has to point at something. But three of its
columns are Form 4 vocabulary that a 10-K simply does not have:

* `source_xml` and `source_xml_sha256` hold an ownership document. A 10-K has none, and it
  must not be given the 10-K's HTML -- that is what `filing_documents.content` is for. An
  empty string would be worse than NULL: it would claim there is XML and that it is empty,
  rather than that the question does not apply.
* `holding_rows_skipped` counts holding rows the Form 4 parser declined to treat as
  transactions. No parser ran on a 10-K, so a count of 0 would imply one did.

Relaxing NOT NULL cannot affect a row that already exists; every Form 4 filing still has
all three. `document_type` and `footnotes` stay NOT NULL because they do have a truthful
value for any filing -- the form type, and an empty footnote map.

`0004` is left exactly as it was. It had already been applied when this was found, and an
applied migration is not edited.

Revision ID: 0005_form4_columns_nullable
Revises: 0004_company_context
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_form4_columns_nullable"
down_revision: str | None = "0004_company_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FORM4_ONLY_COLUMNS = ("source_xml", "source_xml_sha256", "holding_rows_skipped")

_COLUMN_TYPES = {
    "source_xml": sa.Text(),
    "source_xml_sha256": sa.String(length=64),
    "holding_rows_skipped": sa.Integer(),
}


def upgrade() -> None:
    for column in _FORM4_ONLY_COLUMNS:
        op.alter_column(
            "sec_filings",
            column,
            existing_type=_COLUMN_TYPES[column],
            nullable=True,
        )


def downgrade() -> None:
    """Restore NOT NULL, which will fail if a disclosure filing is present.

    That is the correct failure: a 10-K row has NULL in these columns, and there is no value
    that could truthfully fill them. Delete the disclosure filings first, or do not go back.
    """
    for column in _FORM4_ONLY_COLUMNS:
        op.alter_column(
            "sec_filings",
            column,
            existing_type=_COLUMN_TYPES[column],
            nullable=False,
        )
