"""create companies, daily prices, SEC filings and insider transactions

Written out explicitly rather than generated, for the same reason 0001 was:
Alembic's autogenerate does not compare CHECK constraints, so a generated migration
could have created these five tables while silently omitting every CHECK below.

Purely additive. Nothing here reads or writes `portfolios` or `holdings`, so existing
portfolio rows are untouched by this migration -- and its downgrade drops only the
five tables it created.

Revision ID: 0002_market_data_and_filings
Revises: 0001_portfolios_holdings
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_market_data_and_filings"
down_revision: str | None = "0001_portfolios_holdings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        # Text, not integer: a CIK is ten digits with leading zeros.
        sa.Column("sec_issuer_cik", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "sec_issuer_cik ~ '^[0-9]{1,10}$'",
            name="ck_companies_sec_issuer_cik_digits",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", name="uq_companies_ticker"),
        sa.UniqueConstraint("sec_issuer_cik", name="uq_companies_sec_issuer_cik"),
    )

    op.create_table(
        "daily_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("adjustment_basis", sa.String(length=16), nullable=False),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # low > 0 with the two range checks forces all four prices positive, and
        # implies high >= low below. No other price check is needed.
        sa.CheckConstraint("low > 0", name="ck_daily_prices_low_positive"),
        sa.CheckConstraint("high >= low", name="ck_daily_prices_high_gte_low"),
        sa.CheckConstraint(
            "open >= low AND open <= high", name="ck_daily_prices_open_within_low_high"
        ),
        sa.CheckConstraint(
            "close >= low AND close <= high",
            name="ck_daily_prices_close_within_low_high",
        ),
        sa.CheckConstraint("volume >= 0", name="ck_daily_prices_volume_nonnegative"),
        sa.CheckConstraint(
            "adjustment_basis IN ('raw', 'adjusted')",
            name="ck_daily_prices_adjustment_basis",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Distinguishes provider and adjustment basis, so raw and adjusted bars for
        # the same day cannot collide or overwrite one another.
        #
        # `company_date` rather than `company_id_trading_date`: naming every column
        # pushes this past PostgreSQL's 63-character identifier limit, which aborts
        # the migration rather than truncating quietly.
        sa.UniqueConstraint(
            "company_id",
            "trading_date",
            "provider",
            "adjustment_basis",
            name="uq_daily_prices_company_date_provider_basis",
        ),
    )

    op.create_table(
        "sec_filings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("accession_number", sa.String(length=25), nullable=False),
        sa.Column("form_type", sa.String(length=16), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        # Nullable: supplied by some sources and not others.
        sa.Column("acceptance_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_document_url", sa.String(length=512), nullable=False),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "is_amendment", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("amends_filing_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "amends_filing_id IS NULL OR amends_filing_id <> id",
            name="ck_sec_filings_not_self_amending",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["amends_filing_id"], ["sec_filings.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "accession_number", name="uq_sec_filings_accession_number"
        ),
    )
    op.create_index(
        "ix_sec_filings_company_id_filing_date",
        "sec_filings",
        ["company_id", "filing_date"],
    )

    op.create_table(
        "insider_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.Integer(), nullable=False),
        sa.Column("source_table", sa.String(length=24), nullable=False),
        sa.Column("row_position", sa.Integer(), nullable=False),
        sa.Column("is_derivative", sa.Boolean(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("security_title", sa.String(length=255), nullable=False),
        sa.Column("transaction_code", sa.String(length=4), nullable=False),
        sa.Column("acquired_disposed", sa.String(length=1), nullable=False),
        sa.Column("shares", sa.Numeric(precision=24, scale=6), nullable=False),
        # Nullable, and deliberately without a default.
        sa.Column("price_per_share", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("ownership_direct_indirect", sa.String(length=1), nullable=False),
        sa.Column("footnotes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "source_table IN ('nonDerivativeTable', 'derivativeTable')",
            name="ck_insider_transactions_source_table",
        ),
        sa.CheckConstraint(
            "row_position >= 0", name="ck_insider_transactions_row_position_nonnegative"
        ),
        sa.CheckConstraint(
            "is_derivative = (source_table = 'derivativeTable')",
            name="ck_insider_transactions_is_derivative_matches_source_table",
        ),
        sa.CheckConstraint(
            "acquired_disposed IN ('A', 'D')",
            name="ck_insider_transactions_acquired_disposed",
        ),
        sa.CheckConstraint("shares > 0", name="ck_insider_transactions_shares_positive"),
        # >= 0 rather than > 0, so a reported zero price is storable.
        sa.CheckConstraint(
            "price_per_share IS NULL OR price_per_share >= 0",
            name="ck_insider_transactions_price_per_share_nonnegative",
        ),
        sa.CheckConstraint(
            "ownership_direct_indirect IN ('D', 'I')",
            name="ck_insider_transactions_ownership_direct_indirect",
        ),
        # No CHECK on transaction_code: see the model docstring. A closed list would
        # reject a code EDGAR adds later.
        sa.ForeignKeyConstraint(
            ["filing_id"], ["sec_filings.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Identity is provenance, not content: never (date, amount).
        sa.UniqueConstraint(
            "filing_id",
            "source_table",
            "row_position",
            name="uq_insider_transactions_filing_id_source_table_row_position",
        ),
    )
    op.create_index(
        "ix_insider_transactions_transaction_date",
        "insider_transactions",
        ["transaction_date"],
    )

    op.create_table(
        "insider_reporting_owners",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.Integer(), nullable=False),
        sa.Column("reporting_owner_cik", sa.Text(), nullable=False),
        sa.Column("owner_name", sa.String(length=255), nullable=False),
        sa.Column("is_director", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_officer", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("officer_title", sa.String(length=255), nullable=True),
        sa.Column(
            "is_ten_percent_owner",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("is_other", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("other_text", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "reporting_owner_cik ~ '^[0-9]{1,10}$'",
            name="ck_insider_reporting_owners_cik_digits",
        ),
        sa.ForeignKeyConstraint(
            ["filing_id"], ["sec_filings.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "filing_id",
            "reporting_owner_cik",
            name="uq_insider_reporting_owners_filing_id_reporting_owner_cik",
        ),
    )


def downgrade() -> None:
    # Reverse dependency order. Indexes go with their tables.
    op.drop_table("insider_reporting_owners")
    op.drop_table("insider_transactions")
    op.drop_table("sec_filings")
    op.drop_table("daily_prices")
    op.drop_table("companies")