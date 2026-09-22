"""The news API: read the sources once, list what is stored, search it by meaning.

Three routes and one rule about what they claim. A listing is what PostgreSQL holds, with
the publisher's own publication time on every row. A search is what Qdrant returns, scored by
similarity and validated against the article it came from. Neither says the feed is live --
Alpaca documents that news is delayed without real-time entitlement, and a successful request
does not prove otherwise, so `timeliness` states the position once and the page repeats it.

Nothing here runs on its own. `POST /news/ingest` is the only thing that reads a provider, and
it is only ever called because a person pressed something.
"""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.embeddings import EmbeddingUnavailableError, get_embedder
from app.models import NewsArticle
from app.news import CATEGORIES, excerpt
from app.news_index import NewsIndexingError, search_news
from app.news_ingestion import (
    IngestionInProgressError,
    IngestionSummary,
    ingest,
    last_success_by_source,
)
from app.schemas import (
    NewsArticleOut,
    NewsIngestOut,
    NewsListOut,
    NewsSearchOut,
    NewsSearchPassageOut,
    NewsSourceStatusOut,
)
from app.vector_store import VectorStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/news", tags=["news"])

# What this build says about the freshness of the feed. Stated as a module constant because
# it is a claim about the integration, not about any one article, and copying it into the
# page's own copy would let the two drift apart.
TIMELINESS = (
    "News from Alpaca is provided by Benzinga and is delayed: without real-time entitlement "
    "Alpaca documents a window ending fifteen minutes before the request. Official Federal "
    "Reserve and BLS releases are read from their own public feeds. Nothing here is a live "
    "market feed, and nothing here is a recommendation."
)

# How many articles a listing returns when the caller does not say, and the ceiling on what
# it may ask for. A page shows a screenful; anything larger belongs to paging.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# How many passages a search returns by default, and its ceiling.
DEFAULT_TOP_K = 8
MAX_TOP_K = 25

ARTICLE_COLUMNS = (
    NewsArticle.id,
    NewsArticle.provider,
    NewsArticle.source,
    NewsArticle.category,
    NewsArticle.title,
    NewsArticle.text,
    NewsArticle.canonical_url,
    NewsArticle.symbols,
    NewsArticle.published_at,
    NewsArticle.provider_updated_at,
    NewsArticle.ingested_at,
    NewsArticle.indexed_at,
)


def news_store() -> VectorStore:
    """The news collection. A dependency so a test can hand in an in-memory Qdrant."""
    return VectorStore(
        url=settings.qdrant_url, collection_name=settings.qdrant_news_collection
    )


@router.post("/ingest", response_model=NewsIngestOut)
def post_news_ingest(
    session: Session = Depends(get_session),
    store: VectorStore = Depends(news_store),
) -> NewsIngestOut:
    """Read every source once and store what came back, then index what changed.

    The one route that reaches a provider, and the only one that writes. It is bounded on
    every axis -- article count, entries per feed, release pages, and the window searched --
    so pressing it twice costs a predictable amount and cannot become a crawl.

    **A partial failure is a 200.** One agency being down does not make the run a failure: the
    other sources stored what they read, and `sources` names which succeeded and which did
    not. A 503 is for the case where nothing at all could be read.
    """
    try:
        summary = ingest(session, store=store, embedder=get_embedder())
        # The ingestion writes into this session's transaction, one savepoint per source,
        # and **nothing is durable until this commits**. Without it every article is rolled
        # back when the request's session closes -- while the summary returned above still
        # reads like a success, and while the Qdrant writes, which are not transactional,
        # stay behind. That combination is the worst of both: a response saying 80 stored,
        # an empty table, and an index full of points that retrieval has to drop.
        session.commit()
    except IngestionInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EmbeddingUnavailableError as exc:
        # The embedding model could not be loaded. Articles may already be stored by the
        # time this is raised, so the message says where the failure was.
        raise HTTPException(
            status_code=503,
            detail=(
                f"{exc} The articles that were read may be stored; none of them are "
                "searchable until the model is available."
            ),
        ) from exc
    finally:
        store.close()

    if not summary.any_succeeded:
        raise HTTPException(
            status_code=503,
            detail=_nothing_read_detail(summary),
        )

    return _ingest_out(summary)


@router.get("", response_model=NewsListOut)
def get_news(
    session: Session = Depends(get_session),
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
    source: Annotated[str | None, Query(max_length=120)] = None,
    category: Annotated[str | None, Query(max_length=16)] = None,
    symbol: Annotated[str | None, Query(max_length=20)] = None,
    published_after: datetime | None = None,
    published_before: datetime | None = None,
) -> NewsListOut:
    """Stored articles, newest first by the publisher's own publication time.

    Ordered by `published_at` and never by `ingested_at`: a release published a month ago and
    read this morning belongs where it was published, not at the top of the page.

    A `symbol` filter matches the articles tagged with it. Macro releases carry no symbols and
    are therefore absent from a filtered listing -- deliberately, because they are not about
    that company -- and are found by filtering on `category=macro` or by not filtering at all.
    """
    conditions = _listing_conditions(
        source=source,
        category=category,
        symbol=symbol,
        published_after=published_after,
        published_before=published_before,
    )

    total = session.scalar(
        select(func.count()).select_from(NewsArticle).where(*conditions)
    )
    rows = session.execute(
        select(*ARTICLE_COLUMNS)
        .where(*conditions)
        .order_by(NewsArticle.published_at.desc(), NewsArticle.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return NewsListOut(
        total=int(total or 0),
        limit=limit,
        offset=offset,
        articles=[_article_out(row) for row in rows],
        sources=_source_status(session),
        timeliness=TIMELINESS,
    )


@router.get("/search", response_model=NewsSearchOut)
def get_news_search(
    session: Session = Depends(get_session),
    store: VectorStore = Depends(news_store),
    q: Annotated[str, Query(min_length=1, max_length=500)] = ...,
    top_k: Annotated[int, Query(ge=1, le=MAX_TOP_K)] = DEFAULT_TOP_K,
    category: Annotated[str | None, Query(max_length=16)] = None,
    symbol: Annotated[str | None, Query(max_length=20)] = None,
    published_after: datetime | None = None,
    published_before: datetime | None = None,
) -> NewsSearchOut:
    """The stored passages most similar to `q`, filtered before the search chooses.

    A similarity search, not a keyword match: it finds passages that mean the same thing as
    the query, which is also why it can return something that shares none of its words. The
    status says which of "nothing matched", "nothing is indexed" and "the index is down"
    happened, because those are three different things to be told.
    """
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail="q must not be blank")

    try:
        result = search_news(
            session,
            query=query,
            top_k=top_k,
            store=store,
            embedder=get_embedder(),
            category=category,
            symbols=[symbol] if symbol else [],
            published_after=published_after,
            published_before=published_before,
        )
    except NewsIndexingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        store.close()

    return NewsSearchOut(
        status=result.status,
        reason=result.reason,
        query=result.query,
        returned=result.returned,
        passages=[
            NewsSearchPassageOut(
                text=passage.text,
                similarity=passage.similarity,
                chunk_index=passage.chunk_index,
                article=NewsArticleOut(
                    id=passage.article_id,
                    provider=passage.provider,
                    source=passage.source,
                    category=passage.category,
                    title=passage.title,
                    excerpt=excerpt(passage.text),
                    url=passage.canonical_url,
                    symbols=list(passage.symbols),
                    published_at=passage.published_at,
                    provider_updated_at=None,
                    ingested_at=passage.ingested_at,
                    indexed=True,
                ),
            )
            for passage in result.passages
        ],
        warnings=list(result.warnings),
    )


# --- helpers ------------------------------------------------------------------------------


def _listing_conditions(
    *,
    source: str | None,
    category: str | None,
    symbol: str | None,
    published_after: datetime | None,
    published_before: datetime | None,
) -> list:
    conditions = []
    if source:
        conditions.append(NewsArticle.source == source.strip())
    if category:
        if category not in CATEGORIES:
            # Refused rather than ignored. A filter that quietly did nothing would return
            # everything and look like it had worked.
            raise HTTPException(
                status_code=422,
                detail=(
                    f"unknown category {category!r}; expected one of "
                    f"{', '.join(CATEGORIES)}"
                ),
            )
        conditions.append(NewsArticle.category == category)
    if symbol:
        # Containment against a JSONB list, which is what the GIN index is for. Matching is
        # on the whole upper-cased symbol, never a substring: "A" must not match "AAPL".
        conditions.append(
            NewsArticle.symbols.contains([symbol.strip().upper()])
        )
    if published_after is not None:
        conditions.append(NewsArticle.published_at >= published_after)
    if published_before is not None:
        conditions.append(NewsArticle.published_at <= published_before)
    return conditions


def _article_out(row) -> NewsArticleOut:  # noqa: ANN001 - a SQLAlchemy Row
    (
        article_id,
        provider,
        source,
        category,
        title,
        text,
        url,
        symbols,
        published_at,
        provider_updated_at,
        ingested_at,
        indexed_at,
    ) = row
    return NewsArticleOut(
        id=article_id,
        provider=provider,
        source=source,
        category=category,
        title=title,
        excerpt=excerpt(text),
        url=url,
        symbols=list(symbols or []),
        published_at=published_at,
        provider_updated_at=provider_updated_at,
        ingested_at=ingested_at,
        indexed=indexed_at is not None,
    )


def _source_status(session: Session) -> list[NewsSourceStatusOut]:
    """Every source's last successful read, whether or not it has any articles stored."""
    return [
        NewsSourceStatusOut(source=source, last_success_at=when)
        for source, when in sorted(last_success_by_source(session).items())
    ]


def _ingest_out(summary: IngestionSummary) -> NewsIngestOut:
    return NewsIngestOut(
        started_at=summary.started_at,
        completed_at=summary.completed_at,
        symbols=list(summary.symbols),
        stored=summary.stored,
        indexed=summary.indexed,
        failed_sources=list(summary.failed_sources),
        sources=[result.as_dict() for result in summary.results],
    )


def _nothing_read_detail(summary: IngestionSummary) -> str:
    reasons = "; ".join(
        f"{result.source}: {result.error}" for result in summary.results if result.error
    )
    return (
        "No news source could be read, so nothing was stored. The stored articles are "
        f"unchanged. {reasons}" if reasons else
        "No news source could be read, so nothing was stored."
    )


__all__ = ["router"]
