"""Index a company's stored filing text into Qdrant.

    docker compose exec backend python -m app.index_filings --symbol NVDA --dry-run
    docker compose exec backend python -m app.index_filings --symbol NVDA

Reads what Step 5B stored in PostgreSQL and writes vectors to Qdrant. It fetches nothing:
the text is already here, and a command that could reach out for more would be able to change
what it is indexing while it indexes it.

Safe to run repeatedly. A document whose text and configuration are unchanged is skipped
without being embedded again, and a document that is re-indexed lands on the same
deterministic point ids, so nothing duplicates.
"""

import argparse
import json
import sys

from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import SessionLocal
from app.embeddings import EmbeddingError, get_embedder
from app.filing_index import IndexSummary, IndexingError, index_company
from app.vector_store import VectorStoreError

DEFAULT_SYMBOL = "NVDA"

EXIT_OK = 0
EXIT_FAILED = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.index_filings",
        description=(
            "Chunk and embed a company's stored SEC filing text into Qdrant. Re-running "
            "with unchanged documents adds no points and embeds nothing again."
        ),
    )
    parser.add_argument(
        "--symbol", default=DEFAULT_SYMBOL, help=f"ticker (default: {DEFAULT_SYMBOL})"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="chunk and report what would be indexed, and write nothing",
    )
    args = parser.parse_args(argv)

    symbol = args.symbol.strip().upper()
    if not symbol:
        print("error: --symbol must not be empty", file=sys.stderr)
        return EXIT_FAILED

    try:
        summary = run(symbol, dry_run=args.dry_run)
    except IndexingError as exc:
        # Includes the "reindex required" refusal, whose message says what to do.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except (VectorStoreError, EmbeddingError) as exc:
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


def run(symbol: str, *, dry_run: bool) -> IndexSummary:
    """Chunk and index, then commit the manifest in one transaction.

    Ordering is the whole design: the points go to Qdrant first, and the manifest row that
    marks each document complete is committed after. A failure between the two leaves an
    index that is missing a document, which reads as "not indexed" and is safe. The reverse
    order would leave a manifest claiming an index that is not there.
    """
    store = _store()
    try:
        embedder = get_embedder()
        session = SessionLocal()
        try:
            if dry_run:
                return index_company(
                    session, symbol=symbol, store=store, embedder=embedder, dry_run=True
                )
            with session.begin():
                return index_company(
                    session, symbol=symbol, store=store, embedder=embedder
                )
        finally:
            session.close()
    finally:
        store.close()


def _store():
    from app.vector_store import VectorStore

    return VectorStore(
        url=settings.qdrant_url, collection_name=settings.qdrant_collection
    )


if __name__ == "__main__":
    raise SystemExit(main())
