"""widen imported numerics and add the columns ingestion needs

Three debts come due here, all deferred deliberately by earlier steps.

**Precision.** Step 2 found that Twelve Data returns prices needing a seventh decimal
place, and deliberately refused to round them. `daily_prices` and the imported numeric
columns on `insider_transactions` become plain NUMERIC so the source's own precision is
what gets stored. Every existing CHECK constraint stays attached -- only the type is
loosened. `portfolios.cash_balance` and `holdings.quantity`/`average_buy_price` are NOT
touched: they hold the demo portfolio's money and positions, and the demo valuation path
must behave identically.

**Adjustment metadata.** The provider's exact mode joins the unique key, so two modes for
one day cannot overwrite each other.

**Nullability.** Step 3's parser returns `bool | None` for owner relationship flags and
`str | None` for direct/indirect ownership, because a document can genuinely omit them.
The columns were NOT NULL, which would have meant inventing a fact. They become nullable.

Written out explicitly rather than generated, for the same reason as 0001 and 0002:
Alembic's autogenerate does not compare CHECK constraints, and would happily have
"migrated" this schema while dropping every one of them.

**Every table this touches is empty**, which is why the added NOT NULL columns need no
defaults and there are no backfills. That was checked before writing this file rather than
assumed: inventing metadata for rows that do not exist would be worse than useless.

Revision ID: 0003_ingestion_storage
Revises: 0002_market_data_and_filings
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_ingestion_storage"
down_revision: str | None = "0002_market_data_and_filings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The columns whose fixed scale would have rounded valid provider values.
_RETYPED = {
    "daily_prices": ("open", "high", "low", "close"),
    "insider_transactions": ("shares", "price_per_share"),
}

_ORIGINAL_TYPES = {
    ("daily_prices", "open"): sa.Numeric(precision=18, scale=6),
    ("daily_prices", "high"): sa.Numeric(precision=18, scale=6),
    ("daily_prices", "low"): sa.Numeric(precision=18, scale=6),
    ("daily_prices", "close"): sa.Numeric(precision=18, scale=6),
    ("insider_transactions", "shares"): sa.Numeric(precision=24, scale=6),
    ("insider_transactions", "price_per_share"): sa.Numeric(precision=18, scale=6),
}


def upgrade() -> None:
    # --- precision ------------------------------------------------------------------
    for table, columns in _RETYPED.items():
        for column in columns:
            op.alter_column(
                table,
                column,
                type_=sa.Numeric(),
                existing_type=_ORIGINAL_TYPES[(table, column)],
                existing_nullable=False,
            )

    # --- adjustment metadata --------------------------------------------------------
    op.add_column(
        "daily_prices",
        sa.Column("provider_adjust_mode", sa.String(length=16), nullable=False),
    )
    # Always NULL for now: the Twelve Data client reports nothing about volume adjustment.
    # NULL means "not stated", which is not the same as "not adjusted".
    op.add_column(
        "daily_prices",
        sa.Column("volume_adjustment", sa.String(length=16), nullable=True),
    )
    op.drop_constraint(
        "uq_daily_prices_company_date_provider_basis", "daily_prices", type_="unique"
    )
    op.create_unique_constraint(
        "uq_daily_prices_company_date_provider_basis_mode",
        "daily_prices",
        ["company_id", "trading_date", "provider", "adjustment_basis", "provider_adjust_mode"],
    )

    # --- SEC filing fields ----------------------------------------------------------
    op.add_column(
        "sec_filings", sa.Column("document_type", sa.String(length=16), nullable=False)
    )
    op.add_column(
        "sec_filings", sa.Column("schema_version", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "sec_filings", sa.Column("date_of_original_submission", sa.Date(), nullable=True)
    )
    op.add_column("sec_filings", sa.Column("rule_10b5_1", sa.Boolean(), nullable=True))
    op.add_column(
        "sec_filings", sa.Column("holding_rows_skipped", sa.Integer(), nullable=False)
    )
    op.add_column("sec_filings", sa.Column("remarks", sa.Text(), nullable=True))
    op.add_column(
        "sec_filings",
        sa.Column("footnotes", postgresql.JSONB(), nullable=False),
    )
    op.add_column("sec_filings", sa.Column("source_xml", sa.Text(), nullable=False))
    op.add_column(
        "sec_filings", sa.Column("source_xml_sha256", sa.String(length=64), nullable=False)
    )

    # --- insider transaction fields -------------------------------------------------
    # Nullable because the parser returns None when a document omits `ownershipNature`.
    op.alter_column(
        "insider_transactions",
        "ownership_direct_indirect",
        existing_type=sa.String(length=1),
        nullable=True,
    )
    op.add_column(
        "insider_transactions", sa.Column("nature_of_ownership", sa.Text(), nullable=True)
    )
    op.add_column(
        "insider_transactions", sa.Column("shares_owned_following", sa.Numeric(), nullable=True)
    )
    op.add_column(
        "insider_transactions", sa.Column("footnote_refs", postgresql.JSONB(), nullable=False)
    )
    op.add_column(
        "insider_transactions",
        sa.Column("underlying_security_title", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "insider_transactions", sa.Column("underlying_shares", sa.Numeric(), nullable=True)
    )
    op.add_column(
        "insider_transactions", sa.Column("exercise_price", sa.Numeric(), nullable=True)
    )
    op.add_column(
        "insider_transactions", sa.Column("expiration_date", sa.Date(), nullable=True)
    )

    # --- reporting-owner nullability -------------------------------------------------
    for column in ("is_director", "is_officer", "is_ten_percent_owner", "is_other"):
        op.alter_column(
            "insider_reporting_owners",
            column,
            existing_type=sa.Boolean(),
            nullable=True,
            server_default=None,
        )

    # --- ingestion receipts ----------------------------------------------------------
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingestion_runs_company_id_started_at",
        "ingestion_runs",
        ["company_id", "started_at"],
    )


def downgrade() -> None:
    """Reverse the above.

    Two of these are lossy in a way worth naming. Narrowing the numeric columns back to
    `numeric(18,6)` will round -- or fail outright on -- any value that needed the extra
    digits, which is the whole reason they were widened. Dropping the added columns
    discards the filing's source XML and the parsed fields that have nowhere else to live.

    That is why the downgrade is exercised only against the disposable `alphadesk_test`
    database, and never against one holding real ingested data.
    """
    op.drop_index("ix_ingestion_runs_company_id_started_at", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")

    for column in ("is_director", "is_officer", "is_ten_percent_owner", "is_other"):
        op.alter_column(
            "insider_reporting_owners",
            column,
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )

    op.drop_column("insider_transactions", "expiration_date")
    op.drop_column("insider_transactions", "exercise_price")
    op.drop_column("insider_transactions", "underlying_shares")
    op.drop_column("insider_transactions", "underlying_security_title")
    op.drop_column("insider_transactions", "footnote_refs")
    op.drop_column("insider_transactions", "shares_owned_following")
    op.drop_column("insider_transactions", "nature_of_ownership")
    op.alter_column(
        "insider_transactions",
        "ownership_direct_indirect",
        existing_type=sa.String(length=1),
        nullable=False,
    )

    op.drop_column("sec_filings", "source_xml_sha256")
    op.drop_column("sec_filings", "source_xml")
    op.drop_column("sec_filings", "footnotes")
    op.drop_column("sec_filings", "remarks")
    op.drop_column("sec_filings", "holding_rows_skipped")
    op.drop_column("sec_filings", "rule_10b5_1")
    op.drop_column("sec_filings", "date_of_original_submission")
    op.drop_column("sec_filings", "schema_version")
    op.drop_column("sec_filings", "document_type")

    op.drop_constraint(
        "uq_daily_prices_company_date_provider_basis_mode", "daily_prices", type_="unique"
    )
    op.create_unique_constraint(
        "uq_daily_prices_company_date_provider_basis",
        "daily_prices",
        ["company_id", "trading_date", "provider", "adjustment_basis"],
    )
    op.drop_column("daily_prices", "volume_adjustment")
    op.drop_column("daily_prices", "provider_adjust_mode")

    for table, columns in _RETYPED.items():
        for column in columns:
            op.alter_column(
                table,
                column,
                type_=_ORIGINAL_TYPES[(table, column)],
                existing_type=sa.Numeric(),
                existing_nullable=False,
            )