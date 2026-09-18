"""add disclosure documents, fact snapshots, and financial facts

Three new tables and one column, all additive.

**`filing_documents`** exists because a filing has one primary document and may have several
exhibits, so the documents cannot live as columns on `sec_filings`. Nothing is moved: the
Form 4 columns on `sec_filings` keep their meaning, and `source_xml` is not repurposed to
hold 10-K HTML.

**`company_fact_snapshots`** keeps each distinct Company Facts response whole, deduplicated
by content hash.

**`financial_facts`** stores observations. Its natural key is declared NULLS NOT DISTINCT
because instant facts such as `Assets` have no `period_start`; under PostgreSQL's default
every NULL counts as distinct, and the same balance-sheet figure would be re-inserted on
every run.

**`ingestion_runs.scope`** separates insider ingestion from disclosure ingestion. Its default
is an accurate backfill rather than an invented value: every run written before this column
existed was a Form 4 run. Without the separation, a disclosure run would become "the latest
run" for the company and a Form 4 analysis would report three filings as insider coverage.

Written out explicitly rather than generated, for the same reason as every migration before
it: autogenerate does not compare CHECK constraints.

Revision ID: 0004_company_context
Revises: 0003_ingestion_storage
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_company_context"
down_revision: str | None = "0003_ingestion_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FORM4_SCOPE = "nvda_form4"


def upgrade() -> None:
    op.add_column(
        "ingestion_runs",
        sa.Column(
            "scope",
            sa.String(length=32),
            nullable=False,
            server_default=FORM4_SCOPE,
        ),
    )

    op.create_table(
        "filing_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.Integer(), nullable=False),
        sa.Column("document_name", sa.String(length=255), nullable=False),
        sa.Column("document_type", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("source_url", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extraction_version", sa.String(length=16), nullable=True),
        sa.Column("extraction_status", sa.String(length=32), nullable=False),
        sa.Column("extraction_limitations", sa.Text(), nullable=True),
        sa.Column("sections", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "role IN ('primary', 'exhibit')", name="ck_filing_documents_role"
        ),
        sa.CheckConstraint(
            "extraction_status IN ('extracted', 'unsupported', 'too_large', 'failed')",
            name="ck_filing_documents_extraction_status",
        ),
        sa.ForeignKeyConstraint(
            ["filing_id"], ["sec_filings.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "filing_id",
            "document_name",
            name="uq_filing_documents_filing_id_document_name",
        ),
    )
    op.create_index(
        "ix_filing_documents_filing_id_role",
        "filing_documents",
        ["filing_id", "role"],
    )

    op.create_table(
        "company_fact_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.String(length=512), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_sha256", name="uq_company_fact_snapshots_content_sha256"
        ),
    )

    op.create_table(
        "financial_facts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("taxonomy", sa.String(length=16), nullable=False),
        sa.Column("concept", sa.String(length=128), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        # Unconstrained NUMERIC: the reported value is stored exactly as reported. No
        # non-negative check, because net income is negative in a loss-making period.
        sa.Column("value", sa.Numeric(), nullable=False),
        # NULL for an instant fact; set for a duration fact.
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("accession_number", sa.String(length=25), nullable=True),
        sa.Column("form", sa.String(length=16), nullable=True),
        sa.Column("filed_date", sa.Date(), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_period", sa.String(length=8), nullable=True),
        sa.Column("frame", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["company_fact_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # NULLS NOT DISTINCT so that instant facts -- which have no period_start -- are
        # still deduplicated. PostgreSQL treats NULLs as distinct by default, which would
        # let the same balance-sheet figure in on every run.
        sa.UniqueConstraint(
            "company_id",
            "taxonomy",
            "concept",
            "unit",
            "period_start",
            "period_end",
            "accession_number",
            name="uq_financial_facts_observation",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_financial_facts_company_id_concept_period_end",
        "financial_facts",
        ["company_id", "concept", "period_end"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_financial_facts_company_id_concept_period_end", table_name="financial_facts"
    )
    op.drop_table("financial_facts")
    op.drop_table("company_fact_snapshots")
    op.drop_index(
        "ix_filing_documents_filing_id_role", table_name="filing_documents"
    )
    op.drop_table("filing_documents")
    op.drop_column("ingestion_runs", "scope")
