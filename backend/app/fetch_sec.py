"""Fetch recent SEC Form 4 filings for a company. Read-only.

Prints what it found, then exits. It opens no database session, writes no rows, and
touches no portfolio. It is not wired into application startup.

Persisting filings into `sec_filings`, `insider_transactions`, and
`insider_reporting_owners` is Step 4. This command exists so the client and parser can be
exercised against the real SEC before anything depends on them.

    docker compose exec backend python -m app.fetch_sec
    docker compose exec backend python -m app.fetch_sec --cik 0001045810 --limit 3

Every decimal is printed as a JSON **string**, never a JSON number -- the same reasoning
as `fetch_prices`. `volume`-style integers do not appear here, but share counts and prices
do, and a JSON number would be a double.
"""

import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal

from app.form4 import Form4Error
from app.sec_edgar import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    DiscoveryResult,
    FilingRecord,
    SecEdgarClient,
    SecEdgarError,
)

# NVIDIA's CIK, and the ticker SEC metadata must still report for it. The ticker is
# checked at run time rather than trusted: a number that meant NVDA when this was written
# is not evidence that it means NVDA now.
NVDA_CIK = "0001045810"
NVDA_TICKER = "NVDA"

EXIT_OK = 0
EXIT_FAILED = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.fetch_sec",
        description=(
            "Fetch recent SEC Form 4 insider filings for one company and print them. "
            "Read-only: nothing is stored."
        ),
    )
    parser.add_argument(
        "--cik",
        default=None,
        help=f"issuer CIK (default: {NVDA_CIK}, NVIDIA)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"how many filings to fetch (default: {DEFAULT_LIMIT}, maximum: {MAX_LIMIT})",
    )
    args = parser.parse_args(argv)

    # `None` means the flag was not given, which is what turns on the NVDA check. An
    # explicitly passed CIK is taken at face value.
    using_default_cik = args.cik is None
    cik = NVDA_CIK if using_default_cik else args.cik

    try:
        discovery, records = collect(cik, args.limit, verify_nvda=using_default_cik)
    except (SecEdgarError, Form4Error) as exc:
        # Only anticipated failures are caught. Each carries a message already stripped
        # of anything sensitive, and printing it *instead of* a traceback is the point --
        # a traceback is where a request URL or a header would surface.
        #
        # A genuine bug is left to raise: a crash that hides behind "something went
        # wrong" is harder to fix than one that shows its trace.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED

    json.dump(_document(discovery, records), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


def collect(
    cik: str, limit: int, *, verify_nvda: bool = False
) -> tuple[DiscoveryResult, tuple[FilingRecord, ...]]:
    """Discover and fetch. One session, so the rate limiter covers the whole run."""
    with SecEdgarClient() as client:
        discovery = client.discover(cik, limit)

        if verify_nvda and NVDA_TICKER not in discovery.tickers:
            raise SecEdgarError(
                f"the default CIK {NVDA_CIK} does not resolve to {NVDA_TICKER} in SEC "
                f"metadata. It reports {discovery.issuer_name!r} with tickers "
                f"{list(discovery.tickers) or 'none'}. Pass --cik explicitly if that is "
                "the company you meant."
            )

        records = client.fetch_filings(discovery)
        return discovery, records


def _scope_sentence(discovery: DiscoveryResult) -> str:
    """What was searched, in words, so the limit is not mistaken for a full history."""
    return (
        f"SEC submissions 'recent' list only: {discovery.recent_filings_scanned} "
        f"filings scanned; {discovery.older_filing_files_not_searched} older filings "
        "held in separate submission files were NOT searched"
    )


def _document(
    discovery: DiscoveryResult, records: tuple[FilingRecord, ...]
) -> dict:
    return {
        "discovery": {
            "issuer_cik": discovery.issuer_cik,
            "issuer_name": discovery.issuer_name,
            "tickers": list(discovery.tickers),
            "exchanges": list(discovery.exchanges),
            "scope": _scope_sentence(discovery),
            "recent_filings_scanned": discovery.recent_filings_scanned,
            "older_filing_files_not_searched": discovery.older_filing_files_not_searched,
            "filing_count": len(records),
        },
        "filings": [_filing_document(record) for record in records],
    }


def _filing_document(record: FilingRecord) -> dict:
    document = record.document
    return {
        "accession_number": record.filing.accession_number,
        "form_type": record.filing.form_type,
        "is_amendment": document.is_amendment,
        "filing_date": _date_text(record.filing.filing_date),
        "acceptance_datetime": _datetime_text(record.filing.acceptance_datetime),
        "report_date": _date_text(record.filing.report_date),
        "retrieved_at": _datetime_text(record.retrieved_at),
        "source_xml_url": record.source_xml_url,
        "issuer": {
            "cik": document.issuer_cik,
            "name": document.issuer_name,
            "trading_symbol": document.issuer_trading_symbol,
        },
        "document_type": document.document_type,
        "schema_version": document.schema_version,
        "period_of_report": _date_text(document.period_of_report),
        "date_of_original_submission": _date_text(document.date_of_original_submission),
        "rule_10b5_1": document.rule_10b5_1,
        "remarks": document.remarks,
        "holding_rows_skipped": document.holding_rows_skipped,
        "footnotes": dict(document.footnotes),
        "owners": [
            {
                "owner_cik": owner.owner_cik,
                "owner_name": owner.owner_name,
                "is_director": owner.is_director,
                "is_officer": owner.is_officer,
                "is_ten_percent_owner": owner.is_ten_percent_owner,
                "is_other": owner.is_other,
                "officer_title": owner.officer_title,
                "other_text": owner.other_text,
            }
            for owner in document.owners
        ],
        "transactions": [
            {
                "source_table": row.source_table,
                "row_position": row.row_position,
                "security_title": row.security_title,
                "transaction_date": _date_text(row.transaction_date),
                "transaction_code": row.transaction_code,
                "acquired_disposed": row.acquired_disposed,
                "shares": _decimal_text(row.shares),
                "price_per_share": _decimal_text(row.price_per_share),
                "ownership_direct_indirect": row.ownership_direct_indirect,
                "nature_of_ownership": row.nature_of_ownership,
                "shares_owned_following": _decimal_text(row.shares_owned_following),
                "footnote_refs": [
                    {"field": ref.field, "footnote_id": ref.footnote_id}
                    for ref in row.footnote_refs
                ],
                "underlying_security_title": row.underlying_security_title,
                "underlying_shares": _decimal_text(row.underlying_shares),
                "exercise_price": _decimal_text(row.exercise_price),
                "expiration_date": _date_text(row.expiration_date),
            }
            for row in document.transactions
        ],
    }


def _decimal_text(value: Decimal | None) -> str | None:
    """`str()`, deliberately. The conversion to text is a decision, not a fallback."""
    return None if value is None else str(value)


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _datetime_text(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


if __name__ == "__main__":
    raise SystemExit(main())