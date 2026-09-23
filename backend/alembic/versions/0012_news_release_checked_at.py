"""add news_articles.release_checked_at

When this article's linked release page was last actually retrieved from the agency's site.

An official feed carries a headline and often one sentence -- the Fed's `description` is its
headline repeated verbatim -- so the release page itself is what there is to index. Reading it
is a request to a public service, and a repeated ingestion was making that request again for
every entry it already held, on every run.

None of the three times already on the row answers "when did we last read the page":
`published_at` is the publisher's, `provider_updated_at` is the publisher's claim about its own
record, and `ingested_at` is when this database last learned something *new* -- which an
unchanged article never is. So the freshness rule had nothing to measure against, and the
column is what it measures.

**Deliberately not backfilled.** Which stored articles were built from a release page is not
recorded anywhere, and inferring it from body length would be a guess written into a column
that is supposed to be a fact. NULL reads as "no release page retrieved", which is why the
first run after this migration re-reads each release once and stamps it.

Revision ID: 0012_news_release_checked_at
Revises: 0011_news
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_news_release_checked_at"
down_revision: str | None = "0011_news"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "news_articles",
        sa.Column("release_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("news_articles", "release_checked_at")
