"""Chunking and embedding stored news into its own Qdrant collection, and searching it.

The same shape as `app/filing_index`, against a different corpus, and the differences are
the interesting part:

* **A separate collection.** News and filings have different lifetimes -- a filing is a
  permanent record, a news feed is a rolling window -- and keeping them apart means either
  one can be dropped and rebuilt without touching the other.
* **The article is the indexed unit.** A filing owns many documents, each with its own
  manifest row; a news article is one row with its chunks hanging off it, so its index
  identity is columns on that row rather than a second table.
* **Changed text replaces its chunks, it does not add to them.** A revised story is
  re-chunked, and the previous version's points are deleted by filter before the new ones
  are written -- otherwise a version that produced more chunks than its replacement would
  leave a tail behind that no query would ever mention.

PostgreSQL stays authoritative. Everything in Qdrant could be deleted and rebuilt from
`news_articles`, and that is the property `index_article` is written to preserve: the
identity columns are written **last**, after every point was acknowledged, so an interrupted
run leaves an article that reads as not indexed and a retry that converges.
"""

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from qdrant_client import models

from app.chunking import CHUNKING_VERSION, chunk_document
from app.embeddings import EMBEDDING_MODEL, Embedder, EmbeddingUnavailableError
from app.models import NewsArticle
from app.news import CATEGORIES
from app.vector_store import (
    CollectionMismatchError,
    CollectionMissingError,
    Point,
    ScoredPoint,
    VectorStore,
    VectorStoreUnavailableError,
)

# The namespace every news point id is derived in. Fixed forever, and distinct from the
# filings namespace: changing it would orphan every point already written.
POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://alphadesk.local/news")

# How many candidates to ask for per wanted result. Validation drops hits whose article has
# changed or is not completely indexed, so asking for exactly `top_k` would let those
# consume the whole result set.
_OVERFETCH_FACTOR = 4
_MAX_QUERY_LIMIT = 100

# What an article's heading says when it is embedded. The symbols are what a reader would
# recognise the story by; a macro release has none and is labelled by its category instead.
_HEADING_FOR_MACRO = "news | macro"

# Retrieval statuses. Each is a different answer -- "nothing matched" and "the index is down"
# call for different things to be said, and collapsing them would report one as the other.
STATUS_OK = "ok"
STATUS_NO_MATCHING_RESULTS = "no_matching_results"
STATUS_NOTHING_INDEXED = "nothing_indexed"
STATUS_INDEX_UNAVAILABLE = "index_unavailable"
STATUS_MODEL_UNAVAILABLE = "model_unavailable"

# What happened to one article during an indexing run.
OUTCOME_INDEXED = "indexed"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_FAILED = "failed"


class NewsIndexingError(Exception):
    """Base class for every failure this module reports."""


@dataclass(frozen=True)
class ArticleOutcome:
    article_id: int
    status: str
    chunks: int
    reason: str | None = None


@dataclass(frozen=True)
class IndexSummary:
    collection_name: str
    collection_created: bool
    embedding_model: str
    chunking_version: str
    outcomes: tuple[ArticleOutcome, ...]
    elapsed_seconds: float

    @property
    def indexed(self) -> int:
        return sum(1 for item in self.outcomes if item.status == OUTCOME_INDEXED)

    @property
    def unchanged(self) -> int:
        return sum(1 for item in self.outcomes if item.status == OUTCOME_UNCHANGED)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.outcomes if item.status == OUTCOME_FAILED)

    @property
    def chunk_count(self) -> int:
        return sum(item.chunks for item in self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection_name,
            "collection_created": self.collection_created,
            "embedding_model": self.embedding_model,
            "chunking_version": self.chunking_version,
            "indexed": self.indexed,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "chunks": self.chunk_count,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }


@dataclass(frozen=True)
class RetrievedNews:
    """One passage, and the article it came from, in enough detail to cite it."""

    text: str
    similarity: float
    chunk_index: int
    article_id: int
    provider: str
    source: str
    title: str
    canonical_url: str
    symbols: tuple[str, ...]
    category: str
    published_at: datetime
    ingested_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            # A similarity, never a percentage. Cosine similarity is not a probability.
            "similarity": self.similarity,
            "chunk_index": self.chunk_index,
            "article": {
                "id": self.article_id,
                "provider": self.provider,
                "source": self.source,
                "title": self.title,
                "url": self.canonical_url,
                "symbols": list(self.symbols),
                "category": self.category,
                "published_at": self.published_at.isoformat(),
                "ingested_at": self.ingested_at.isoformat(),
            },
        }


@dataclass(frozen=True)
class SearchResult:
    status: str
    query: str
    returned: int
    passages: tuple[RetrievedNews, ...]
    reason: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "query": self.query,
            "returned": self.returned,
            "passages": [passage.as_dict() for passage in self.passages],
            "warnings": list(self.warnings),
        }


# --- indexing ---------------------------------------------------------------------------


def index_articles(
    session: Session,
    *,
    articles: Sequence[NewsArticle],
    store: VectorStore,
    embedder: Embedder,
    force: bool = False,
) -> IndexSummary:
    """Index every article given that is not already indexed at its current content.

    `force` re-embeds even unchanged articles, which is what a model change would need. It
    is not the default and nothing in this milestone calls it: re-embedding unchanged text
    is the cost this function exists to avoid.
    """
    started = time.monotonic()
    created = store.ensure_collection()

    outcomes: list[ArticleOutcome] = []
    for article in articles:
        outcomes.append(
            _index_article(
                session,
                article=article,
                store=store,
                embedder=embedder,
                force=force,
            )
        )

    return IndexSummary(
        collection_name=store.collection_name,
        collection_created=created,
        embedding_model=embedder.model_name,
        chunking_version=CHUNKING_VERSION,
        outcomes=tuple(outcomes),
        elapsed_seconds=time.monotonic() - started,
    )


def _index_article(
    session: Session,
    *,
    article: NewsArticle,
    store: VectorStore,
    embedder: Embedder,
    force: bool,
) -> ArticleOutcome:
    row = _row(session, article)
    if row is None:
        return ArticleOutcome(
            article_id=0,
            status=OUTCOME_FAILED,
            chunks=0,
            reason="the article is no longer stored, so it has nothing to index",
        )

    text = article.text.strip()
    if not text:
        # Stored, and deliberately not indexed. An article with no readable body is a fact
        # about what the publisher supplied, and indexing an empty body would put a point
        # in the collection that represents nothing.
        return ArticleOutcome(
            article_id=row.id,
            status=OUTCOME_FAILED,
            chunks=0,
            reason="the article has no readable text to index",
        )

    if not force and _already_indexed(row, article, embedder):
        return ArticleOutcome(
            article_id=row.id,
            status=OUTCOME_UNCHANGED,
            chunks=row.indexed_point_count or 0,
        )

    heading = _heading(article)
    try:
        chunks = chunk_document(
            text,
            # A news article has no filing form and no sections. The label carries what a
            # reader would recognise the story by instead.
            form_type="news",
            heading_label=heading,
            counter=embedder,
        )
        # Embedded in one batch, before anything is written, so a failure while embedding
        # cannot leave an article half-written. A provider that returns a long article
        # produces several chunks; the model's 512-token ceiling is what decides.
        vectors = embedder.embed_passages(chunk.embedding_input for chunk in chunks)
    except EmbeddingUnavailableError as exc:
        return ArticleOutcome(
            article_id=row.id, status=OUTCOME_FAILED, chunks=0, reason=str(exc)
        )

    # Delete the previous version's points before writing the new ones, so a shorter
    # replacement cannot leave the longer one's tail behind. This runs even for an article
    # that has never been indexed -- the filter matches nothing, and doing it
    # unconditionally means there is one code path rather than two.
    store.delete(query_filter=_article_filter(row.id))

    points = [
        Point(
            id=_point_id(row.id, index),
            vector=vector,
            payload=_payload(
                row=row,
                chunk=chunk,
                chunk_index=index,
                embedding_model=embedder.model_name,
            ),
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]
    store.upsert(points)

    # Last, and only after every point was acknowledged. Their absence is what marks an
    # article as not completely indexed, so writing them earlier would be the one
    # unrecoverable mistake.
    row.indexed_content_sha256 = article.content_sha256
    row.indexed_embedding_model = embedder.model_name
    row.indexed_chunking_version = CHUNKING_VERSION
    row.indexed_point_count = len(points)
    row.indexed_at = datetime.now(timezone.utc)
    session.flush()

    return ArticleOutcome(
        article_id=row.id, status=OUTCOME_INDEXED, chunks=len(points)
    )


def _already_indexed(
    row: NewsArticle, article: NewsArticle, embedder: Embedder
) -> bool:
    """True when the stored article is indexed at exactly this content and configuration.

    All three have to match. The content hash alone would call an index built by a different
    model current, and the model alone would call one built from text that has since changed
    current -- and both of those are indexes whose vectors no longer describe their text.

    This is the check that stops an unchanged article being embedded again, which is what
    makes a repeated ingestion cheap enough to run on a button.
    """
    if row.indexed_at is None:
        return False
    return (
        row.indexed_content_sha256 == article.content_sha256
        and row.indexed_embedding_model == embedder.model_name
        and row.indexed_chunking_version == CHUNKING_VERSION
    )


def _heading(article: NewsArticle) -> str:
    if not article.symbols:
        return _HEADING_FOR_MACRO
    return f"news | {', '.join(article.symbols)}"


def _row(session: Session, article: NewsArticle) -> NewsArticle | None:
    """The stored row for a normalized article, identified the way the table identifies it."""
    return session.scalars(
        select(NewsArticle).where(
            NewsArticle.provider == article.provider,
            NewsArticle.provider_article_id == article.provider_article_id,
        )
    ).first()


def _point_id(article_id: int, chunk_index: int) -> str:
    """A deterministic id, so a repeat run writes over its own points instead of beside
    them -- which is what makes a retry converge rather than duplicate."""
    return str(
        uuid.uuid5(
            POINT_NAMESPACE,
            f"{article_id}:{CHUNKING_VERSION}:{EMBEDDING_MODEL}:{chunk_index}",
        )
    )


def _article_filter(article_id: int) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(
                key="article_id", match=models.MatchValue(value=article_id)
            )
        ]
    )


def _payload(
    *,
    row: NewsArticle,
    chunk: Any,
    chunk_index: int,
    embedding_model: str,
) -> dict[str, Any]:
    """Everything a citation needs, carried on the point itself.

    `published_epoch` exists alongside the ISO string purely for filtering: Qdrant compares
    integers unambiguously, and a range filter over a formatted date would depend on the
    string parsing the same way on both sides. `symbols` is a list so Qdrant can match any
    of them, which is what makes a filter by one holding work.
    """
    return {
        "article_id": row.id,
        "provider": row.provider,
        "source": row.source,
        "category": row.category,
        "symbols": list(row.symbols or []),
        "published_at": row.published_at.isoformat(),
        "published_epoch": int(row.published_at.timestamp()),
        "canonical_url": row.canonical_url,
        "title": row.title,
        "content_sha256": row.content_sha256,
        "chunk_index": chunk_index,
        "chunking_version": chunk.chunking_version,
        # The embedder's own name, not the module constant: recording the constant here
        # would make every payload disagree with its row the moment the two differed, and
        # validation would drop every passage rather than none.
        "embedding_model": embedding_model,
        "token_count": chunk.token_count,
        "text": chunk.text,
    }


# --- retrieval --------------------------------------------------------------------------


def search_news(
    session: Session,
    *,
    query: str,
    top_k: int,
    store: VectorStore,
    embedder: Embedder,
    provider: str | None = None,
    category: str | None = None,
    symbols: Sequence[str] = (),
    published_after: datetime | None = None,
    published_before: datetime | None = None,
) -> SearchResult:
    """The `top_k` passages most similar to `query`, filtered inside Qdrant.

    The filter is applied by Qdrant before it chooses results, not afterwards here, so an
    excluded article cannot occupy one of the `top_k` places. Every hit is then validated
    against the stored article, because a point can outlive the text it was built from.
    """
    filters = _search_filter(
        provider=provider,
        category=category,
        symbols=symbols,
        published_after=published_after,
        published_before=published_before,
    )

    def unavailable(status: str, reason: str) -> SearchResult:
        return SearchResult(
            status=status, query=query, returned=0, passages=(), reason=reason
        )

    try:
        if not store.exists():
            return unavailable(
                STATUS_NOTHING_INDEXED,
                f"the {store.collection_name!r} collection does not exist, so no news has "
                "been indexed",
            )
        store.validate_collection()
    except (VectorStoreUnavailableError, CollectionMissingError, CollectionMismatchError) as exc:
        return unavailable(STATUS_INDEX_UNAVAILABLE, str(exc))

    try:
        vector = embedder.embed_query(query)
    except EmbeddingUnavailableError as exc:
        return unavailable(STATUS_MODEL_UNAVAILABLE, str(exc))

    limit = min(top_k * _OVERFETCH_FACTOR, _MAX_QUERY_LIMIT)
    try:
        hits = store.query(vector, query_filter=filters, limit=limit)
    except VectorStoreUnavailableError as exc:
        return unavailable(STATUS_INDEX_UNAVAILABLE, str(exc))

    warnings: list[str] = []
    validated, dropped = _validate(session, hits)
    passages = validated[:top_k]
    if dropped:
        warnings.append(
            f"{dropped} retrieved passage(s) were dropped because the article they came "
            "from has changed since it was indexed, or is not completely indexed. They are "
            "not shown, because their vectors may no longer describe their text."
        )
    if not passages:
        return SearchResult(
            status=STATUS_NO_MATCHING_RESULTS,
            query=query,
            returned=0,
            passages=(),
            reason="nothing in the stored news matched that query",
            warnings=tuple(warnings),
        )

    warnings.append(
        "These are passages retrieved by similarity. That they were returned does not mean "
        "they answer the question, and nothing here reads them."
    )
    if len(passages) < top_k:
        warnings.append(
            f"{len(passages)} passage(s) were returned where {top_k} were asked for."
        )

    return SearchResult(
        status=STATUS_OK,
        query=query,
        returned=len(passages),
        passages=tuple(passages),
        warnings=tuple(warnings),
    )


def _validate(
    session: Session, hits: Sequence[ScoredPoint]
) -> tuple[list[RetrievedNews], int]:
    """Keep only hits whose article is indexed at exactly what the payload claims.

    The payload is a claim; the row is the record. An article revised since it was indexed
    has a content hash that no longer matches its points, and those points are dropped --
    the passage's vector was built from text that is no longer what is stored.
    """
    article_ids = [hit.payload.get("article_id") for hit in hits]
    rows = _rows_by_id(session, [i for i in article_ids if isinstance(i, int)])

    passages: list[RetrievedNews] = []
    seen: set[str] = set()
    dropped = 0

    for hit in hits:
        if hit.id in seen:
            continue
        payload = hit.payload
        row = rows.get(payload.get("article_id"))
        if row is None or row.indexed_at is None or not _payload_current(payload, row):
            dropped += 1
            continue
        seen.add(hit.id)
        passages.append(_passage(hit, payload, row))

    return passages, dropped


def _payload_current(payload: Mapping[str, Any], row: NewsArticle) -> bool:
    return (
        payload.get("content_sha256") == row.indexed_content_sha256
        and payload.get("embedding_model") == row.indexed_embedding_model
        and payload.get("chunking_version") == row.indexed_chunking_version
    )


def _rows_by_id(session: Session, article_ids: Sequence[int]) -> dict[int, NewsArticle]:
    if not article_ids:
        return {}
    rows = session.scalars(
        select(NewsArticle).where(NewsArticle.id.in_(list(set(article_ids))))
    ).all()
    return {row.id: row for row in rows}


def _passage(
    hit: ScoredPoint, payload: Mapping[str, Any], row: NewsArticle
) -> RetrievedNews:
    return RetrievedNews(
        text=str(payload.get("text", "")),
        similarity=hit.score,
        chunk_index=int(payload.get("chunk_index", 0)),
        article_id=row.id,
        provider=row.provider,
        source=row.source,
        title=row.title,
        canonical_url=row.canonical_url,
        symbols=tuple(row.symbols or ()),
        category=row.category,
        published_at=row.published_at,
        ingested_at=row.ingested_at,
    )


def _search_filter(
    *,
    provider: str | None,
    category: str | None,
    symbols: Sequence[str],
    published_after: datetime | None,
    published_before: datetime | None,
) -> models.Filter | None:
    """The filter Qdrant applies before it chooses anything.

    Every clause is a fact stored on the point, so the filter never has to consult the
    database. A macro release carries an empty `symbols` list, and a symbol filter excludes
    it -- which is the correct behaviour and the reason the listing endpoint, not this one,
    is what serves "everything about the economy".
    """
    must: list[Any] = []

    if provider is not None:
        # The feed slug the payload carries, which is what the page's Source filter holds.
        # Matching on `source` here would be the publisher's name and would return nothing
        # for any slug, while still reporting the search as filtered.
        must.append(
            models.FieldCondition(key="provider", match=models.MatchValue(value=provider))
        )

    if category is not None:
        if category not in CATEGORIES:
            # Refused rather than ignored: a filter that silently did nothing would return
            # everything and look like it had worked.
            raise NewsIndexingError(
                f"unknown category {category!r}; expected one of {', '.join(CATEGORIES)}"
            )
        must.append(
            models.FieldCondition(key="category", match=models.MatchValue(value=category))
        )

    cleaned = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
    if cleaned:
        must.append(
            models.FieldCondition(key="symbols", match=models.MatchAny(any=cleaned))
        )

    if published_after is not None:
        must.append(
            models.FieldCondition(
                key="published_epoch",
                range=models.Range(gte=int(published_after.timestamp())),
            )
        )
    if published_before is not None:
        must.append(
            models.FieldCondition(
                key="published_epoch",
                range=models.Range(lte=int(published_before.timestamp())),
            )
        )

    return models.Filter(must=must) if must else None


__all__ = [
    "OUTCOME_FAILED",
    "OUTCOME_INDEXED",
    "OUTCOME_UNCHANGED",
    "POINT_NAMESPACE",
    "STATUS_INDEX_UNAVAILABLE",
    "STATUS_MODEL_UNAVAILABLE",
    "STATUS_NO_MATCHING_RESULTS",
    "STATUS_NOTHING_INDEXED",
    "STATUS_OK",
    "ArticleOutcome",
    "IndexSummary",
    "NewsIndexingError",
    "RetrievedNews",
    "SearchResult",
    "index_articles",
    "search_news",
]
