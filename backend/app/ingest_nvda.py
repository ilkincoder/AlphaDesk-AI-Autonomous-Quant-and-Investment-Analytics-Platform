"""Fetch and store NVIDIA's daily prices and recent Form 4 filings.

    docker compose exec backend python -m app.ingest_nvda --bars 30 --filings 3
    docker compose exec backend python -m app.ingest_nvda --dry-run

A fixed-identity wrapper around `app.company_ingestion.run`, kept because NVIDIA is the
company this project is developed against and the shorter command is the one in the README
and in every earlier step. It is a convenience, not a separate code path: every rule about
identity, precision, adjustment metadata, duplicate detection and conflicts lives in the
shared module, and this file supplies one `CompanyIdentity` and nothing else.

For another ticker, use `app.ingest_company`, which takes the symbol, exchange and CIK
explicitly:

    docker compose exec backend python -m app.ingest_company \\
      --symbol AAPL --exchange NASDAQ --cik 0000320193 --bars 30 --filings 3
"""

import argparse
import json
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.company_ingestion import CompanyIdentity, parse_identity, run
from app.ingestion import IngestionError, IngestionSummary
from app.sec_edgar import DEFAULT_LIMIT as DEFAULT_FILINGS, MAX_LIMIT as MAX_FILINGS
from app.sec_edgar import SecEdgarError
from app.twelvedata import DEFAULT_BARS, MAX_BARS, TwelveDataError

# NVIDIA's identity, stated once. `parse_identity` normalizes it the same way a user-supplied
# identity is normalized, so the fixed path cannot drift from the general one.
NVDA = CompanyIdentity(symbol="NVDA", exchange="NASDAQ", cik="0001045810")

EXIT_OK = 0
EXIT_FAILED = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest_nvda",
        description=(
            "Fetch recent daily prices and Form 4 filings for NVIDIA and store them. "
            "Safe to run repeatedly: identical data is left exactly as it is."
        ),
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=DEFAULT_BARS,
        help=f"daily bars to fetch (default: {DEFAULT_BARS}, maximum: {MAX_BARS})",
    )
    parser.add_argument(
        "--filings",
        type=int,
        default=DEFAULT_FILINGS,
        help=f"Form 4 filings to fetch (default: {DEFAULT_FILINGS}, maximum: {MAX_FILINGS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and validate, report what would change, and write nothing",
    )
    args = parser.parse_args(argv)

    try:
        summary = run_nvda(args.bars, args.filings, dry_run=args.dry_run)
    except (SecEdgarError, TwelveDataError, IngestionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except SQLAlchemyError as exc:
        print(
            f"error: could not read or write the stored data: {type(exc).__name__}",
            file=sys.stderr,
        )
        return EXIT_FAILED

    json.dump(summary.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


def run_nvda(bars: int, filings: int, *, dry_run: bool) -> IngestionSummary:
    """Fetch, validate and store NVIDIA. The identity is checked like any other."""
    return run(parse_identity(NVDA.symbol, NVDA.exchange, NVDA.cik), bars=bars,
               filings=filings, dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
