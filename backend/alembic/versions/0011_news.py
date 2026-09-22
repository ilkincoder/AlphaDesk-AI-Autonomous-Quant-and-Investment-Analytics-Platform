"""store news articles and the receipts of the runs that read them

Module 2 milestone 2: a second corpus beside the SEC filings. Two new tables, and nothing
existing is altered.

* `news_articles` is the canonical store. Its identity is `(provider, provider_article_id)`,
  where the id is the provider's own and falls back to the canonical URL when a provider
  supplies none -- one column carrying the whole dedupe rule, so there is never a second way
  for two rows to be the same article.
* `news_ingestion_runs` is the receipt. It exists because a feed read successfully that had
  nothing new to say changes no article rows, and "when did this source last succeed" is a
  question the articles cannot answer.

**Times are kept apart on purpose.** `published_at` is the publisher's and is what listings
are ordered by; `ingested_at` is ours. Storing one in place of the other is the one mistake
here that nothing could detect afterwards.

The `symbols` GIN index is what makes a company article findable, and the empty list on a
macro release is a fact rather than missing data -- which is why it is an empty JSONB array
and not NULL.

Nothing in Module 1's tables is referenced or changed.

Revision ID: 0011_news
Revises: 0010_alpaca_broker_link
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_news"
down_revision: str | None = "0010_alpaca_broker_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=48), nullable=False),
        sa.Column("provider_article_id", sa.String(length=512), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("canonical_url", sa.String(length=1024), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "symbols", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("indexed_content_sha256", sa.String(length=64), nullable=True),
        sa.Column("indexed_embedding_model", sa.String(length=128), nullable=True),
        sa.Column("indexed_chunking_version", sa.String(length=16), nullable=True),
        sa.Column("indexed_point_count", sa.Integer(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('company', 'macro')", name="ck_news_articles_category"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "provider_article_id",
            name="uq_news_articles_provider_provider_article_id",
        ),
    )
    op.create_index(
        "ix_news_articles_published_at", "news_articles", ["published_at"]
    )
    op.create_index(
        "ix_news_articles_provider_published_at",
        "news_articles",
        ["provider", "published_at"],
    )
    op.create_index(
        "ix_news_articles_symbols",
        "news_articles",
        ["symbols"],
        postgresql_using="gin",
    )

    op.create_table(
        "news_ingestion_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "results", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # Indexes go with their table; dropping them first only makes the intent explicit.
    op.drop_index("ix_news_articles_symbols", table_name="news_articles")
    op.drop_index("ix_news_articles_provider_published_at", table_name="news_articles")
    op.drop_index("ix_news_articles_published_at", table_name="news_articles")
    op.drop_table("news_articles")
    op.drop_table("news_ingestion_runs")
