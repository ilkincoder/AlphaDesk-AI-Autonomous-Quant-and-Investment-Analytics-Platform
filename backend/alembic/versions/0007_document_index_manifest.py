"""add the document index manifest

One table, purely additive.

The vector store is a derived index and cannot answer two questions that matter:

* *Is this document completely indexed?* Qdrant can be asked whether it holds points for a
  document, but not whether **all** of them arrived. A row here is written only after every
  point for that document has been acknowledged, so its absence means "not completely
  indexed" -- the safe direction to be wrong in, because retrieval ignores what is not listed.
* *What was it indexed with?* A collection built with a different 384-dimensional model is
  indistinguishable from this one by inspection. The model, chunking version and both content
  hashes are recorded here so a changed source or a changed pipeline is detected rather than
  silently mixed into the same collection.

Qdrant remains derived: this table can be dropped and the whole index rebuilt from PostgreSQL,
which stays authoritative. Nothing here is a substitute for the source rows.

Revision ID: 0007_document_index_manifest
Revises: 0006_sec_filings_report_date
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_document_index_manifest"
down_revision: str | None = "0006_sec_filings_report_date"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_index_manifest",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("collection_name", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("chunking_version", sa.String(length=16), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("extracted_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("extraction_version", sa.String(length=16), nullable=True),
        sa.Column("point_count", sa.Integer(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["filing_documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # One row per document. A second row would mean two conflicting claims about what is
        # indexed, which is exactly the ambiguity this table exists to remove.
        sa.UniqueConstraint(
            "document_id", name="uq_document_index_manifest_document_id"
        ),
    )
    op.create_index(
        "ix_document_index_manifest_collection_name",
        "document_index_manifest",
        ["collection_name"],
    )


def downgrade() -> None:
    """Drops the manifest only.

    The points in Qdrant are left alone: this table records what was indexed, it does not hold
    the index. Going back means the collection no longer has a manifest saying which documents
    are complete, so retrieval will report nothing as indexed until it is rebuilt.
    """
    op.drop_index(
        "ix_document_index_manifest_collection_name",
        table_name="document_index_manifest",
    )
    op.drop_table("document_index_manifest")
