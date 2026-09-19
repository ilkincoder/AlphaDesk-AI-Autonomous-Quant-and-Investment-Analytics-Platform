"""Read-only price and insider-activity analysis for one stored company.

    docker compose exec backend python -m app.analyze_insiders --symbol NVDA
    docker compose exec backend python -m app.analyze_insiders --symbol NVDA \\
        --start 2026-08-06 --end 2026-09-17

Every database read lives here; every rule lives in `app.analysis`, which is pure. This
module's only job is to fetch the right rows and hand them over as plain values.

**No writes and no network.** Every statement is a SELECT, and nothing is fetched from a
provider. Deliberately: an analysis that could fetch its own missing data would be able to
manufacture the answer it was looking for.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.analysis import (
    OWNERSHIP_FORMS,
    PRICE_REASON_AMBIGUOUS_SERIES,
    PRICE_REASON_NO_SERIES,
    UNAVAILABLE_NO_COMPANY,
    AnalysisResult,
    CoverageInput,
    PriceObservation,
    PriceSeriesSelection,
    TransactionRecord,
    analyze,
    unavailable_result,
)
from app.db import SessionLocal
from app.ingestion import FORM4_SCOPE
from app.models import Company, DailyPrice, IngestionRun, InsiderTransaction, SecFiling

DEFAULT_SYMBOL = "NVDA"

EXIT_OK = 0
EXIT_FAILED = 1


@dataclass(frozen=True)
class StoredCompany:
    """The company's identity, read out of the session before it closes."""

    id: int
    cik: str
    name: str
    ticker: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.analyze_insiders",
        description=(
            "Analyse stored daily prices and SEC insider transactions for one company. "
            "Read-only: nothing is written and nothing is fetched."
        ),
    )
    parser.add_argument(
        "--symbol", default=DEFAULT_SYMBOL, help=f"ticker (default: {DEFAULT_SYMBOL})"
    )
    parser.add_argument(
        "--start",
        type=_iso_date,
        default=None,
        help="period start, YYYY-MM-DD (default: the earliest stored price date)",
    )
    parser.add_argument(
        "--end",
        type=_iso_date,
        default=None,
        help="period end, YYYY-MM-DD (default: the latest stored price date)",
    )
    args = parser.parse_args(argv)

    symbol = args.symbol.strip().upper()
    if not symbol:
        print("error: --symbol must not be empty", file=sys.stderr)
        return EXIT_FAILED
    if args.start and args.end and args.start > args.end:
        print(
            f"error: --start {args.start} is after --end {args.end}",
            file=sys.stderr,
        )
        return EXIT_FAILED

    try:
        result = run(symbol, args.start, args.end)
    except SQLAlchemyError as exc:
        # An operational failure -- the database being unreachable, say -- exits nonzero
        # with a short explanation. "We hold no data" does not come through here; that
        # returns a typed unavailable result, because it is an answer rather than a fault.
        #
        # A genuine bug is left to raise. Catching everything here would turn a coding
        # mistake into an unexplained exit code.
        print(
            f"error: could not read the stored data: {type(exc).__name__}", file=sys.stderr
        )
        return EXIT_FAILED

    json.dump(result.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


def run(
    symbol: str, start: date | None, end: date | None, *, session: Session | None = None
) -> AnalysisResult:
    """Read everything, then analyse. Nothing is read after `analyze` is called.

    `session` exists so a caller that already holds one -- the Module 1 tools, which must be
    able to run inside a test's rolled-back transaction -- can supply it instead of this
    function opening its own. With no session given, one is opened and closed here, which is
    what the command line does.
    """
    if session is not None:
        return _analyse(session, symbol, start, end)

    with SessionLocal() as owned:
        return _analyse(owned, symbol, start, end)


def _analyse(
    session: Session, symbol: str, start: date | None, end: date | None
) -> AnalysisResult:
    """The reads and the analysis, over a session the caller owns."""
    company = _company_for(session, symbol)

    if company is None:
        stored = _stored_tickers(session)
        today = date.today()
        return unavailable_result(
            symbol=symbol,
            reason=UNAVAILABLE_NO_COMPANY,
            requested_start=start or today,
            requested_end=end or today,
            note=(
                f"No company is stored under {symbol!r}. Stored tickers: "
                f"{', '.join(stored) if stored else 'none'}. Ingest a company before "
                "analysing it."
            ),
        )

    coverage = _coverage(session, company.id)
    series, series_reason = _price_series(session, company.id)

    if series is None:
        today = date.today()
        return unavailable_result(
            symbol=symbol,
            reason=series_reason or PRICE_REASON_NO_SERIES,
            requested_start=start or today,
            requested_end=end or today,
            company_cik=company.cik,
            company_name=company.name,
            note=_series_note(series_reason),
            coverage=coverage,
        )

    # No emptiness check here: the series above is derived from the stored rows, so a
    # series that exists has at least one observation behind it. A company with no
    # prices at all has no series and was already handled.
    observations = _observations(session, company.id, series)

    # The dates actually used. When the caller gave none, the stored range decides --
    # and the result reports both, so what was asked for and what was used cannot be
    # confused.
    requested_start = start or observations[0].trading_date
    requested_end = end or observations[-1].trading_date
    transactions = _transactions(session, company.id)

    return analyze(
        symbol=symbol,
        company_cik=company.cik,
        company_name=company.name,
        requested_start=requested_start,
        requested_end=requested_end,
        observations=observations,
        transactions=transactions,
        coverage=coverage,
        price_series=series,
    )


# --- reads ------------------------------------------------------------------------------


def _company_for(session: Session, symbol: str) -> StoredCompany | None:
    row = session.execute(
        select(Company.id, Company.sec_issuer_cik, Company.name, Company.ticker).where(
            func.upper(Company.ticker) == symbol
        )
    ).one_or_none()

    return None if row is None else StoredCompany(*row)


def _stored_tickers(session: Session) -> tuple[str, ...]:
    return tuple(session.scalars(select(Company.ticker).order_by(Company.ticker)))


def _price_series(
    session: Session, company_id: int
) -> tuple[PriceSeriesSelection | None, str | None]:
    """The one stored series, or a reason there is not exactly one.

    Two series are refused rather than picked between. Raw and adjusted prices are
    different numbers for the same day, and a change computed across them would be an
    artefact of mixing them.
    """
    rows = session.execute(
        select(
            DailyPrice.provider,
            DailyPrice.adjustment_basis,
            DailyPrice.provider_adjust_mode,
        )
        .where(DailyPrice.company_id == company_id)
        .distinct()
        .order_by(
            DailyPrice.provider,
            DailyPrice.adjustment_basis,
            DailyPrice.provider_adjust_mode,
        )
    ).all()

    if not rows:
        return None, PRICE_REASON_NO_SERIES
    if len(rows) > 1:
        return None, PRICE_REASON_AMBIGUOUS_SERIES

    provider, basis, mode = rows[0]
    return PriceSeriesSelection(
        provider=provider, adjustment_basis=basis, provider_adjust_mode=mode
    ), None


def _observations(
    session: Session, company_id: int, series: PriceSeriesSelection
) -> list[PriceObservation]:
    rows = session.execute(
        select(DailyPrice.trading_date, DailyPrice.close)
        .where(
            DailyPrice.company_id == company_id,
            DailyPrice.provider == series.provider,
            DailyPrice.adjustment_basis == series.adjustment_basis,
            DailyPrice.provider_adjust_mode == series.provider_adjust_mode,
        )
        .order_by(DailyPrice.trading_date)
    ).all()

    return [PriceObservation(trading_date=row[0], close=row[1]) for row in rows]


def _transactions(session: Session, company_id: int) -> list[TransactionRecord]:
    """Every stored transaction for the company, **without touching owners**.

    `insider_reporting_owners` is not joined, and that is the point: owners attach to the
    filing, so joining them here would return each transaction once per owner and silently
    multiply every count and total built from it. The accession number is carried instead,
    so an owner can be looked up deliberately.
    """
    rows = session.execute(
        select(
            SecFiling.accession_number,
            InsiderTransaction.source_table,
            InsiderTransaction.row_position,
            InsiderTransaction.security_title,
            InsiderTransaction.transaction_date,
            InsiderTransaction.transaction_code,
            InsiderTransaction.acquired_disposed,
            InsiderTransaction.shares,
            InsiderTransaction.price_per_share,
            InsiderTransaction.is_derivative,
            SecFiling.acceptance_datetime,
            SecFiling.rule_10b5_1,
            SecFiling.is_amendment,
            SecFiling.source_document_url,
        )
        .join(SecFiling, SecFiling.id == InsiderTransaction.filing_id)
        .where(
            SecFiling.company_id == company_id,
            # Explicit, not incidental: only Form 4 reports insider transactions, and
            # disclosure filings now share this table.
            SecFiling.form_type.in_(OWNERSHIP_FORMS),
        )
        .order_by(
            SecFiling.accession_number,
            InsiderTransaction.source_table,
            InsiderTransaction.row_position,
        )
    ).all()

    return [TransactionRecord(**row._mapping) for row in rows]


def _coverage(session: Session, company_id: int) -> CoverageInput:
    """What the ingestion receipts say about how much was searched."""
    # Form 4 runs only. A company-context run also writes `ingestion_runs`, and if it were
    # counted here it would become "the latest run" and report its three disclosure filings
    # as insider-history coverage -- a generic filing count standing in for a specific one.
    by_scope = (
        IngestionRun.company_id == company_id,
        IngestionRun.scope == FORM4_SCOPE,
    )

    run_count = session.scalar(
        select(func.count()).select_from(IngestionRun).where(*by_scope)
    ) or 0
    if run_count == 0:
        return CoverageInput(0, None, None, None, None)

    latest = session.scalars(
        select(IngestionRun)
        .where(*by_scope)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    ).one()

    filings = (latest.summary or {}).get("filings") or {}
    return CoverageInput(
        ingestion_run_count=run_count,
        latest_run_started_at=latest.started_at,
        latest_requested_filings=filings.get("requested_filings"),
        latest_stored_filings=filings.get("returned_filings"),
        discovery_scope=filings.get("discovery_scope"),
    )


def _series_note(reason: str | None) -> str:
    if reason == PRICE_REASON_AMBIGUOUS_SERIES:
        return (
            "More than one price series is stored for this company, and they are different "
            "numbers for the same days. Refusing to pick one: which series to analyse is a "
            "choice a person should make, not something to guess."
        )
    return "No daily prices are stored for this company."


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date in YYYY-MM-DD form"
        ) from None


if __name__ == "__main__":
    raise SystemExit(main())