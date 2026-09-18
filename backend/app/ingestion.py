"""Persisting fetched market and SEC data, once, without duplicating it on a re-run.

This module holds the mapping from what the clients return to what the tables store, and
the rule that decides what a second run is allowed to do.

**The mapping.** In outline:

* `DailyPriceSeries` → `companies` (by CIK) + `daily_prices` (one row per `DailyBar`), with
  `provider`, `adjustment_basis`, `provider_adjust_mode`, `currency`, and `retrieved_at`
  carried from the series and the four prices and volume from the bar.
* `DiscoveryResult` + `FilingRecord` → `sec_filings`, with the feed's `accession_number`,
  `form_type`, `filing_date`, and `acceptance_datetime` beside the document's own
  `document_type`, `schema_version`, `rule_10b5_1`, `remarks`, `footnotes`,
  `holding_rows_skipped`, and `date_of_original_submission`; plus the preserved
  `source_xml` and its SHA-256.
* `ReportingOwner` → `insider_reporting_owners`, keyed on `(filing_id, reporting_owner_cik)`.
* `TransactionRow` → `insider_transactions`, keyed on
  `(filing_id, source_table, row_position)` — provenance, never date-and-amount.

**Deliberately not stored.** The XML's `issuer_name` and `issuer_trading_symbol` (they are
validated against `companies`, and storing them twice would create a second source of
truth), and the feed's `report_date` (the document's `periodOfReport` is authoritative).
`amends_filing_id` is left NULL always: the document never says which accession an
amendment replaces, and guessing is worse than an acknowledged gap.

**What a second run may and may not do.** Every write is
`INSERT ... ON CONFLICT (identity) DO NOTHING`, so the unique constraint -- not a check in
this file -- is what prevents duplicates. When no row is inserted the existing row is read
back and its business columns compared:

* identical → counted *unchanged*, and not written to at all. Its `id` and its original
  `retrieved_at` survive because nothing touched them.
* different → `IngestionConflictError` naming the table, the identity, and the columns that
  differ. Not overwritten, and not ignored either.

`retrieved_at` is provenance, not a business value: it differs on every run by definition,
so it is excluded from the comparison and never updated.

A provider correction, a revised split history, or a reparse after a parser fix all land in
that third case, and all need a deliberate refresh policy that this milestone does not have.
Failing loudly is the honest placeholder for it.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.form4 import Form4Document, TransactionRow
from app.models import (
    Company,
    DailyPrice,
    IngestionRun,
    InsiderReportingOwner,
    InsiderTransaction,
    SecFiling,
)
from app.sec_edgar import DiscoveryResult, FilingRecord
from app.twelvedata import DailyPriceSeries

# The tables an ingestion writes to, in dependency order. Used for the per-table counts in
# the summary, so the reporting cannot drift from what was actually written.
# What a Form 4 run is called in `ingestion_runs.scope`. A company-context run writes the
# same table under a different scope, so anything asking "how much insider history has been
# ingested" has to say which kind of run it means.
#
# The scope names the *kind* of run, not the company, so it carries no ticker. It was
# `nvda_form4` until a second company could be ingested, at which point it would have labelled
# Apple's Form 4 runs with NVIDIA's name.
FORM4_SCOPE = "form4"

TRACKED_TABLES = (
    "companies",
    "daily_prices",
    "sec_filings",
    "insider_reporting_owners",
    "insider_transactions",
    "ingestion_runs",
)

# Written but never compared, because they describe when we fetched rather than what we
# fetched. A re-run must not treat "the clock moved" as a change.
_PROVENANCE_COLUMNS = frozenset({"retrieved_at", "started_at", "completed_at"})

_CONFLICT_COLUMNS_SHOWN = 3
_CONFLICT_VALUE_WIDTH = 48


class IngestionError(Exception):
    """Base class for every failure this module reports."""


class IngestionIdentityError(IngestionError):
    """The two sources do not describe the same company, so nothing may be written."""


class IngestionConflictError(IngestionError):
    """Stored data differs from incoming data under the same identity.

    Raised rather than resolved. Overwriting would destroy the record of what was stored
    first; ignoring would leave the database quietly disagreeing with the provider. Both
    are worse than stopping and saying which record differs.
    """


@dataclass
class TableCounts:
    """What happened to one table during a run."""

    inserted: int = 0
    unchanged: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "unchanged": self.unchanged,
            "total": self.inserted + self.unchanged,
        }


@dataclass(frozen=True)
class IngestionSummary:
    """Everything worth saying about one ingestion run.

    Deliberately explicit about scope. Three filings are a sample, and a summary that did
    not say so would invite treating a bounded fetch as complete insider coverage.
    """

    company_cik: str
    company_name: str
    company_ticker: str
    company_id: int | None

    requested_bars: int
    returned_bars: int
    price_first_date: date | None
    price_last_date: date | None
    adjustment_basis: str
    provider_adjust_mode: str

    requested_filings: int
    returned_filings: int
    discovery_scope: str
    recent_filings_scanned: int
    older_filing_files_not_searched: int
    filing_accessions: tuple[str, ...]
    filing_first_date: date | None
    filing_last_date: date | None
    holding_rows_skipped: int
    amendment_count: int

    counts: Mapping[str, TableCounts]
    warnings: tuple[str, ...]
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe view, for the command's output and the run record.

        No Decimal appears here, so nothing needs the string treatment prices get; the
        dates are the only values that are not already JSON-native.
        """
        return {
            "company": {
                "cik": self.company_cik,
                "name": self.company_name,
                "ticker": self.company_ticker,
                "id": self.company_id,
            },
            "prices": {
                "requested_bars": self.requested_bars,
                "returned_bars": self.returned_bars,
                "first_date": _iso(self.price_first_date),
                "last_date": _iso(self.price_last_date),
                "adjustment_basis": self.adjustment_basis,
                "provider_adjust_mode": self.provider_adjust_mode,
            },
            "filings": {
                "requested_filings": self.requested_filings,
                "returned_filings": self.returned_filings,
                "discovery_scope": self.discovery_scope,
                "recent_filings_scanned": self.recent_filings_scanned,
                "older_filing_files_not_searched": self.older_filing_files_not_searched,
                "accessions": list(self.filing_accessions),
                "first_filing_date": _iso(self.filing_first_date),
                "last_filing_date": _iso(self.filing_last_date),
                "holding_rows_skipped": self.holding_rows_skipped,
                "amendment_count": self.amendment_count,
            },
            "counts": {
                table: counts.as_dict() for table, counts in self.counts.items()
            },
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
        }


def ingest(
    session: Session,
    *,
    series: DailyPriceSeries,
    discovery: DiscoveryResult,
    records: Sequence[FilingRecord],
    parameters: Mapping[str, Any],
    started_at: datetime,
    dry_run: bool = False,
) -> IngestionSummary:
    """Write one bounded dataset into the database.

    **Does not commit.** The caller owns the transaction, which is what lets a test run
    this inside a connection it then rolls back, and what lets the command decide that a
    failure anywhere means nothing survives.

    With `dry_run`, nothing is written and no insertion is attempted, but every row is
    still classified and every conflict still raised -- so a dry run answers "would this
    work, and what would change" rather than only "did the fetch succeed".
    """
    _check_sources_agree(series, discovery)
    _check_payload_has_no_duplicate_identities(series, records)

    counts = {table: TableCounts() for table in TRACKED_TABLES}

    # Serialises runs for this company. Two concurrent ingests cannot interleave into a
    # half-written filing; the second waits, then finds the first's rows already there and
    # reports them unchanged. Released by commit or rollback, so a crashed run cannot
    # leave the lock behind.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"alphadesk:ingest:{discovery.issuer_cik}"},
    )

    company_id = _persist_company(session, series, discovery, counts, dry_run)

    for bar in series.bars:
        _persist_price(session, company_id, series, bar, counts, dry_run)

    for record in records:
        filing_id = _persist_filing(session, company_id, record, counts, dry_run)
        for owner in record.document.owners:
            _persist_owner(session, filing_id, owner, counts, dry_run)
        for row in record.document.transactions:
            _persist_transaction(
                session, filing_id, record.document, row, counts, dry_run
            )

    # Counted before the summary is built, so the summary reports the run record it is
    # itself about. Incrementing afterwards would work only because the summary holds a
    # reference to this same dict, which is too subtle a thing to rely on.
    if not dry_run:
        counts["ingestion_runs"].inserted += 1

    summary = _build_summary(
        series=series,
        discovery=discovery,
        records=records,
        counts=counts,
        company_id=company_id,
        dry_run=dry_run,
    )

    if not dry_run:
        _persist_run(session, company_id, parameters, summary, started_at)

    return summary


# --- the write primitive ----------------------------------------------------------------


def persist_row(
    session: Session,
    model: type,
    identity: Mapping[str, Any],
    values: Mapping[str, Any],
    counts: TableCounts,
    dry_run: bool,
    *,
    label: str,
    provenance: Sequence[str] = (),
) -> int | None:
    """Insert a row under `identity`, or confirm the existing one matches.

    Returns the row's id, or None on a dry run where the row does not exist yet.

    The insert uses `ON CONFLICT DO NOTHING`, so the unique constraint decides whether a
    duplicate is possible -- not this function. The read-back exists only to tell
    *unchanged* apart from *conflicting*, which a constraint cannot express.

    `provenance` names extra columns that are written but never compared, for the same
    reason `retrieved_at` is not: they record where a row was first seen rather than what it
    says. A financial fact's `snapshot_id` is the case that needs this -- a fresh Company
    Facts snapshot must not make every unchanged observation look like a conflict.
    """
    ignored = _PROVENANCE_COLUMNS | set(provenance)
    existing = session.scalar(select(model).filter_by(**identity))

    if existing is not None:
        _require_unchanged(identity, existing, values, label, ignored)
        counts.unchanged += 1
        return existing.id

    if dry_run:
        # Nothing to compare against and nothing to write; this is a would-be insert.
        counts.inserted += 1
        return None

    statement = (
        pg_insert(model)
        .values(**identity, **values)
        .on_conflict_do_nothing(index_elements=list(identity))
        .returning(model.id)
    )
    inserted_id = session.scalar(statement)

    if inserted_id is None:
        # The row appeared between the read and the insert. The constraint caught it,
        # which is exactly why the insert does not rely on the read being right.
        raced = session.scalar(select(model).filter_by(**identity))
        if raced is None:
            raise IngestionError(
                f"{label}: the insert conflicted but no matching row could be read back"
            )
        _require_unchanged(identity, raced, values, label, ignored)
        counts.unchanged += 1
        return raced.id

    counts.inserted += 1
    return inserted_id


def _require_unchanged(
    identity: Mapping[str, Any],
    existing: Any,
    values: Mapping[str, Any],
    label: str,
    ignored: frozenset[str] | set[str] = _PROVENANCE_COLUMNS,
) -> None:
    """Raise unless every business column matches the stored row."""
    compared = [name for name in values if name not in ignored and name not in identity]

    differences = [
        (name, getattr(existing, name), values[name])
        for name in sorted(compared)
        if getattr(existing, name) != values[name]
    ]
    if not differences:
        return

    raise IngestionConflictError(
        f"{label} already exists with different values: "
        f"{_describe(identity, differences)}. Nothing was overwritten; the stored row is "
        "unchanged and the incoming one was not written. A provider correction or a "
        "revised adjustment history needs an explicit refresh policy, which this "
        "milestone does not yet have."
    )


def _describe(identity: Mapping[str, Any], differences: Sequence[tuple]) -> str:
    """Name the record and what differs, without reproducing the whole row."""
    where = ", ".join(f"{name}={value!r}" for name, value in identity.items())
    shown = differences[:_CONFLICT_COLUMNS_SHOWN]
    parts = [
        f"{name} stored {_short(old)} vs incoming {_short(new)}"
        for name, old, new in shown
    ]
    if len(differences) > len(shown):
        parts.append(f"and {len(differences) - len(shown)} more column(s)")
    return f"[{where}] {'; '.join(parts)}"


def _short(value: Any) -> str:
    """Truncated for a message. A source XML must not end up in an error string."""
    text_value = repr(value)
    if len(text_value) <= _CONFLICT_VALUE_WIDTH:
        return text_value
    return f"{text_value[:_CONFLICT_VALUE_WIDTH]}... ({len(text_value)} chars)"


# --- per-table writes -------------------------------------------------------------------


def _persist_company(
    session: Session,
    series: DailyPriceSeries,
    discovery: DiscoveryResult,
    counts: dict[str, TableCounts],
    dry_run: bool,
) -> int:
    """Resolve the company by issuer CIK, and require its identity to be consistent.

    A CIK identifies a company; a ticker does not. So the CIK is what is looked up, and the
    ticker, exchange, name, and currency are checked against what is already stored rather
    than used to decide which row this is.
    """
    values = {
        "name": discovery.issuer_name,
        "ticker": series.symbol,
        "exchange": series.exchange,
        "currency": series.currency,
        "sec_issuer_cik": discovery.issuer_cik,
    }
    identity = {"sec_issuer_cik": discovery.issuer_cik}

    existing = session.scalar(select(Company).filter_by(**identity))
    if existing is not None:
        _require_unchanged(identity, existing, values, "company")
        counts["companies"].unchanged += 1
        return existing.id

    if dry_run:
        counts["companies"].inserted += 1
        return 0

    company = Company(**values)
    session.add(company)
    try:
        session.flush()
    except IntegrityError as exc:
        # `companies` is also unique on ticker, so this is most likely another CIK already
        # holding the same ticker. Reported rather than retried: two CIKs with one ticker
        # is a real-world ambiguity this milestone has no business resolving quietly.
        raise IngestionConflictError(
            f"cannot store {series.symbol} under CIK {discovery.issuer_cik}: another "
            "company row already holds that ticker. Nothing was written."
        ) from exc

    counts["companies"].inserted += 1
    return company.id


def _persist_price(
    session: Session,
    company_id: int,
    series: DailyPriceSeries,
    bar: Any,
    counts: dict[str, TableCounts],
    dry_run: bool,
) -> None:
    identity = {
        "company_id": company_id,
        "trading_date": bar.trading_date,
        "provider": series.provider,
        "adjustment_basis": series.adjustment_basis,
        "provider_adjust_mode": series.provider_adjust_mode,
    }
    values = {
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "currency": series.currency,
        # NULL: the provider states nothing about volume adjustment, and "not stated" is
        # not the same as "not adjusted".
        "volume_adjustment": None,
        "retrieved_at": series.retrieved_at,
    }
    persist_row(
        session,
        DailyPrice,
        identity,
        values,
        counts["daily_prices"],
        dry_run,
        label=f"daily bar for {series.symbol} on {bar.trading_date}",
    )


def _persist_filing(
    session: Session,
    company_id: int,
    record: FilingRecord,
    counts: dict[str, TableCounts],
    dry_run: bool,
) -> int:
    document = record.document
    identity = {"accession_number": record.filing.accession_number}
    values = {
        "company_id": company_id,
        "form_type": record.filing.form_type,
        "filing_date": record.filing.filing_date,
        "acceptance_datetime": record.filing.acceptance_datetime,
        "source_document_url": record.source_xml_url,
        "retrieved_at": record.retrieved_at,
        "is_amendment": document.is_amendment,
        # Never inferred. The document does not say which accession it amends.
        "amends_filing_id": None,
        "document_type": document.document_type,
        "schema_version": document.schema_version,
        "date_of_original_submission": document.date_of_original_submission,
        "rule_10b5_1": document.rule_10b5_1,
        "holding_rows_skipped": document.holding_rows_skipped,
        "remarks": document.remarks,
        "footnotes": dict(document.footnotes),
        "source_xml": _decode_xml(record),
        "source_xml_sha256": hashlib.sha256(record.source_xml).hexdigest(),
    }
    filing_id = persist_row(
        session,
        SecFiling,
        identity,
        values,
        counts["sec_filings"],
        dry_run,
        label=f"filing {record.filing.accession_number}",
    )
    # On a dry run the filing did not exist, so there is no id to hang owners and
    # transactions off. Zero is not a real id and is never written -- it only stands in
    # while classifying, which is why the dry-run path never reaches an INSERT.
    return filing_id if filing_id is not None else 0


def _persist_owner(
    session: Session,
    filing_id: int,
    owner: Any,
    counts: dict[str, TableCounts],
    dry_run: bool,
) -> None:
    identity = {"filing_id": filing_id, "reporting_owner_cik": owner.owner_cik}
    values = {
        "owner_name": owner.owner_name,
        "is_director": owner.is_director,
        "is_officer": owner.is_officer,
        "officer_title": owner.officer_title,
        "is_ten_percent_owner": owner.is_ten_percent_owner,
        "is_other": owner.is_other,
        "other_text": owner.other_text,
    }
    persist_row(
        session,
        InsiderReportingOwner,
        identity,
        values,
        counts["insider_reporting_owners"],
        dry_run,
        label=f"reporting owner {owner.owner_cik} on filing {filing_id}",
    )


def _persist_transaction(
    session: Session,
    filing_id: int,
    document: Form4Document,
    row: TransactionRow,
    counts: dict[str, TableCounts],
    dry_run: bool,
) -> None:
    identity = {
        "filing_id": filing_id,
        "source_table": row.source_table,
        "row_position": row.row_position,
    }
    values = {
        "is_derivative": row.source_table == "derivativeTable",
        "transaction_date": row.transaction_date,
        "security_title": row.security_title,
        "transaction_code": row.transaction_code,
        "acquired_disposed": row.acquired_disposed,
        "shares": row.shares,
        "price_per_share": row.price_per_share,
        "ownership_direct_indirect": row.ownership_direct_indirect,
        "nature_of_ownership": row.nature_of_ownership,
        "shares_owned_following": row.shares_owned_following,
        "footnote_refs": [
            {"field": ref.field, "footnote_id": ref.footnote_id}
            for ref in row.footnote_refs
        ],
        "footnotes": _footnote_text(document, row),
        "underlying_security_title": row.underlying_security_title,
        "underlying_shares": row.underlying_shares,
        "exercise_price": row.exercise_price,
        "expiration_date": row.expiration_date,
    }
    persist_row(
        session,
        InsiderTransaction,
        identity,
        values,
        counts["insider_transactions"],
        dry_run,
        label=(
            f"transaction {row.source_table}[{row.row_position}] on filing {filing_id}"
        ),
    )


def _persist_run(
    session: Session,
    company_id: int,
    parameters: Mapping[str, Any],
    summary: IngestionSummary,
    started_at: datetime,
) -> None:
    """The receipt. Written last, inside the same transaction, so a failure leaves none."""
    session.add(
        IngestionRun(
            company_id=company_id,
            scope=FORM4_SCOPE,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            parameters=dict(parameters),
            summary=summary.as_dict(),
        )
    )
    session.flush()


# --- helpers ----------------------------------------------------------------------------


def _check_sources_agree(series: DailyPriceSeries, discovery: DiscoveryResult) -> None:
    """Both sources must be describing the same instrument before anything is written.

    The price client's symbol and the SEC's ticker list are independent statements about
    which company this is. If they disagree, one of them is about something else, and
    writing either would attach prices to the wrong issuer.
    """
    if not discovery.tickers:
        raise IngestionIdentityError(
            f"SEC metadata for CIK {discovery.issuer_cik} lists no tickers, so it cannot "
            f"be confirmed as {series.symbol}. Nothing was written."
        )
    if series.symbol not in discovery.tickers:
        raise IngestionIdentityError(
            f"the price source says {series.symbol} but SEC metadata for CIK "
            f"{discovery.issuer_cik} lists {sorted(discovery.tickers)}. The two sources "
            "disagree about which company this is, so nothing was written."
        )


def _check_payload_has_no_duplicate_identities(
    series: DailyPriceSeries, records: Sequence[FilingRecord]
) -> None:
    """Refuse a payload that names the same record twice.

    This is not paranoia about the clients, which already deduplicate. It is about what
    `ON CONFLICT DO NOTHING` would do with a duplicate: the second occurrence would be
    reported as *unchanged*, which is a lie -- it was never stored, it was sent twice. A
    count that says "unchanged" when the truth is "the provider sent this twice" is worse
    than an error, so the payload is checked before any of it is written.
    """
    dates = [bar.trading_date for bar in series.bars]
    if len(dates) != len(set(dates)):
        raise IngestionError(
            f"the price payload contains more than one bar for the same session "
            f"({_duplicates(dates)}), so the run cannot say which one it stored"
        )

    accessions = [record.filing.accession_number for record in records]
    if len(accessions) != len(set(accessions)):
        raise IngestionError(
            f"the filing payload contains the same accession twice "
            f"({_duplicates(accessions)})"
        )

    for record in records:
        positions = [
            (row.source_table, row.row_position)
            for row in record.document.transactions
        ]
        if len(positions) != len(set(positions)):
            raise IngestionError(
                f"filing {record.filing.accession_number} reports the same source row "
                f"twice ({_duplicates(positions)})"
            )

        owner_ciks = [owner.owner_cik for owner in record.document.owners]
        if len(owner_ciks) != len(set(owner_ciks)):
            raise IngestionError(
                f"filing {record.filing.accession_number} lists the same reporting "
                f"owner twice ({_duplicates(owner_ciks)})"
            )


def _duplicates(values: Sequence[Any]) -> str:
    seen: set[Any] = set()
    repeated: list[Any] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return ", ".join(repr(value) for value in repeated[:3])


def _decode_xml(record: FilingRecord) -> str:
    """The source document as text, for storage and later inspection.

    Decoded strictly: a document that is not UTF-8 is a surprise worth hearing about,
    not something to paper over with replacement characters in the one copy being kept.
    """
    try:
        return record.source_xml.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IngestionError(
            f"the source XML for {record.filing.accession_number} is not valid UTF-8, so "
            "it cannot be stored as text"
        ) from exc


def _footnote_text(document: Form4Document, row: TransactionRow) -> str | None:
    """The resolved text of the footnotes this row references, in reference order."""
    seen: set[str] = set()
    parts: list[str] = []
    for ref in row.footnote_refs:
        if ref.footnote_id in seen:
            continue
        seen.add(ref.footnote_id)
        note = document.footnotes.get(ref.footnote_id)
        if note:
            parts.append(f"[{ref.footnote_id}] {note}")
    return "\n".join(parts) or None


def _build_summary(
    *,
    series: DailyPriceSeries,
    discovery: DiscoveryResult,
    records: Sequence[FilingRecord],
    counts: dict[str, TableCounts],
    company_id: int,
    dry_run: bool,
) -> IngestionSummary:
    price_dates = [bar.trading_date for bar in series.bars]
    filing_dates = [record.filing.filing_date for record in records]
    amendments = [record for record in records if record.document.is_amendment]

    warnings: list[str] = [
        "SEC discovery covers the submissions 'recent' list only, so this is a bounded "
        "sample of insider activity and not a complete history.",
    ]
    if amendments:
        warnings.append(
            f"{len(amendments)} of these filings are amendments. Raw transactions must "
            "not be summed across a Form 4 and its 4/A: they are stored as separate "
            "filings with no link, so adding them would double-count what they share. "
            "Resolving that belongs to the step that computes insider totals."
        )
    if not records:
        warnings.append(
            "No Form 4 filings were returned. Prices were still ingested; the absence of "
            "filings is not evidence of no insider activity."
        )

    return IngestionSummary(
        company_cik=discovery.issuer_cik,
        company_name=discovery.issuer_name,
        company_ticker=series.symbol,
        company_id=None if dry_run else company_id,
        requested_bars=series.requested_bars,
        returned_bars=series.returned_bars,
        price_first_date=min(price_dates) if price_dates else None,
        price_last_date=max(price_dates) if price_dates else None,
        adjustment_basis=series.adjustment_basis,
        provider_adjust_mode=series.provider_adjust_mode,
        requested_filings=len(discovery.filings),
        returned_filings=len(records),
        discovery_scope=(
            f"SEC submissions 'recent' list only: {discovery.recent_filings_scanned} "
            f"filings scanned; {discovery.older_filing_files_not_searched} older filings "
            "held in separate submission files were NOT searched"
        ),
        recent_filings_scanned=discovery.recent_filings_scanned,
        older_filing_files_not_searched=discovery.older_filing_files_not_searched,
        filing_accessions=tuple(
            record.filing.accession_number for record in records
        ),
        filing_first_date=min(filing_dates) if filing_dates else None,
        filing_last_date=max(filing_dates) if filing_dates else None,
        holding_rows_skipped=sum(
            record.document.holding_rows_skipped for record in records
        ),
        amendment_count=len(amendments),
        counts=counts,
        warnings=tuple(warnings),
        dry_run=dry_run,
    )


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()