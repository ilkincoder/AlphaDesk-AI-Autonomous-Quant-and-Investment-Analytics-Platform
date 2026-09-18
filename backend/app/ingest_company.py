"""Fetch and store one company's daily prices and recent Form 4 filings.

    docker compose exec backend python -m app.ingest_company \\
      --symbol AAPL --exchange NASDAQ --cik 0000320193 --bars 30 --filings 3

The company must be named, not guessed. `--symbol`, `--exchange` and `--cik` are all required,
because identity is the thing this command establishes and a default for any of the three
would be an assumption about which company was meant.

Adding a company is three commands, run by hand, in this order:

    python -m app.ingest_company          # this file: prices and Form 4 filings
    python -m app.ingest_company_context  # the 10-K, 10-Q, 8-Ks and financial facts
    python -m app.index_filings           # chunk and embed the filing text

and then `python -m app.search_filings`, `python -m app.analyze_insiders` and
`python -m app.ingest_company_context` all work for it, each filtered to that company. Nothing
runs on a schedule and nothing fetches on its own.

Safe to run repeatedly: identical data adds no rows and leaves the stored ones alone.
"""

import argparse
import json
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.company_ingestion import parse_identity, run
from app.embeddings import EmbeddingError
from app.ingestion import IngestionError
from app.sec_edgar import DEFAULT_LIMIT as DEFAULT_FILINGS, MAX_LIMIT as MAX_FILINGS
from app.sec_edgar import SecEdgarError
from app.twelvedata import DEFAULT_BARS, MAX_BARS, TwelveDataError

EXIT_OK = 0
EXIT_FAILED = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest_company",
        description=(
            "Fetch and store daily prices and recent Form 4 filings for one company. "
            "Safe to run repeatedly: identical data is left exactly as it is."
        ),
    )
    parser.add_argument("--symbol", required=True, help="ticker, for example AAPL")
    parser.add_argument(
        "--exchange", required=True, help="exchange it lists on, for example NASDAQ"
    )
    parser.add_argument(
        "--cik",
        required=True,
        help="SEC issuer CIK, for example 0000320193; leading zeros are preserved",
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
        identity = parse_identity(args.symbol, args.exchange, args.cik)
        summary = run(
            identity, bars=args.bars, filings=args.filings, dry_run=args.dry_run
        )
    except (SecEdgarError, TwelveDataError, IngestionError, EmbeddingError) as exc:
        # Every anticipated failure -- a bad identity, a refusal, a provider problem -- is
        # reported as itself. The identity refusals in particular name both what was asked
        # for and what was found, because that is the whole content of the message.
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


if __name__ == "__main__":
    raise SystemExit(main())
