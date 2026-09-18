"""Search a company's indexed SEC filings for passages relevant to a question.

    docker compose exec backend python -m app.search_filings --symbol NVDA \\
        --as-of 2026-09-17 --query "What export restrictions does the company disclose?" \\
        --top-k 5

`--as-of` is required and has no default. There is no sensible one: every other default in
this project stands in for "the stored range" or "today", but a question about what a filing
said is a question about what was knowable on a particular date, and guessing that date would
silently change the answer.

**This retrieves passages. It does not answer the question.** A result that returns five
relevant passages has found five relevant passages, and nothing more -- it does not mean the
question is answered, and it says nothing about whether the filings held here are complete.
Both of those are warnings on the result rather than things a reader has to remember.

Exit status is 0 for every answer about the data, including "nothing matched". It is 1 only
when the search could not be carried out at all -- the index or the model being unavailable,
or the arguments being wrong. Losing the index is a fault; finding nothing in it is an answer.
"""

import argparse
import json
import sys
from datetime import date

from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import SessionLocal
from app.filing_index import (
    STATUS_INDEX_UNAVAILABLE,
    STATUS_MODEL_UNAVAILABLE,
    RetrievalResult,
    search_filings,
)
from app.embeddings import EmbeddingError, get_embedder
from app.vector_store import VectorStore, VectorStoreError

DEFAULT_SYMBOL = "NVDA"
DEFAULT_TOP_K = 5
MAX_TOP_K = 20

EXIT_OK = 0
EXIT_FAILED = 1

# Statuses that mean the search could not run, as opposed to running and finding nothing.
_BROKEN = frozenset({STATUS_INDEX_UNAVAILABLE, STATUS_MODEL_UNAVAILABLE})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.search_filings",
        description=(
            "Retrieve passages from a company's indexed SEC filings. Returns evidence, not "
            "answers and not recommendations."
        ),
    )
    parser.add_argument(
        "--symbol", default=DEFAULT_SYMBOL, help=f"ticker (default: {DEFAULT_SYMBOL})"
    )
    parser.add_argument(
        "--as-of",
        type=_iso_date,
        required=True,
        help="analysis cut-off date, YYYY-MM-DD; required, because it decides what counts "
        "as publicly available",
    )
    parser.add_argument("--query", required=True, help="the question to search for")
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"how many passages to return (default: {DEFAULT_TOP_K}, maximum: {MAX_TOP_K})",
    )
    args = parser.parse_args(argv)

    symbol = args.symbol.strip().upper()
    if not symbol:
        print("error: --symbol must not be empty", file=sys.stderr)
        return EXIT_FAILED
    if not args.query.strip():
        print("error: --query must not be empty", file=sys.stderr)
        return EXIT_FAILED
    if not 1 <= args.top_k <= MAX_TOP_K:
        print(
            f"error: --top-k must be between 1 and {MAX_TOP_K}", file=sys.stderr
        )
        return EXIT_FAILED

    try:
        result = run(symbol, args.query, args.as_of, args.top_k)
    except (VectorStoreError, EmbeddingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except SQLAlchemyError as exc:
        print(
            f"error: could not read the stored data: {type(exc).__name__}",
            file=sys.stderr,
        )
        return EXIT_FAILED

    json.dump(result.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_FAILED if result.status in _BROKEN else EXIT_OK


def run(symbol: str, query: str, as_of: date, top_k: int) -> RetrievalResult:
    store = VectorStore(
        url=settings.qdrant_url, collection_name=settings.qdrant_collection
    )
    try:
        embedder = get_embedder()
        session = SessionLocal()
        try:
            return search_filings(
                session,
                symbol=symbol,
                query=query,
                as_of=as_of,
                top_k=top_k,
                store=store,
                embedder=embedder,
            )
        finally:
            session.close()
    finally:
        store.close()


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date in YYYY-MM-DD form"
        ) from None


if __name__ == "__main__":
    raise SystemExit(main())
