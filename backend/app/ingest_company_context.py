"""Fetch and store a bounded company context: disclosures, and selected financial facts.

    docker compose exec backend python -m app.ingest_company_context --symbol NVDA \\
        --as-of 2026-09-17

What it stores: the latest original 10-K and 10-Q accepted before the as-of cutoff, up to
three of the most recent 8-Ks from the preceding 90 days, the earnings exhibits attached to
those 8-Ks, the readable text of each, and a small set of financial concepts from SEC's
Company Facts.

What it does not do: page through history, fetch PDFs or images, follow links, resolve
amendments, derive ratios, or produce a recommendation.

**Two sets of limits, both explicit.** Each document must fit the client's existing 8 MiB
cap, and the whole run is bounded by a request budget. Reaching either marks the run's
coverage incomplete and says so; it does not silently return a smaller set as though it were
the whole one.

**Everything is fetched before the write transaction opens**, so a network failure cannot
leave a half-written filing behind and the transaction stays short.
"""

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.analysis import information_cutoff
from app.company_facts import (
    SNAPSHOT_LABEL,
    ParsedCompanyFacts,
    content_sha256,
    fetch_payload,
    parse_company_facts,
)
from app.context_ingestion import (
    CONTEXT_SCOPE,
    FetchedDocument,
    FetchedSnapshot,
    SelectedFiling,
    persist_context,
    record_context_run,
)
from app.db import SessionLocal
from app.htmltext import EXTRACTION_VERSION, ExtractedDocument, extract
from app.ingestion import IngestionError
from app.models import Company
from app.sec_edgar import (
    FilingIndexEntry,
    SecEdgarClient,
    SecEdgarError,
    archive_file_url,
    archive_index_html_url,
    parse_filing_index,
)

DEFAULT_SYMBOL = "NVDA"

EXIT_OK = 0
EXIT_FAILED = 1

# The forms selected, and the amendments looked for but never fetched.
ORIGINAL_FORMS = ("10-K", "10-Q", "8-K")
AMENDMENT_FORMS = ("10-K/A", "10-Q/A", "8-K/A")

EIGHT_K_WINDOW_DAYS = 90
MAX_EIGHT_K = 3
MAX_EXHIBITS_PER_FILING = 2

# The whole run's request ceiling. One submissions read, up to three index pages, five
# primary documents, up to six exhibits and one Company Facts fetch is 16; the margin is
# there so the budget is a bound rather than a coincidence.
MAX_REQUESTS = 24

# Exhibit types are the SEC's own designation for material filed alongside a report, and
# EX-99 is where earnings releases live. Matching on the type rather than the filename is
# what keeps this from being a guess about what a document is called.
_EXHIBIT_TYPE_PREFIX = "EX-99"
_FETCHABLE_EXTENSIONS = (".htm", ".html", ".txt")
_EXHIBIT_NUMBER = re.compile(r"^EX-99\.?(\d*)$", re.IGNORECASE)


@dataclass(frozen=True)
class StoredCompany:
    """The company's identity as plain values."""

    id: int
    cik: str
    name: str
    ticker: str


@dataclass(frozen=True)
class Selection:
    """Which filings were chosen, and what could not be."""

    filings: tuple[SelectedFiling, ...]
    amendment_accessions: tuple[str, ...]
    limitations: tuple[str, ...]
    recent_filings_scanned: int
    older_filing_files_not_searched: int


@dataclass
class RequestBudget:
    """A hard ceiling on outbound requests, shared across the whole run."""

    limit: int
    used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def spend(self) -> None:
        self.used += 1


@dataclass(frozen=True)
class DocumentOutcome:
    """What happened to one candidate document."""

    document: FetchedDocument | None
    skipped_reason: str | None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest_company_context",
        description=(
            "Fetch and store recent company disclosures and selected financial facts for "
            "one company. Writes only when not given --dry-run."
        ),
    )
    parser.add_argument(
        "--symbol", default=DEFAULT_SYMBOL, help=f"ticker (default: {DEFAULT_SYMBOL})"
    )
    parser.add_argument(
        "--as-of",
        type=_iso_date,
        default=None,
        help="analysis cut-off date, YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and validate, report what would change, and write nothing",
    )
    args = parser.parse_args(argv)

    symbol = args.symbol.strip().upper()
    if not symbol:
        print("error: --symbol must not be empty", file=sys.stderr)
        return EXIT_FAILED

    as_of = args.as_of or date.today()

    try:
        summary = run(symbol, as_of, dry_run=args.dry_run)
    except (SecEdgarError, IngestionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except SQLAlchemyError as exc:
        print(
            f"error: could not read or write the stored data: {type(exc).__name__}",
            file=sys.stderr,
        )
        return EXIT_FAILED

    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


def run(symbol: str, as_of: date, *, dry_run: bool) -> dict[str, Any]:
    """Fetch, validate, then write -- in that order, and never any other."""
    started_at = datetime.now(timezone.utc)
    cutoff = information_cutoff(as_of)

    with SessionLocal() as session:
        company = _company_for(session, symbol)
        if company is None:
            stored = tuple(session.scalars(select(Company.ticker).order_by(Company.ticker)))
            return _unavailable(
                symbol=symbol,
                as_of=as_of,
                cutoff=cutoff,
                note=(
                    f"No company is stored under {symbol!r}. Stored tickers: "
                    f"{', '.join(stored) if stored else 'none'}."
                ),
            )

    # --- everything over the network, before any write transaction --------------------
    budget = RequestBudget(limit=MAX_REQUESTS)
    with SecEdgarClient() as client:
        discovery = client.discover_forms(
            company.cik, ORIGINAL_FORMS + AMENDMENT_FORMS
        )
        selection = select_filings(discovery, cutoff=cutoff, as_of=as_of)

        documents, document_notes = _fetch_documents(
            client, company.cik, selection.filings, cutoff=cutoff, budget=budget
        )

        snapshot_payload, snapshot_url, fetched_at = fetch_payload(client, company.cik)
        budget.spend()

    # Only observations from the selected 10-K and 10-Q, so a figure cannot enter from a
    # filing whose availability was never established.
    fact_accessions = {
        filing.accession_number
        for filing in selection.filings
        if filing.form_type in ("10-K", "10-Q")
    }
    parsed_facts = parse_company_facts(snapshot_payload, accessions=fact_accessions)

    snapshot = FetchedSnapshot(
        payload=snapshot_payload,
        source_url=snapshot_url,
        retrieved_at=fetched_at,
        byte_size=len(snapshot_payload),
    )

    # --- prepare and validate in memory -----------------------------------------------
    limitations = list(selection.limitations) + list(parsed_facts.limitations)
    if budget.exhausted:
        limitations.append(
            f"The request budget of {MAX_REQUESTS} was reached, so some documents were not "
            "fetched. This run's coverage is incomplete."
        )

    parameters = {
        "ticker": company.ticker,
        "cik": company.cik,
        "as_of": as_of.isoformat(),
        "scope": CONTEXT_SCOPE,
        "dry_run": dry_run,
    }

    # --- one short transaction ---------------------------------------------------------
    session = SessionLocal()
    try:
        if dry_run:
            # Classified, not written: `persist_context` issues no INSERT on a dry run, and
            # no transaction is opened, so nothing here can commit.
            counts = persist_context(
                session,
                company_id=company.id,
                filings=selection.filings,
                documents=documents,
                snapshot=snapshot,
                observations=parsed_facts.observations,
                dry_run=True,
            )
            return _summary(
                company=company,
                as_of=as_of,
                cutoff=cutoff,
                selection=selection,
                documents=documents,
                document_notes=document_notes,
                facts=parsed_facts,
                snapshot=snapshot,
                counts=counts,
                limitations=limitations,
                requests_used=budget.used,
                dry_run=True,
            )

        with session.begin():
            counts = persist_context(
                session,
                company_id=company.id,
                filings=selection.filings,
                documents=documents,
                snapshot=snapshot,
                observations=parsed_facts.observations,
            )
            # Counted before the summary is built, so the summary reports the receipt that
            # is about to be written alongside it.
            counts["ingestion_runs"].inserted += 1

            summary = _summary(
                company=company,
                as_of=as_of,
                cutoff=cutoff,
                selection=selection,
                documents=documents,
                document_notes=document_notes,
                facts=parsed_facts,
                snapshot=snapshot,
                counts=counts,
                limitations=limitations,
                requests_used=budget.used,
                dry_run=False,
            )
            record_context_run(
                session,
                company_id=company.id,
                parameters=parameters,
                summary=summary,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
            )
        return summary
    finally:
        session.close()


# --- selection ----------------------------------------------------------------------------


def select_filings(discovery, *, cutoff: datetime, as_of: date) -> Selection:
    """Choose the latest original 10-K and 10-Q, and up to three recent 8-Ks.

    Only filings accepted strictly before the cutoff are eligible, so a later submission is
    never used for an earlier analysis. "Latest" is latest acceptance, with the accession
    number breaking ties so the choice does not depend on feed ordering.
    """
    limitations: list[str] = []
    usable = [
        ref
        for ref in discovery.filings
        if ref.acceptance_datetime is not None and ref.acceptance_datetime < cutoff
    ]
    unknown_acceptance = [
        ref.accession_number
        for ref in discovery.filings
        if ref.acceptance_datetime is None
    ]

    windows: Mapping[str, Sequence] = {
        "10-K": [ref for ref in usable if ref.form_type == "10-K"],
        "10-Q": [ref for ref in usable if ref.form_type == "10-Q"],
    }

    chosen: list[SelectedFiling] = []
    for form_type, candidates in windows.items():
        if not candidates:
            limitations.append(
                f"No original {form_type} appears in the searched submissions window. That "
                "is a limit of the search, not a statement that the company has never "
                "filed one -- this milestone does not page through older filings."
            )
            continue
        latest = max(candidates, key=lambda ref: (ref.acceptance_datetime, ref.accession_number))
        chosen.append(_to_selected(latest, discovery.issuer_cik))

    window_start = _window_start(as_of, EIGHT_K_WINDOW_DAYS)
    eight_ks = sorted(
        (
            ref
            for ref in usable
            if ref.form_type == "8-K"
            and window_start <= ref.acceptance_datetime < cutoff
        ),
        key=lambda ref: (ref.acceptance_datetime, ref.accession_number),
        reverse=True,
    )
    if not eight_ks:
        limitations.append(
            f"No original 8-K was accepted in the {EIGHT_K_WINDOW_DAYS} days before "
            f"{as_of}. Again a limit of the window and the search, not of the company."
        )
    chosen.extend(_to_selected(ref, discovery.issuer_cik) for ref in eight_ks[:MAX_EIGHT_K])
    if len(eight_ks) > MAX_EIGHT_K:
        limitations.append(
            f"{len(eight_ks)} 8-Ks were in the window and only the {MAX_EIGHT_K} most recent "
            "were taken. These are recent filings, not a claim to be the material ones or "
            "the most relevant."
        )

    amendments = tuple(
        sorted(
            ref.accession_number
            for ref in usable
            if ref.form_type in AMENDMENT_FORMS
        )
    )
    if amendments:
        limitations.append(
            f"{len(amendments)} amendment(s) were accepted before the cutoff: "
            f"{', '.join(amendments)}. They are reported, not applied: nothing here "
            "replaces or merges an amended filing, and amendment resolution is not "
            "implemented."
        )
    if unknown_acceptance:
        limitations.append(
            f"{len(unknown_acceptance)} filing(s) in the window carry no acceptance "
            "timestamp, so the cutoff cannot be applied to them and they were not "
            "considered."
        )

    return Selection(
        filings=tuple(chosen),
        amendment_accessions=amendments,
        limitations=tuple(limitations),
        recent_filings_scanned=discovery.recent_filings_scanned,
        older_filing_files_not_searched=discovery.older_filing_files_not_searched,
    )


def _to_selected(ref, issuer_cik: str) -> SelectedFiling:
    name = ref.primary_document.rsplit("/", 1)[-1]
    return SelectedFiling(
        accession_number=ref.accession_number,
        form_type=ref.form_type,
        filing_date=ref.filing_date,
        report_date=ref.report_date,
        acceptance_datetime=ref.acceptance_datetime,
        primary_document=name,
        source_document_url=archive_file_url(
            issuer_cik, ref.accession_number, ref.primary_document
        ),
    )


def _window_start(as_of: date, days: int) -> datetime:
    """The instant `days` calendar days before `as_of` begins, in the market's timezone.

    Expressed through the same cutoff helper the analysis uses, so "the preceding 90 days"
    means the same kind of 90 days in both places.
    """
    return information_cutoff(as_of - timedelta(days=days + 1))


# --- documents ----------------------------------------------------------------------------


def _fetch_documents(
    client: SecEdgarClient,
    issuer_cik: str,
    filings: Sequence[SelectedFiling],
    *,
    cutoff: datetime,
    budget: RequestBudget,
) -> tuple[tuple[FetchedDocument, ...], tuple[str, ...]]:
    """Fetch each filing's primary document, plus exhibits for the 8-Ks.

    Returns the documents and a note for every candidate that was not fetched, so an
    omission is reported rather than passed off as nothing to fetch.
    """
    documents: list[FetchedDocument] = []
    notes: list[str] = []

    for filing in filings:
        if budget.exhausted:
            notes.append(
                f"{filing.accession_number}: not fetched, the request budget was reached."
            )
            continue

        outcome = _fetch_one(
            client,
            archive_file_url(issuer_cik, filing.accession_number, filing.primary_document),
            filing=filing,
            document_name=filing.primary_document,
            document_type=filing.form_type,
            role="primary",
            sequence=None,
            budget=budget,
        )
        if outcome.document is not None:
            documents.append(outcome.document)
        if outcome.skipped_reason:
            notes.append(f"{filing.accession_number} primary: {outcome.skipped_reason}")

        if filing.form_type == "8-K":
            exhibit_documents, exhibit_notes = _fetch_exhibits(
                client, issuer_cik, filing, budget=budget
            )
            documents.extend(exhibit_documents)
            notes.extend(exhibit_notes)

    return tuple(documents), tuple(notes)


def _fetch_exhibits(
    client: SecEdgarClient,
    issuer_cik: str,
    filing: SelectedFiling,
    *,
    budget: RequestBudget,
) -> tuple[list[FetchedDocument], list[str]]:
    """Read the filing's index and fetch its eligible EX-99 exhibits.

    The primary 8-K is a cover page; the earnings release is an exhibit beside it, which is
    why this looks rather than assuming.
    """
    notes: list[str] = []
    if budget.exhausted:
        notes.append(
            f"{filing.accession_number}: exhibits not looked for, the request budget was "
            "reached."
        )
        return [], notes

    index_url = archive_index_html_url(issuer_cik, filing.accession_number)
    index_html, _ = client.fetch_document(index_url)
    budget.spend()

    entries = parse_filing_index(index_html)
    if not entries:
        notes.append(
            f"{filing.accession_number}: its filing index could not be read, so no exhibit "
            "was selected. This is a failure to read the index, not a filing with no "
            "exhibits."
        )
        return [], notes

    chosen, omitted = _select_exhibits(entries)
    notes.extend(f"{filing.accession_number}: {reason}" for reason in omitted)

    documents: list[FetchedDocument] = []
    for entry in chosen:
        if budget.exhausted:
            notes.append(
                f"{filing.accession_number}: exhibit {entry.document_name} not fetched, the "
                "request budget was reached."
            )
            break
        outcome = _fetch_one(
            client,
            archive_file_url(issuer_cik, filing.accession_number, entry.document_name),
            filing=filing,
            document_name=entry.document_name,
            document_type=entry.document_type,
            role="exhibit",
            sequence=entry.sequence,
            budget=budget,
        )
        if outcome.document is not None:
            documents.append(outcome.document)
        if outcome.skipped_reason:
            notes.append(
                f"{filing.accession_number} {entry.document_name}: {outcome.skipped_reason}"
            )
    return documents, notes


def _select_exhibits(
    entries: Sequence[FilingIndexEntry],
) -> tuple[list[FilingIndexEntry], list[str]]:
    """Choose up to two EX-99 exhibits, and explain every EX-99 that was not chosen.

    Ordered by exhibit number so "EX-99.1 before EX-99.2" is a rule rather than a
    coincidence of the index's ordering.
    """
    exhibits = [entry for entry in entries if entry.document_type.upper().startswith(_EXHIBIT_TYPE_PREFIX)]
    exhibits.sort(key=lambda entry: (_exhibit_number(entry.document_type), entry.sequence or 0))

    chosen: list[FilingIndexEntry] = []
    omitted: list[str] = []
    for entry in exhibits:
        if len(chosen) >= MAX_EXHIBITS_PER_FILING:
            omitted.append(
                f"exhibit {entry.document_name} ({entry.document_type}) not fetched: "
                f"only {MAX_EXHIBITS_PER_FILING} exhibits are taken per filing."
            )
            continue
        if not entry.document_name.lower().endswith(_FETCHABLE_EXTENSIONS):
            omitted.append(
                f"exhibit {entry.document_name} ({entry.document_type}) not fetched: only "
                "HTML and text exhibits are read, so a PDF or another binary format is left "
                "alone rather than being claimed as covered."
            )
            continue
        chosen.append(entry)

    return chosen, omitted


def _exhibit_number(document_type: str) -> float:
    match = _EXHIBIT_NUMBER.match(document_type.strip())
    if not match or not match.group(1):
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _fetch_one(
    client: SecEdgarClient,
    url: str,
    *,
    filing: SelectedFiling,
    document_name: str,
    document_type: str,
    role: str,
    sequence: int | None,
    budget: RequestBudget,
) -> DocumentOutcome:
    """Fetch and extract one document, or say why it was left out."""
    if budget.exhausted:
        return DocumentOutcome(None, "not fetched, the request budget was reached.")

    try:
        content, content_type = client.fetch_document(url)
    except SecEdgarError as exc:
        # A document that cannot be fetched is one omission, not the end of the run. It is
        # reported, and the run's coverage is marked incomplete.
        return DocumentOutcome(None, f"not fetched: {exc}")
    finally:
        budget.spend()

    extraction, status = _extract(content, document_name, filing.form_type)
    return DocumentOutcome(
        FetchedDocument(
            filing_accession=filing.accession_number,
            document_name=document_name,
            document_type=document_type,
            role=role,
            sequence=sequence,
            source_url=url,
            content_type=content_type,
            content=content,
            retrieved_at=datetime.now(timezone.utc),
            extraction=extraction,
            extraction_status=status,
        ),
        None,
    )


def _extract(
    content: str, document_name: str, form_type: str
) -> tuple[ExtractedDocument | None, str]:
    """Extract readable text, and say what kind of extraction it was."""
    lowered = document_name.lower()
    if lowered.endswith((".htm", ".html")):
        return extract(content, form_type=form_type), "extracted"

    if lowered.endswith(".txt"):
        # Already text. There is no markup to strip, so the document is its own extraction
        # and no sections can be located in it.
        return (
            ExtractedDocument(
                text=content,
                sections={},
                limitations=(
                    "This document is plain text, so it was stored as-is without markup "
                    "extraction and no sections were looked for.",
                ),
                extraction_version=EXTRACTION_VERSION,
            ),
            "extracted",
        )

    return None, "unsupported"


# --- output -------------------------------------------------------------------------------


def _summary(
    *,
    company,
    as_of: date,
    cutoff: datetime,
    selection: Selection,
    documents: Sequence[FetchedDocument],
    document_notes: Sequence[str],
    facts: ParsedCompanyFacts,
    snapshot: FetchedSnapshot,
    counts,
    limitations: Sequence[str],
    requests_used: int,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "company": {"cik": company.cik, "name": company.name, "ticker": company.ticker},
        "as_of": as_of.isoformat(),
        "information_cutoff": cutoff.isoformat(),
        "scope": CONTEXT_SCOPE,
        "selection": {
            "criterion": (
                "latest original 10-K and 10-Q accepted before the cutoff, plus up to "
                f"{MAX_EIGHT_K} original 8-Ks from the preceding {EIGHT_K_WINDOW_DAYS} days"
            ),
            "filings": [
                {
                    "accession_number": filing.accession_number,
                    "form_type": filing.form_type,
                    "filing_date": filing.filing_date.isoformat(),
                    "report_date": (
                        None if filing.report_date is None else filing.report_date.isoformat()
                    ),
                    "acceptance_datetime": (
                        None
                        if filing.acceptance_datetime is None
                        else filing.acceptance_datetime.isoformat()
                    ),
                }
                for filing in selection.filings
            ],
            "amendment_accessions": list(selection.amendment_accessions),
            "recent_filings_scanned": selection.recent_filings_scanned,
            "older_filing_files_not_searched": selection.older_filing_files_not_searched,
        },
        "documents": {
            "fetched": len(documents),
            "by_role": {
                "primary": sum(1 for d in documents if d.role == "primary"),
                "exhibit": sum(1 for d in documents if d.role == "exhibit"),
            },
            "with_text": sum(1 for d in documents if d.extraction is not None),
            "omissions": list(document_notes),
        },
        "facts": {
            "snapshot_label": SNAPSHOT_LABEL,
            "source_url": snapshot.source_url,
            "content_sha256": content_sha256(snapshot.payload),
            "byte_size": snapshot.byte_size,
            "observation_count": len(facts.observations),
            "concepts_matched": list(facts.concepts_matched),
            "concepts_unmatched": list(facts.concepts_unmatched),
        },
        "counts": _counts_document(counts),
        "coverage_incomplete": bool(document_notes) or requests_used >= MAX_REQUESTS,
        "requests_used": requests_used,
        "request_budget": MAX_REQUESTS,
        "limitations": list(limitations),
        "dry_run": dry_run,
    }


def _counts_document(counts) -> dict[str, Any]:
    return {table: value.as_dict() for table, value in counts.items()}


def _unavailable(*, symbol: str, as_of: date, cutoff: datetime, note: str) -> dict[str, Any]:
    """A result for the case where there is no company to work on."""
    return {
        "company": None,
        "requested_symbol": symbol,
        "as_of": as_of.isoformat(),
        "information_cutoff": cutoff.isoformat(),
        "scope": CONTEXT_SCOPE,
        "selection": None,
        "documents": None,
        "facts": None,
        "counts": {},
        "coverage_incomplete": True,
        "limitations": [note],
        "dry_run": False,
    }


def _company_for(session: Session, symbol: str) -> StoredCompany | None:
    """The company's identity, read out of the session before it closes.

    A plain value rather than the ORM object: everything downstream of this runs after the
    session that loaded it has gone, and a detached instance would either fail on a lazy
    load or, worse, appear to work until it did not.
    """
    row = session.execute(
        select(Company.id, Company.sec_issuer_cik, Company.name, Company.ticker).where(
            func.upper(Company.ticker) == symbol
        )
    ).one_or_none()
    return None if row is None else StoredCompany(*row)


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date in YYYY-MM-DD form"
        ) from None


if __name__ == "__main__":
    raise SystemExit(main())
