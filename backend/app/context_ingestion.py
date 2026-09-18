"""Persisting company disclosures and financial facts.

The same conventions as `app.ingestion`, reused rather than restated: every write goes
through `persist_row`, so identity is enforced by a unique constraint and a changed payload
under an existing identity stops the run instead of overwriting anything.

**A disclosure filing is not a Form 4 with missing fields.** A 10-K has no ownership XML and
no holding rows, so the columns that mean something only for an ownership document are set to
NULL rather than to an empty string or a zero. `document_type` and `footnotes` do have a
truthful value for any filing -- the form type, and an empty footnote map -- so they keep the
form they always had.

**Two things are deliberately not conflicts.** A Company Facts response that changed is a new
snapshot, because that endpoint grows as filings are added and treating growth as a conflict
would make the command unusable. A fact that reappears in a new snapshot is unchanged, not
conflicting, which is why `snapshot_id` is written but never compared.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.company_facts import FactObservation
from app.htmltext import ExtractedDocument
from app.ingestion import TableCounts, persist_row
from app.models import (
    CompanyFactSnapshot,
    FilingDocument,
    FinancialFact,
    IngestionRun,
    SecFiling,
)

# What a company-context run is called in `ingestion_runs.scope`. It is deliberately not
# `form4`: a Form 4 analysis reading "the latest run" must never pick up a disclosure
# run's filing counts and present them as insider-history coverage.
CONTEXT_SCOPE = "company_context"

CONTEXT_TABLES = (
    "sec_filings",
    "filing_documents",
    "company_fact_snapshots",
    "financial_facts",
    "ingestion_runs",
)

# Written but never compared: which fetch a fact was first seen in, not what it says.
_SNAPSHOT_IS_PROVENANCE = ("snapshot_id",)

# What an immutable document is, is its content. The text is *derived* from that content, so
# it is written but never compared: an improved extractor must not make the SEC's document
# look as though it changed, and a conflict reading "the document changed" when only the
# extractor did would send someone to the wrong place entirely.
#
# The consequence is deliberate and is why `extraction_version` is stored: re-running does
# not rewrite derived text, and finding the rows a newer extractor should revisit is a query
# on that column. Re-extraction is a refresh policy, and this milestone does not have one.
_EXTRACTION_IS_PROVENANCE = (
    "extracted_text",
    "extraction_version",
    "extraction_status",
    "extraction_limitations",
    "sections",
)


@dataclass(frozen=True)
class SelectedFiling:
    """A disclosure filing chosen for the context set, before its documents are read."""

    accession_number: str
    form_type: str
    filing_date: date
    report_date: date | None
    acceptance_datetime: datetime | None
    primary_document: str
    source_document_url: str


@dataclass(frozen=True)
class FetchedDocument:
    """One document as fetched, with whatever extraction was possible."""

    filing_accession: str
    document_name: str
    document_type: str
    role: str
    sequence: int | None
    source_url: str
    content_type: str | None
    content: str
    retrieved_at: datetime
    extraction: ExtractedDocument | None
    extraction_status: str


@dataclass(frozen=True)
class FetchedSnapshot:
    """A Company Facts response, before any of it is interpreted."""

    payload: bytes
    source_url: str
    retrieved_at: datetime
    byte_size: int

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


def persist_context(
    session: Session,
    *,
    company_id: int,
    filings: Sequence[SelectedFiling],
    documents: Sequence[FetchedDocument],
    snapshot: FetchedSnapshot | None,
    observations: Sequence[FactObservation],
    dry_run: bool = False,
) -> dict[str, TableCounts]:
    """Write the whole dataset. Does not commit; the caller owns the transaction.

    Everything is passed in already fetched and already validated, so a failure here is a
    persistence failure and nothing else -- and a failure anywhere leaves no partial set,
    because the caller's transaction rolls back whole.
    """
    counts = {table: TableCounts() for table in CONTEXT_TABLES}
    filing_ids: dict[str, int] = {}

    for filing in filings:
        filing_ids[filing.accession_number] = _persist_filing(
            session, company_id, filing, counts, dry_run
        )

    for document in documents:
        filing_id = filing_ids.get(document.filing_accession)
        if filing_id is None:
            # Only reachable if a caller pairs a document with a filing it did not select.
            raise ValueError(
                f"document {document.document_name!r} belongs to filing "
                f"{document.filing_accession!r}, which is not in this context set"
            )
        _persist_document(session, filing_id, document, counts, dry_run)

    snapshot_id = (
        None
        if snapshot is None
        else _persist_snapshot(session, company_id, snapshot, counts, dry_run)
    )

    if snapshot_id is not None or dry_run:
        for observation in observations:
            _persist_fact(
                session, company_id, snapshot_id or 0, observation, counts, dry_run
            )

    return counts


def record_context_run(
    session: Session,
    *,
    company_id: int,
    parameters: Mapping[str, Any],
    summary: Mapping[str, Any],
    started_at: datetime,
    completed_at: datetime,
) -> None:
    """The receipt, written under its own scope. Written last, so a failure leaves none."""
    session.add(
        IngestionRun(
            company_id=company_id,
            scope=CONTEXT_SCOPE,
            started_at=started_at,
            completed_at=completed_at,
            parameters=dict(parameters),
            summary=dict(summary),
        )
    )
    session.flush()


# --- per-table writes ---------------------------------------------------------------------


def _persist_filing(
    session: Session,
    company_id: int,
    filing: SelectedFiling,
    counts: dict[str, TableCounts],
    dry_run: bool,
) -> int:
    identity = {"accession_number": filing.accession_number}
    values = {
        "company_id": company_id,
        "form_type": filing.form_type,
        "filing_date": filing.filing_date,
        "report_date": filing.report_date,
        "acceptance_datetime": filing.acceptance_datetime,
        "source_document_url": filing.source_document_url,
        "retrieved_at": filing.acceptance_datetime,
        "is_amendment": filing.form_type.upper().endswith("/A"),
        # Never inferred, for a disclosure filing as much as for a Form 4.
        "amends_filing_id": None,
        # The form type is the truthful value here: unlike the ownership columns below, a
        # 10-K genuinely does have a document type.
        "document_type": filing.form_type,
        "schema_version": None,
        "date_of_original_submission": None,
        # A 10-K has no Rule 10b5-1 checkbox, so NULL -- "not stated" -- is the honest
        # value, not False.
        "rule_10b5_1": None,
        # The Form 4-only columns, set to NULL rather than to an empty string or a zero.
        # A 10-K is not an empty Form 4: it has no ownership XML, no schema version, and no
        # holding rows for a parser to have skipped.
        "holding_rows_skipped": None,
        "remarks": None,
        "source_xml": None,
        "source_xml_sha256": None,
        # No Form 4 footnotes exist, so an empty map is accurate rather than invented.
        "footnotes": {},
    }
    filing_id = persist_row(
        session,
        SecFiling,
        identity,
        values,
        counts["sec_filings"],
        dry_run,
        label=f"filing {filing.accession_number}",
    )
    return filing_id if filing_id is not None else 0


def _persist_document(
    session: Session,
    filing_id: int,
    document: FetchedDocument,
    counts: dict[str, TableCounts],
    dry_run: bool,
) -> None:
    identity = {"filing_id": filing_id, "document_name": document.document_name}
    values = {
        "document_type": document.document_type,
        "role": document.role,
        "sequence": document.sequence,
        "source_url": document.source_url,
        "content_type": document.content_type,
        "content": document.content,
        # The hash of the text actually stored, so re-reading it reproduces the hash.
        "content_sha256": hashlib.sha256(document.content.encode("utf-8")).hexdigest(),
        "retrieved_at": document.retrieved_at,
        "extracted_text": (
            None if document.extraction is None else document.extraction.text
        ),
        "extraction_version": (
            None if document.extraction is None else document.extraction.extraction_version
        ),
        "extraction_status": document.extraction_status,
        "extraction_limitations": (
            None
            if document.extraction is None
            else "\n".join(document.extraction.limitations) or None
        ),
        "sections": (
            {}
            if document.extraction is None
            else {
                name: {"start": span.start, "heading": span.heading}
                for name, span in document.extraction.sections.items()
            }
        ),
    }
    persist_row(
        session,
        FilingDocument,
        identity,
        values,
        counts["filing_documents"],
        dry_run,
        label=f"document {document.document_name} of filing {filing_id}",
        provenance=_EXTRACTION_IS_PROVENANCE,
    )


def _persist_snapshot(
    session: Session,
    company_id: int,
    snapshot: FetchedSnapshot,
    counts: dict[str, TableCounts],
    dry_run: bool,
) -> int:
    """Store a Company Facts response, deduplicated by its content hash.

    The hash is the identity, so an unchanged endpoint adds no row and a changed one adds a
    row rather than conflicting -- that endpoint grows as filings are added, and growth is
    not a disagreement.
    """
    identity = {"content_sha256": snapshot.content_sha256}
    values = {
        "company_id": company_id,
        "source_url": snapshot.source_url,
        "retrieved_at": snapshot.retrieved_at,
        "byte_size": snapshot.byte_size,
    }
    snapshot_id = persist_row(
        session,
        CompanyFactSnapshot,
        identity,
        values,
        counts["company_fact_snapshots"],
        dry_run,
        label=f"Company Facts snapshot {snapshot.content_sha256[:16]}",
    )
    return snapshot_id if snapshot_id is not None else 0


def _persist_fact(
    session: Session,
    company_id: int,
    snapshot_id: int,
    observation: FactObservation,
    counts: dict[str, TableCounts],
    dry_run: bool,
) -> None:
    """Store one observation under its period-and-filing identity.

    `period_start` is part of the identity and may be NULL, which is why the table's unique
    constraint is declared NULLS NOT DISTINCT: without that, PostgreSQL would treat every
    instant fact as unique and re-insert the whole balance sheet on every run.
    """
    identity = {
        "company_id": company_id,
        "taxonomy": observation.taxonomy,
        "concept": observation.concept,
        "unit": observation.unit,
        "period_start": observation.period_start,
        "period_end": observation.period_end,
        "accession_number": observation.accession_number,
    }
    values = {
        "snapshot_id": snapshot_id,
        "value": observation.value,
        "form": observation.form,
        "filed_date": observation.filed_date,
        "fiscal_year": observation.fiscal_year,
        "fiscal_period": observation.fiscal_period,
        "frame": observation.frame,
    }
    periods = (
        f"{observation.period_start}..{observation.period_end}"
        if observation.period_start
        else f"instant {observation.period_end}"
    )
    persist_row(
        session,
        FinancialFact,
        identity,
        values,
        counts["financial_facts"],
        dry_run,
        label=f"{observation.concept} {observation.unit} {periods}",
        provenance=_SNAPSHOT_IS_PROVENANCE,
    )
