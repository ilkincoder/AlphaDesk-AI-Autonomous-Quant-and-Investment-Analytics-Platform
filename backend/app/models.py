"""SQLAlchemy models for portfolios, holdings, market data, and SEC filings."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Portfolio(Base):
    """A single portfolio of cash plus long positions.

    Money uses NUMERIC, never FLOAT: binary floating point cannot represent values
    like 0.10 exactly, so summing cash in floats drifts by tiny amounts.

    **A portfolio is either linked to a broker account or it is not, and there is no
    half-way house.** `broker`, `broker_account_id`, `broker_equity` and `last_synced_at`
    are written together by one successful sync and read together by everything that
    values the portfolio, so "linked but unpriced" cannot exist to be handled. That is
    what `ck_portfolios_broker_link_is_whole` asserts.

    `cash_balance` is non-negative only while the portfolio is unlinked. That is the
    original constraint, kept where it still means something: the placeholder portfolio's
    cash is a figure this application chose, and a negative one there is a bug. A linked
    portfolio's cash is whatever the broker reports, and a margin paper account can report
    a negative balance, so refusing to store it would mean refusing to synchronise at all.
    """

    __tablename__ = "portfolios"
    __table_args__ = (
        UniqueConstraint("name", name="uq_portfolios_name"),
        CheckConstraint(
            "broker IS NOT NULL OR cash_balance >= 0",
            name="ck_portfolios_unlinked_cash_balance_nonnegative",
        ),
        CheckConstraint(
            "(broker IS NULL) = (broker_account_id IS NULL) "
            "AND (broker IS NULL) = (broker_equity IS NULL) "
            "AND (broker IS NULL) = (last_synced_at IS NULL)",
            name="ck_portfolios_broker_link_is_whole",
        ),
        # One broker account binds one portfolio. Without this, two portfolios could
        # claim the same account and the sync would have to guess which to update.
        UniqueConstraint(
            "broker", "broker_account_id", name="uq_portfolios_broker_account"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # 2 decimal places: cash is genuinely counted in cents.
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))

    # --- the broker link, absent until the first successful sync -------------------------
    #
    # A source slug such as "alpaca_paper". Unconstrained for the same reason
    # `daily_prices.provider` is: a second broker should not need a migration.
    broker: Mapped[str | None] = mapped_column(String(32))
    # The broker's own account identifier -- Alpaca's account uuid. This is the identity
    # a sync is checked against, which is what stops a set of credentials pointed at a
    # different account quietly writing into this portfolio.
    broker_account_id: Mapped[str | None] = mapped_column(String(64))
    # The broker's own equity figure, in the portfolio's currency. Kept because it is the
    # broker's answer to "what is this account worth", and it is not derivable here to the
    # cent: the account and positions endpoints are two moments, so equity need not equal
    # positions plus cash exactly.
    broker_equity: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    # When the last sync that actually wrote started reading from the broker. Also the
    # stale guard: a fetch that began earlier than this is refused rather than allowed to
    # overwrite newer data.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    holdings: Mapped[list["Holding"]] = relationship(
        back_populates="portfolio",
        order_by="Holding.symbol",
    )


class Holding(Base):
    """A long position in one symbol within one portfolio.

    `quantity` and `average_buy_price` describe the position; `market_price` and
    `market_value` are where the broker last priced it. The second pair is NULL on a
    portfolio that has never been synchronised -- a demo holding has a purchase price and
    no market price at all, which is a different fact from a market price of zero.

    They are stored rather than derived because the broker reported them: `market_value`
    is the broker's own number, and computing it here from `quantity * market_price`
    would replace what was reported with what this application thinks it should be.
    """

    __tablename__ = "holdings"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_holdings_quantity_positive"),
        CheckConstraint(
            "average_buy_price > 0", name="ck_holdings_average_buy_price_positive"
        ),
        CheckConstraint(
            "market_price IS NULL OR market_price >= 0",
            name="ck_holdings_market_price_nonnegative",
        ),
        CheckConstraint(
            "market_value IS NULL OR market_value >= 0",
            name="ck_holdings_market_value_nonnegative",
        ),
        UniqueConstraint("portfolio_id", "symbol", name="uq_holdings_portfolio_id_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE")
    )
    symbol: Mapped[str] = mapped_column(String(20))
    # 6 decimal places, so fractional shares remain representable later.
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    # 4 decimal places: this is an average, and averaging can produce more than
    # two decimals (100 / 3), so rounding to cents would quietly lose precision.
    average_buy_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))

    # Unconstrained NUMERIC, for the same reason daily_prices' prices are: what the
    # provider sent is what gets stored, and no precision is lost on the way in.
    market_price: Mapped[Decimal | None] = mapped_column(Numeric)
    market_value: Mapped[Decimal | None] = mapped_column(Numeric)

    portfolio: Mapped["Portfolio"] = relationship(back_populates="holdings")


class Company(Base):
    """A US-listed company, independent of any portfolio.

    Scope: US equities, one primary listing per company. That is why `ticker` is
    unique on its own -- a company listed on two exchanges at once is out of scope.

    Deliberately unrelated to `Holding`. A company is researched whether or not
    anyone holds it, so there is no foreign key from `holdings` to here and no
    relationship between the two: a holding is a position in a portfolio, and
    matching a holding to a company is a query someone writes, not a constraint.
    """

    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "sec_issuer_cik ~ '^[0-9]{1,10}$'",
            name="ck_companies_sec_issuer_cik_digits",
        ),
        UniqueConstraint("ticker", name="uq_companies_ticker"),
        UniqueConstraint("sec_issuer_cik", name="uq_companies_sec_issuer_cik"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    ticker: Mapped[str] = mapped_column(String(20))
    exchange: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3))
    # TEXT, never an integer: SEC CIKs are ten digits carrying leading zeros (Apple
    # is 0000320193), and an integer column would quietly store 320193 instead.
    #
    # This is the *issuer* CIK -- the company that filed. A reporting owner's CIK is
    # a different number about a different entity, and lives on InsiderReportingOwner.
    sec_issuer_cik: Mapped[str] = mapped_column(Text)


class DailyPrice(Base):
    """One trading session's prices for one company, as one provider reported them.

    Raw and adjusted prices are different numbers describing the same day. They are kept
    apart by `adjustment_basis`, and the provider's exact mode is kept separately in
    `provider_adjust_mode` -- both are in the unique key, so two adjustment modes for one
    day can neither collide nor overwrite each other.

    Prices are plain `NUMERIC`, not `NUMERIC(18,6)`. Twelve Data returns values needing a
    seventh decimal place (Step 2 measured 6 of 480 prices in a 120-session window), and
    rounding them on the way in would discard real data while leaving everything
    downstream looking perfectly normal. The CHECK constraints still bound what is valid;
    only the column type is loosened.
    """

    __tablename__ = "daily_prices"
    __table_args__ = (
        # Together these reject any incoherent bar, and no other price check is
        # needed: `low > 0` plus both range checks already forces all four prices
        # positive, and also implies `high >= low`, because an inverted range can
        # contain neither the open nor the close.
        #
        # `high >= low` is therefore implied rather than independently load-bearing.
        # It is kept because it states the invariant outright, and because it is
        # sometimes the one PostgreSQL chooses to report.
        CheckConstraint("low > 0", name="ck_daily_prices_low_positive"),
        CheckConstraint("high >= low", name="ck_daily_prices_high_gte_low"),
        CheckConstraint(
            "open >= low AND open <= high", name="ck_daily_prices_open_within_low_high"
        ),
        CheckConstraint(
            "close >= low AND close <= high",
            name="ck_daily_prices_close_within_low_high",
        ),
        CheckConstraint("volume >= 0", name="ck_daily_prices_volume_nonnegative"),
        CheckConstraint(
            "adjustment_basis IN ('raw', 'adjusted')",
            name="ck_daily_prices_adjustment_basis",
        ),
        # One bar per company per day per provider per basis per mode. Anything coarser
        # would collide raw with adjusted, or splits with dividends; anything finer would
        # allow the same bar twice.
        #
        # `company_date` rather than `company_id_trading_date`: spelling out every
        # column pushes the name past PostgreSQL's 63-character identifier limit, and
        # a truncated name is not one anything can refer to later.
        UniqueConstraint(
            "company_id",
            "trading_date",
            "provider",
            "adjustment_basis",
            "provider_adjust_mode",
            name="uq_daily_prices_company_date_provider_basis_mode",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
    )
    trading_date: Mapped[date] = mapped_column(Date)
    # Unconstrained NUMERIC, so the provider's own precision is what gets stored.
    open: Mapped[Decimal] = mapped_column(Numeric)
    high: Mapped[Decimal] = mapped_column(Numeric)
    low: Mapped[Decimal] = mapped_column(Numeric)
    close: Mapped[Decimal] = mapped_column(Numeric)
    # BigInteger, not Numeric: volume counts shares, it is not money, so it is a
    # whole number. bigint rather than integer because a busy day on a large cap
    # exceeds the 2,147,483,647 an integer allows.
    volume: Mapped[int] = mapped_column(BigInteger)
    # A source slug such as "twelve_data". Deliberately not constrained to a fixed
    # list: adding a provider should not require a migration. The unique key above
    # is what stops one provider's bar being mistaken for another's.
    provider: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3))
    # Our vocabulary: "raw" or "adjusted".
    adjustment_basis: Mapped[str] = mapped_column(String(16))
    # The provider's own mode, e.g. "splits". Kept separately so a future mode change is
    # visible rather than folded into one of the two words above.
    provider_adjust_mode: Mapped[str] = mapped_column(String(16))
    # NULL means the provider said nothing about volume adjustment, which is a different
    # fact from "volume is not adjusted". Twelve Data currently says nothing, so this is
    # NULL on every row ingested today -- recorded rather than silently collapsed.
    volume_adjustment: Mapped[str | None] = mapped_column(String(16))
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SecFiling(Base):
    """One EDGAR filing, and the identity its transactions hang from.

    Three separate times, kept deliberately apart:

    * `filing_date` -- the SEC's own calendar date for the filing.
    * `acceptance_datetime` -- the moment EDGAR accepted it, when the source supplies
      one. Nullable, because not every source does.
    * `retrieved_at` -- when *we* fetched it.

    None of these is the `transaction_date` on InsiderTransaction, and none of them is
    the moment the market learned anything. Collapsing them into one column is what
    would make "what did the public know, and when" unanswerable later.
    """

    __tablename__ = "sec_filings"
    __table_args__ = (
        CheckConstraint(
            "amends_filing_id IS NULL OR amends_filing_id <> id",
            name="ck_sec_filings_not_self_amending",
        ),
        UniqueConstraint(
            "accession_number", name="uq_sec_filings_accession_number"
        ),
        # Not covered by the accession_number unique index, which is the only other
        # index on this table.
        Index("ix_sec_filings_company_id_filing_date", "company_id", "filing_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
    )
    accession_number: Mapped[str] = mapped_column(String(25))
    form_type: Mapped[str] = mapped_column(String(16))
    filing_date: Mapped[date] = mapped_column(Date)
    # The period the filing reports on -- a quarter's end, a fiscal year's end. Step 4 left
    # this out because a Form 4's own XML carries `periodOfReport` and is authoritative for
    # it; a 10-K has no such XML, so for a disclosure filing the feed's `reportDate` is the
    # only source and there has to be somewhere to put it.
    report_date: Mapped[date | None] = mapped_column(Date)
    acceptance_datetime: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    source_document_url: Mapped[str] = mapped_column(String(512))
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Stored, never inferred, and never matched automatically in this milestone:
    # deciding that one accession number amends another is a judgement for a later
    # step, and a guess here would silently double-count transactions.
    is_amendment: Mapped[bool] = mapped_column(Boolean, server_default=false())
    amends_filing_id: Mapped[int | None] = mapped_column(
        ForeignKey("sec_filings.id", ondelete="SET NULL")
    )

    # The document's own words, kept beside the feed's. `form_type` above is what
    # discovery selected on; this is what the XML says it is.
    document_type: Mapped[str] = mapped_column(String(16))
    schema_version: Mapped[str | None] = mapped_column(String(16))
    # Set only when the document states it, which in practice means only on a 4/A.
    date_of_original_submission: Mapped[date | None] = mapped_column(Date)
    # True / False / None. None is "the document did not say", matching the parser.
    rule_10b5_1: Mapped[bool | None] = mapped_column(Boolean)
    # Holding rows (`nonDerivativeHolding`, `derivativeHolding`) are not transactions.
    # Counted so the exclusion from `insider_transactions` is visible rather than silent.
    #
    # NULL on a filing no Form 4 parser ran on -- a count of 0 would imply one did.
    holding_rows_skipped: Mapped[int | None] = mapped_column()
    remarks: Mapped[str | None] = mapped_column(Text)
    # The document's footnote map, id -> text. Supplementary detail rather than anything
    # filtered on, so JSONB; ids and text only, so no Decimal needs serialising.
    footnotes: Mapped[dict] = mapped_column(JSONB)
    # The source document, kept so a filing can be inspected or reprocessed without
    # going back to the SEC for it -- and so a later parser change can be re-run over
    # exactly what produced today's rows.
    #
    # NULL on a filing that has no ownership document. A 10-K is not an empty Form 4, and
    # its HTML lives in `filing_documents.content` rather than here.
    source_xml: Mapped[str | None] = mapped_column(Text)
    source_xml_sha256: Mapped[str | None] = mapped_column(String(64))


class InsiderTransaction(Base):
    """One reported transaction row, from a Form 4's non-derivative or derivative table.

    Row identity is *provenance*, not content: `(filing_id, source_table,
    row_position)`. Two genuinely separate transactions can share a transaction date
    and a share count, so neither of those, nor both together, is part of the key.

    `transaction_code` is stored exactly as EDGAR reports it and is deliberately not
    constrained to a list. Grants (A), gifts (G), option exercises (M) and tax
    withholding (F) have to stay distinguishable, and a closed list would reject a
    code EDGAR introduces later.
    """

    __tablename__ = "insider_transactions"
    __table_args__ = (
        CheckConstraint(
            "source_table IN ('nonDerivativeTable', 'derivativeTable')",
            name="ck_insider_transactions_source_table",
        ),
        CheckConstraint(
            "row_position >= 0", name="ck_insider_transactions_row_position_nonnegative"
        ),
        # `is_derivative` is redundant with `source_table` on purpose: it lets a query
        # filter derivatives without knowing EDGAR's XML element names. The check is
        # what stops the two columns ever contradicting each other.
        CheckConstraint(
            "is_derivative = (source_table = 'derivativeTable')",
            name="ck_insider_transactions_is_derivative_matches_source_table",
        ),
        CheckConstraint(
            "acquired_disposed IN ('A', 'D')",
            name="ck_insider_transactions_acquired_disposed",
        ),
        CheckConstraint("shares > 0", name="ck_insider_transactions_shares_positive"),
        # `>= 0`, not `> 0`: a price reported as zero is a real (if odd) reported
        # value and must be storable. NULL is likewise allowed and stays NULL --
        # "no price reported" and "price reported as zero" are different facts, and
        # a default of 0 would erase the difference.
        CheckConstraint(
            "price_per_share IS NULL OR price_per_share >= 0",
            name="ck_insider_transactions_price_per_share_nonnegative",
        ),
        CheckConstraint(
            "ownership_direct_indirect IN ('D', 'I')",
            name="ck_insider_transactions_ownership_direct_indirect",
        ),
        UniqueConstraint(
            "filing_id",
            "source_table",
            "row_position",
            name="uq_insider_transactions_filing_id_source_table_row_position",
        ),
        Index("ix_insider_transactions_transaction_date", "transaction_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(
        ForeignKey("sec_filings.id", ondelete="CASCADE")
    )
    source_table: Mapped[str] = mapped_column(String(24))
    # 0-based index of the transaction element within its source table, in document
    # order. Stable for a given accession number, which is what makes it usable as
    # part of the identity above.
    row_position: Mapped[int] = mapped_column()
    is_derivative: Mapped[bool] = mapped_column(Boolean)
    transaction_date: Mapped[date] = mapped_column(Date)
    security_title: Mapped[str] = mapped_column(String(255))
    # The raw EDGAR code: "P", "S", "A", "M", "G", "F" and so on. See the class
    # docstring for why this is not constrained.
    transaction_code: Mapped[str] = mapped_column(String(4))
    acquired_disposed: Mapped[str] = mapped_column(String(1))
    # Unconstrained NUMERIC for the same reason as the prices: the source's own precision
    # is what gets stored. `shares > 0` and `price_per_share >= 0` still bound both.
    shares: Mapped[Decimal] = mapped_column(Numeric)
    price_per_share: Mapped[Decimal | None] = mapped_column(Numeric)
    # Nullable because the parser returns None when a document omits `ownershipNature`.
    # The CHECK above still holds for every value that is present.
    ownership_direct_indirect: Mapped[str | None] = mapped_column(String(1))
    # The ownership description where the document gives one.
    nature_of_ownership: Mapped[str | None] = mapped_column(Text)
    # The position after the trade, when reported.
    shares_owned_following: Mapped[Decimal | None] = mapped_column(Numeric)
    # Field-level footnote references: [{"field": ..., "footnote_id": ...}]. Kept beside
    # the resolved text below so the attribution survives, not only the prose.
    footnote_refs: Mapped[list] = mapped_column(JSONB)
    # Whatever the source says is needed to read this row correctly -- the resolved text
    # of the footnotes this row references. Free text, because a footnote is prose.
    footnotes: Mapped[str | None] = mapped_column(Text)

    # Derivative-only. NULL throughout on a non-derivative row.
    underlying_security_title: Mapped[str | None] = mapped_column(String(255))
    underlying_shares: Mapped[Decimal | None] = mapped_column(Numeric)
    exercise_price: Mapped[Decimal | None] = mapped_column(Numeric)
    expiration_date: Mapped[date | None] = mapped_column(Date)


class InsiderReportingOwner(Base):
    """A person or entity who signed a filing, and their relationship to the issuer.

    Attached to the *filing*, not to a transaction. In Form 4 XML `reportingOwner` is
    a sibling of the transaction tables, not a child of any transaction, so a filing
    with two owners and five transactions is two owners and five transactions -- the
    owners are joint filers, and EDGAR never says which of them a given row belongs to.

    The relationship columns mirror the source's own `reportingOwnerRelationship`
    element exactly. They are EDGAR's fields, not classifications invented here.

    .. warning::
       Joining InsiderTransaction to this table through SecFiling **repeats each
       transaction once per owner**. Two owners on a filing with five transactions
       yields ten rows. Any total -- shares, transaction count, transaction value --
       must be computed from the transaction side, or over DISTINCT
       `insider_transactions.id`, and never from the fanned-out join.
    """

    __tablename__ = "insider_reporting_owners"
    __table_args__ = (
        CheckConstraint(
            "reporting_owner_cik ~ '^[0-9]{1,10}$'",
            name="ck_insider_reporting_owners_cik_digits",
        ),
        UniqueConstraint(
            "filing_id",
            "reporting_owner_cik",
            name="uq_insider_reporting_owners_filing_id_reporting_owner_cik",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(
        ForeignKey("sec_filings.id", ondelete="CASCADE")
    )
    # TEXT for the same reason as Company.sec_issuer_cik -- leading zeros -- but this
    # is the *reporting owner's* CIK, a different number about a different entity.
    reporting_owner_cik: Mapped[str] = mapped_column(Text)
    owner_name: Mapped[str] = mapped_column(String(255))
    # Nullable, with no server default. The parser returns None when a document omits a
    # flag, and NULL says "the document did not say" -- whereas `false` would assert a
    # fact nobody stated. This mirrors how `rule_10b5_1` already distinguishes the two.
    is_director: Mapped[bool | None] = mapped_column(Boolean)
    is_officer: Mapped[bool | None] = mapped_column(Boolean)
    officer_title: Mapped[str | None] = mapped_column(String(255))
    is_ten_percent_owner: Mapped[bool | None] = mapped_column(Boolean)
    is_other: Mapped[bool | None] = mapped_column(Boolean)
    other_text: Mapped[str | None] = mapped_column(String(255))


class IngestionRun(Base):
    """A record that one bounded ingestion happened, over what scope, and what it found.

    Small on purpose. It exists so a later reader can tell a three-filing sample apart
    from complete coverage, and so the numbers behind a set of rows survive somewhere
    other than a terminal window.

    It is written inside the ingestion transaction, so a run that fails leaves no trace
    of itself -- which is the honest outcome, because nothing it wrote survived either.
    This is a receipt, not a job queue: there are no states, no retries, and no scheduler.
    """

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
    )
    # What kind of run this was. It exists so a company-context run can never be mistaken
    # for insider coverage: a Form 4 analysis reading "the latest run" would otherwise pick
    # up a disclosure run's three filings and present them as insider-history coverage.
    #
    # The default is an accurate backfill rather than an invented value -- every run
    # written before this column existed was a Form 4 run.
    scope: Mapped[str] = mapped_column(String(32), server_default="form4")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # What was asked for: bar and filing counts, and whether it was a dry run.
    parameters: Mapped[dict] = mapped_column(JSONB)
    # What came back and what was written, including the coverage caveats.
    summary: Mapped[dict] = mapped_column(JSONB)


class FilingDocument(Base):
    """One document inside a filing: its primary document, or an exhibit.

    A filing has one primary document and may have several exhibits, which is why this is a
    table rather than more columns on `sec_filings`.

    `content` holds whatever the SEC served -- HTML for a 10-K or an earnings release. It is
    kept alongside `extracted_text` rather than instead of it, so the extraction can be
    improved and re-run without going back to the SEC for the document again. `source_xml`
    on `sec_filings` keeps its own meaning: that column is Form 4 ownership XML, and no HTML
    is ever put in it.

    `sections` records where the extractor found the named sections, as character offsets
    into `extracted_text`. An empty mapping means none were detected, which is reported
    rather than passed off as a document with no sections.
    """

    __tablename__ = "filing_documents"
    __table_args__ = (
        CheckConstraint("role IN ('primary', 'exhibit')", name="ck_filing_documents_role"),
        CheckConstraint(
            "extraction_status IN ('extracted', 'unsupported', 'too_large', 'failed')",
            name="ck_filing_documents_extraction_status",
        ),
        UniqueConstraint(
            "filing_id", "document_name", name="uq_filing_documents_filing_id_document_name"
        ),
        Index("ix_filing_documents_filing_id_role", "filing_id", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int] = mapped_column(
        ForeignKey("sec_filings.id", ondelete="CASCADE")
    )
    document_name: Mapped[str] = mapped_column(String(255))
    # The SEC's own designation: "10-K" for a primary document, "EX-99.1" for an exhibit.
    document_type: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(16))
    # Position in the filing's index, when the index listed one.
    sequence: Mapped[int | None] = mapped_column()
    source_url: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_version: Mapped[str | None] = mapped_column(String(16))
    extraction_status: Mapped[str] = mapped_column(String(32))
    extraction_limitations: Mapped[str | None] = mapped_column(Text)
    sections: Mapped[dict] = mapped_column(JSONB)


class DocumentIndexManifest(Base):
    """Which documents are completely in the search index, and at what identity.

    It exists because the vector store cannot answer either question on its own. Qdrant knows
    it holds points for document 7, but not whether all of document 7's points arrived; and a
    collection built with a different 384-dimensional model looks exactly like one built with
    this one, because dimensions do not name a model.

    **A row here means every point for that document was acknowledged.** The write is the last
    thing that happens for a document, so an interrupted run leaves points in Qdrant and no
    row here -- which reads as "not indexed", and is the safe direction to be wrong in.
    Retrieval ignores documents that are not listed, so a half-written document is never
    served. A retry re-upserts the same deterministic point ids and converges.

    The five identity columns are what a stale index is detected by. Any of them changing
    means the stored vectors no longer describe the stored text, and the run stops rather than
    mixing the two.
    """

    __tablename__ = "document_index_manifest"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_document_index_manifest_document_id"),
        Index("ix_document_index_manifest_collection_name", "collection_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("filing_documents.id", ondelete="CASCADE")
    )
    collection_name: Mapped[str] = mapped_column(String(64))
    embedding_model: Mapped[str] = mapped_column(String(128))
    chunking_version: Mapped[str] = mapped_column(String(16))
    # The two hashes the brief requires on every passage: what the SEC served, and what
    # extraction made of it.
    content_sha256: Mapped[str] = mapped_column(String(64))
    extracted_text_sha256: Mapped[str] = mapped_column(String(64))
    extraction_version: Mapped[str | None] = mapped_column(String(16))
    point_count: Mapped[int] = mapped_column()
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CompanyFactSnapshot(Base):
    """One fetch of an issuer's Company Facts JSON, kept whole.

    Deduplicated by content hash, so re-running against an unchanged endpoint adds nothing.
    A *changed* response is a new snapshot rather than a conflict: that endpoint grows as
    filings are added, and treating growth as a conflict would make the command unusable.

    Facts reference the snapshot they were first seen in, but the snapshot is provenance
    and not identity -- a fresh snapshot of unchanged data must not duplicate every fact.
    """

    __tablename__ = "company_fact_snapshots"
    __table_args__ = (
        UniqueConstraint("content_sha256", name="uq_company_fact_snapshots_content_sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
    )
    source_url: Mapped[str] = mapped_column(String(512))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column()


class FinancialFact(Base):
    """One reported value of one concept, for one period, as one filing reported it.

    Observations are never collapsed into a single value per concept and end date. NVDA's
    `Revenues` carries both a quarter (`2026-04-27` to `2026-07-26`) and a year-to-date
    figure (`2026-01-26` to `2026-07-26`) ending on the same day, and both are real. The
    same period also appears in more than one filing as a revised comparative, and those are
    two facts from two documents.

    That is why the natural key carries `period_start` and `accession_number` as well as the
    period end. `fp` alone proves nothing: both of those observations report `fp = Q2`.

    The key is declared NULLS NOT DISTINCT because instant facts -- `Assets` on a balance
    sheet -- have no `period_start` at all. Under PostgreSQL's default every NULL is
    distinct, so without this the same instant fact would be accepted again on every run.
    """

    __tablename__ = "financial_facts"
    __table_args__ = (
        UniqueConstraint(
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
        Index(
            "ix_financial_facts_company_id_concept_period_end",
            "company_id",
            "concept",
            "period_end",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
    )
    # Which fetch this observation was first seen in. Provenance, not identity.
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("company_fact_snapshots.id", ondelete="CASCADE")
    )
    taxonomy: Mapped[str] = mapped_column(String(16))
    concept: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32))
    # Unconstrained NUMERIC, for the same reason the prices are: the reported value is
    # stored exactly as reported, and no precision is lost on the way in.
    #
    # Deliberately no non-negative check. Net income is negative in a loss-making period,
    # and a constraint that assumed otherwise would reject real filings.
    value: Mapped[Decimal] = mapped_column(Numeric)
    # NULL for an instant fact, set for a duration fact.
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    accession_number: Mapped[str | None] = mapped_column(String(25))
    form: Mapped[str | None] = mapped_column(String(16))
    filed_date: Mapped[date | None] = mapped_column(Date)
    fiscal_year: Mapped[int | None] = mapped_column()
    fiscal_period: Mapped[str | None] = mapped_column(String(8))
    frame: Mapped[str | None] = mapped_column(String(32))


class NewsArticle(Base):
    """One article, from a market news provider or from an official statistical release.

    **`published_at` and `ingested_at` are two different facts.** The first is when the
    publisher says the story ran, and it is what a listing is ordered by; the second is when
    this database first stored it. They coincide only for an article that has just broken.
    Nothing here ever writes one into the other: an old release ingested today must still
    read as old, and the timestamp that makes it look new is the one this system controls
    rather than the one the publisher stated.

    **Identity is `(provider, provider_article_id)`.** `provider_article_id` holds the
    provider's own identifier, and falls back to the canonical URL when a provider supplies
    none -- one column, one rule, so there is no second way for two rows to be the same
    article. `content_sha256` is a *different* question: whether the words changed. A
    provider that revises a story keeps its id and gets a new hash, which is what makes the
    stored copy update rather than duplicate, and what tells the indexer to re-embed.

    **The index identity lives here rather than in a manifest table**, which is a deliberate
    difference from `DocumentIndexManifest`. A filing owns many documents, each separately
    indexable; a news article *is* the indexed unit, one row to one set of chunks. A separate
    table would be a join that can only ever return the row it was joined from.

    `indexed_at` NULL is the honest state for an article that is stored but not yet in the
    search index -- a state an indexing failure leaves behind, and one a retry clears.
    """

    __tablename__ = "news_articles"
    __table_args__ = (
        CheckConstraint(
            "category IN ('company', 'macro')", name="ck_news_articles_category"
        ),
        UniqueConstraint(
            "provider",
            "provider_article_id",
            name="uq_news_articles_provider_provider_article_id",
        ),
        # The listing is always "newest first", across every source.
        Index("ix_news_articles_published_at", "published_at"),
        Index("ix_news_articles_provider_published_at", "provider", "published_at"),
        # Symbol filtering, which is what makes a company article findable. A GIN index
        # rather than a join table: the list is small, read whole, and never queried on its
        # own.
        Index(
            "ix_news_articles_symbols",
            "symbols",
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # A source slug such as "alpaca_news" or "official_fed_monetary". Unconstrained for the
    # same reason `daily_prices.provider` is: a new source should not need a migration.
    provider: Mapped[str] = mapped_column(String(48))
    provider_article_id: Mapped[str] = mapped_column(String(512))
    # The publisher's name as it should be shown -- "benzinga", "Federal Reserve", "BLS".
    source: Mapped[str] = mapped_column(String(120))
    canonical_url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(Text)
    # The publisher's words, markup removed. Untrusted input, stored as text and read as
    # text: nothing in this system executes it or resolves anything it mentions.
    text: Mapped[str] = mapped_column(Text)
    # A JSONB list rather than a join table. Empty for a macro release, which is a fact about
    # the article rather than missing data.
    symbols: Mapped[list] = mapped_column(JSONB)
    category: Mapped[str] = mapped_column(String(16))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The provider's own `updated_at`, when it supplied one. NULL means the provider said
    # nothing about revisions, which is a different fact from "never revised".
    provider_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # sha256 of the title and body. What "this article changed" means.
    content_sha256: Mapped[str] = mapped_column(String(64))

    # --- the search index, as far as this article is concerned ---------------------------
    #
    # Written together, last, after every point for this article was acknowledged. Any of
    # them NULL means the article is not completely indexed, and retrieval ignores it --
    # which is the safe direction to be wrong in.
    indexed_content_sha256: Mapped[str | None] = mapped_column(String(64))
    indexed_embedding_model: Mapped[str | None] = mapped_column(String(128))
    indexed_chunking_version: Mapped[str | None] = mapped_column(String(16))
    indexed_point_count: Mapped[int | None] = mapped_column()
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsIngestionRun(Base):
    """A receipt for one news ingestion: when it ran, and what each source did.

    It exists for one question a row count cannot answer: **when did this source last
    succeed?** A feed that was read successfully and had nothing new to say changes no rows
    at all, and deriving "last success" from `news_articles.ingested_at` would report it as
    stale forever. So success is recorded here, where a zero-article read is still a row.

    Small on purpose, like `IngestionRun`, and written once per run after every source has
    been tried -- including the ones that failed, whose error is recorded beside the rest.
    There is no company to attach it to: a macro release is nobody's filing.
    """

    __tablename__ = "news_ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The sources that were asked, in order, so a source that is not mentioned reads as
    # "never attempted" rather than "attempted and produced nothing".
    sources: Mapped[list] = mapped_column(JSONB)
    # Per source: status, counts, and the sanitized error where there was one.
    results: Mapped[dict] = mapped_column(JSONB)


class Conversation(Base):
    """One conversation: a sequence of turns about a company, carried across requests.

    The row holds two things beyond its identity: **the settled context** the run has agreed
    on (which company, which market window, what was knowable) and **the pending
    clarification** if the last turn ended by asking the user something. Both are what makes
    a follow-up turn continue the earlier request instead of starting a new one.

    **`processing_turn_id` is a lease, not a lock.** A turn claims the conversation by writing
    its own id and a deadline here; it releases the claim when it finishes, and only if the
    claim is still its own -- so a turn that overran its deadline cannot come back later and
    overwrite context that a newer turn has already moved on from. Nothing holds a database
    transaction open while a model is thinking: the claim is committed, and the model call
    happens with no transaction in flight at all.

    A conversation id separates conversations from each other. It is **not** an authorization
    boundary -- see the README on what this local, single-user build does not provide.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        # The claim and its deadline are written together or not at all. A turn id with no
        # deadline would be a claim nothing could ever expire.
        CheckConstraint(
            "(processing_turn_id IS NULL) = (processing_deadline IS NULL)",
            name="ck_conversations_lease_is_whole",
        ),
        # A pending clarification is a question, the date it was asked about, and what was
        # missing. Any one of them without the others cannot be resumed.
        CheckConstraint(
            "(pending_question IS NULL) = (pending_reference_date IS NULL)",
            name="ck_conversations_pending_is_whole",
        ),
    )

    # A uuid4 hex, generated here rather than by the client. Not a sequence: a small integer
    # would let anyone holding one id read the neighbouring conversations by guessing.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # --- the settled context, carried forward -------------------------------------------
    #
    # Nullable throughout: a conversation that has only ever been greeted has settled nothing.
    settled_symbol: Mapped[str | None] = mapped_column(String(20))
    settled_start_date: Mapped[date | None] = mapped_column(Date)
    settled_end_date: Mapped[date | None] = mapped_column(Date)
    settled_as_of: Mapped[date | None] = mapped_column(Date)

    # --- the pending clarification, if the last turn asked one ---------------------------
    #
    # `pending_question` is the *original* analytical question, not the reply. A reply of
    # "August 6 to September 17" means nothing on its own; what it answers is the question
    # that came before it, and that is what has to be resumed.
    pending_question: Mapped[str | None] = mapped_column(Text)
    # The reference date the original question was asked against. Kept so a reply arriving
    # days later still resolves "last quarter" against the date it was actually asked about,
    # rather than silently sliding to today.
    pending_reference_date: Mapped[date | None] = mapped_column(Date)
    # What the Supervisor asked for, so a resume can restate it.
    pending_asked_for: Mapped[str | None] = mapped_column(Text)

    # --- the lease ------------------------------------------------------------------------
    processing_turn_id: Mapped[str | None] = mapped_column(String(32))
    processing_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    turns: Mapped[list["ConversationTurn"]] = relationship(
        back_populates="conversation",
        order_by="ConversationTurn.sequence",
        cascade="all, delete-orphan",
    )


class ConversationTurn(Base):
    """One user message and what came back.

    Written in two steps, deliberately. The row is **committed before the analysis starts**,
    so a client that retries, or a second request that arrives while this one is running, can
    see that the turn exists and what state it is in. The result is written afterwards, in a
    second short transaction that also updates the conversation's carried context.

    **The evidence lives inside `result`, scoped to this turn's run.** An `E1` in one turn's
    citation list belongs to that turn's run and is meaningless anywhere else; nothing ever
    merges evidence across turns, so one run's `E1` cannot resolve to another's.

    `status` is deliberately not constrained to a list. The run's vocabulary
    (`app/agent/run.py`) grows as the analysis does, and a status that needed a migration to
    be recorded would be a status somebody records wrongly instead. Everything written here
    comes from that module's constants.
    """

    __tablename__ = "conversation_turns"
    __table_args__ = (
        # One request id means one turn, per conversation. This is the constraint the whole
        # duplicate-suppression story rests on: a retry cannot create a second turn, so it
        # cannot cause a second paid analysis.
        UniqueConstraint(
            "conversation_id", "request_id", name="uq_conversation_turns_request"
        ),
        # Turns are ordered by this, and two turns cannot share a place in the order.
        UniqueConstraint(
            "conversation_id", "sequence", name="uq_conversation_turns_sequence"
        ),
        # History is read newest-first for a page and oldest-first for the context window.
        Index(
            "ix_conversation_turns_conversation_id_sequence",
            "conversation_id",
            "sequence",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    # Chosen by the client so it can retry safely. Unique within the conversation, not
    # globally: two clients that both start at "1" are not in conflict.
    request_id: Mapped[str] = mapped_column(String(64))
    # 1-based position in the conversation, assigned by the server.
    sequence: Mapped[int] = mapped_column()

    user_message: Mapped[str] = mapped_column(Text)
    # What the client stated outright for this turn, if anything. These are authoritative for
    # the turn; the conversation's settled context is only a default it can override.
    request_symbol: Mapped[str | None] = mapped_column(String(20))
    request_start_date: Mapped[date | None] = mapped_column(Date)
    request_end_date: Mapped[date | None] = mapped_column(Date)
    request_as_of: Mapped[date | None] = mapped_column(Date)
    # The date relative periods were resolved against. Server-supplied, and for a resumed
    # clarification it is the *original* date, not today's.
    reference_date: Mapped[date] = mapped_column(Date)

    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When an unfinished turn stops being believed. Past this, a new request recovers the
    # conversation instead of being told to wait for work that is no longer happening.
    processing_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The analysis run this turn produced, if one ran.
    run_id: Mapped[str | None] = mapped_column(String(64))
    # The Supervisor's answer, including a clarification question. Null when there is none.
    answer: Mapped[str | None] = mapped_column(Text)

    # What this turn resolved to, so a later turn can carry it forward without re-deriving it.
    resolved_symbol: Mapped[str | None] = mapped_column(String(20))
    resolved_start_date: Mapped[date | None] = mapped_column(Date)
    resolved_end_date: Mapped[date | None] = mapped_column(Date)
    resolved_as_of: Mapped[date | None] = mapped_column(Date)

    # The public structured result: citations with their filing metadata, the evidence list,
    # limitations, tool statuses and usage. Public on purpose -- no prompt text, no model
    # reasoning, no credentials. A failure's message is sanitised before it gets here.
    result: Mapped[dict | None] = mapped_column(JSONB)
    # Why the turn did not complete, in a sentence a user could be shown. NULL when it did.
    failure: Mapped[str | None] = mapped_column(Text)

    conversation: Mapped["Conversation"] = relationship(back_populates="turns")
