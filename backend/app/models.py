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
    """

    __tablename__ = "portfolios"
    __table_args__ = (
        CheckConstraint("cash_balance >= 0", name="ck_portfolios_cash_balance_nonnegative"),
        UniqueConstraint("name", name="uq_portfolios_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # 2 decimal places: cash is genuinely counted in cents.
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))

    holdings: Mapped[list["Holding"]] = relationship(
        back_populates="portfolio",
        order_by="Holding.symbol",
    )


class Holding(Base):
    """A long position in one symbol within one portfolio."""

    __tablename__ = "holdings"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_holdings_quantity_positive"),
        CheckConstraint(
            "average_buy_price > 0", name="ck_holdings_average_buy_price_positive"
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
