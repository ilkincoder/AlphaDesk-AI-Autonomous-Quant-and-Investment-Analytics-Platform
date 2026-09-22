"""One bounded news ingestion: read every source, store what came back, index what changed.

**Sources are independent, and that is the central design decision.** Each is fetched on its
own, sanitized on its own, and written in its own transaction. An agency that is down, a
feed that has gone malformed, a credential that has stopped working -- each of those is
recorded against the source that failed, and the others still store what they fetched.
Nothing here deletes an article, so a failed run cannot lose news that is already held; the
worst it can do is leave it as old as it was.

**Storing and indexing are separate steps, in that order.** Every article is committed to
PostgreSQL before a single vector is written, so an embedding failure -- a model that cannot
load, a collection that will not accept the vectors -- leaves articles that are stored,
readable, and marked as not indexed. The next run indexes exactly those and skips the rest,
because an article whose content hash and configuration already match is not embedded again.

**The symbols come from the portfolio, and the macro feeds do not.** Company news is read for
what is actually held, looked up from PostgreSQL rather than written down here. The Fed and
BLS feeds are read regardless of what anybody holds: a rate decision or a CPI print is not
about a ticker, and gating it on holdings is how a macro event goes missing.

No scheduler, and nothing here runs on its own. One call is one bounded run.
"""

import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alpaca_news import AlpacaNewsError, NewsItem, fetch_news
from app.embeddings import Embedder, EmbeddingUnavailableError
from app.models import Holding, NewsArticle, NewsIngestionRun
from app.news import NewsArticle as NormalizedArticle
from app.news import from_alpaca, from_feed_entry
from app.news_index import OUTCOME_FAILED, OUTCOME_INDEXED, OUTCOME_UNCHANGED
from app.news_index import index_articles
from app.official_feeds import FEEDS, Feed, OfficialFeedError
from app.official_feeds import enrich_with_release_text, fetch_feed
from app.portfolio_identity import find as find_portfolio
from app.vector_store import VectorStore, VectorStoreError

logger = logging.getLogger(__name__)

# The source slug for company news, and the one used for the official feeds.
ALPACA_SOURCE = "alpaca_news"

# What one run reads, at most. Every bound here is a deliberate ceiling on a button press:
# the point of an explicit action is that a person can predict what it costs.
DEFAULT_MAX_ARTICLES = 50
DEFAULT_MAX_FEED_ENTRIES = 10
DEFAULT_DAYS = 7

# How many release pages one run will fetch on top of the feeds themselves. The Fed's feed
# carries its headline as its description, so the release has to be read for there to be
# anything to index.
DEFAULT_MAX_RELEASES = 10

# Per source, in the result.
STATUS_OK = "ok"
STATUS_FAILED = "failed"


class NewsIngestionError(Exception):
    """Base class for every failure this module reports."""


class IngestionInProgressError(NewsIngestionError):
    """Another ingestion is already running in this process. Rejected, not queued."""


@dataclass(frozen=True)
class SourceResult:
    """What one source did, in the terms a person would ask about it.

    `fetched` is what the provider returned; `new` and `updated` are what that changed in
    the database; `unchanged` is what it did not. `error` is a sentence fit to show, and
    never a credential or a traceback.
    """

    source: str
    status: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    indexed: int = 0
    failed_index: int = 0
    error: str | None = None
    last_success_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "fetched": self.fetched,
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "indexed": self.indexed,
            "failed_index": self.failed_index,
            "error": self.error,
            "last_success_at": (
                None if self.last_success_at is None else self.last_success_at.isoformat()
            ),
        }


@dataclass(frozen=True)
class IngestionSummary:
    started_at: datetime
    completed_at: datetime
    symbols: tuple[str, ...]
    results: tuple[SourceResult, ...] = field(default_factory=tuple)

    @property
    def stored(self) -> int:
        return sum(result.new + result.updated for result in self.results)

    @property
    def indexed(self) -> int:
        return sum(result.indexed for result in self.results)

    @property
    def failed_sources(self) -> tuple[str, ...]:
        return tuple(result.source for result in self.results if not result.ok)

    @property
    def any_succeeded(self) -> bool:
        return any(result.ok for result in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "symbols": list(self.symbols),
            "stored": self.stored,
            "indexed": self.indexed,
            "failed_sources": list(self.failed_sources),
            "sources": [result.as_dict() for result in self.results],
        }


# One ingestion at a time per process. The endpoint runs as a sync `def`, so a second
# request arrives on another thread of the threadpool and this is what stops it piling on.
_INGESTION_LOCK = threading.Lock()


@contextmanager
def _exclusive() -> Iterator[None]:
    if not _INGESTION_LOCK.acquire(blocking=False):
        raise IngestionInProgressError(
            "A news ingestion is already running. Nothing was started, and the stored "
            "articles are unchanged."
        )
    try:
        yield
    finally:
        _INGESTION_LOCK.release()


def portfolio_symbols(session: Session) -> tuple[str, ...]:
    """The symbols currently held, from the database.

    Never a constant, and never the symbols this application was first built around: a
    portfolio changes, and news about a company nobody holds is not what this is for.
    """
    portfolio = find_portfolio(session)
    if portfolio is None:
        return ()
    rows = session.scalars(
        select(Holding.symbol).where(Holding.portfolio_id == portfolio.id)
    ).all()
    return tuple(sorted({symbol.strip().upper() for symbol in rows if symbol.strip()}))


def ingest(
    session: Session,
    *,
    store: VectorStore,
    embedder: Embedder,
    symbols: Sequence[str] | None = None,
    max_articles: int = DEFAULT_MAX_ARTICLES,
    max_feed_entries: int = DEFAULT_MAX_FEED_ENTRIES,
    max_releases: int = DEFAULT_MAX_RELEASES,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
) -> IngestionSummary:
    """Read every source once, store what is new, and index what changed.

    `symbols` defaults to the portfolio's holdings. The official feeds are read either way.
    """
    began = now if now is not None else datetime.now(timezone.utc)
    resolved_symbols = (
        tuple(symbols) if symbols is not None else portfolio_symbols(session)
    )

    with _exclusive():
        results: list[SourceResult] = []

        results.append(
            _read_company_news(
                session,
                store=store,
                embedder=embedder,
                symbols=resolved_symbols,
                max_articles=max_articles,
                days=days,
                now=began,
            )
        )

        for feed in FEEDS:
            results.append(
                _read_feed(
                    session,
                    store=store,
                    embedder=embedder,
                    feed=feed,
                    max_entries=max_feed_entries,
                    max_releases=max_releases,
                )
            )

        completed = datetime.now(timezone.utc)
        summary = IngestionSummary(
            started_at=began,
            completed_at=completed,
            symbols=resolved_symbols,
            results=tuple(results),
        )
        _record_run(session, summary)
        return summary


# --- company news -----------------------------------------------------------------------


def _read_company_news(
    session: Session,
    *,
    store: VectorStore,
    embedder: Embedder,
    symbols: Sequence[str],
    max_articles: int,
    days: int,
    now: datetime,
) -> SourceResult:
    if not symbols:
        # Not a failure, and not nothing: there is no portfolio to ask about, so no request
        # is made. Saying "ok, fetched 0" would claim a read that did not happen.
        return SourceResult(
            source=ALPACA_SOURCE,
            status=STATUS_OK,
            last_success_at=_last_success(session, ALPACA_SOURCE),
        )

    try:
        items = fetch_news(list(symbols), days=days, limit=max_articles, now=now)
    except AlpacaNewsError as exc:
        logger.warning("company news could not be read: %s", type(exc).__name__)
        return SourceResult(
            source=ALPACA_SOURCE,
            status=STATUS_FAILED,
            error=str(exc),
            last_success_at=_last_success(session, ALPACA_SOURCE),
        )

    articles, problems = _normalize(items, from_alpaca)
    return _store_and_index(
        session,
        store=store,
        embedder=embedder,
        source=ALPACA_SOURCE,
        articles=articles,
        problems=problems,
        fetched=len(items),
    )


# --- official feeds ---------------------------------------------------------------------


def _read_feed(
    session: Session,
    *,
    store: VectorStore,
    embedder: Embedder,
    feed: Feed,
    max_entries: int,
    max_releases: int,
) -> SourceResult:
    source = f"official_{feed.key}"
    try:
        entries = fetch_feed(feed, limit=max_entries)
        entries = enrich_with_release_text(entries, max_releases=max_releases)
    except OfficialFeedError as exc:
        logger.warning("feed %s could not be read: %s", feed.key, type(exc).__name__)
        return SourceResult(
            source=source,
            status=STATUS_FAILED,
            error=str(exc),
            last_success_at=_last_success(session, source),
        )

    articles, problems = _normalize(
        entries, lambda entry: from_feed_entry(entry, feed)
    )
    return _store_and_index(
        session,
        store=store,
        embedder=embedder,
        source=source,
        articles=articles,
        problems=problems,
        fetched=len(entries),
    )


# --- storage ----------------------------------------------------------------------------


def _normalize(
    raw: Sequence[Any], convert: Any
) -> tuple[list[NormalizedArticle], list[str]]:
    """Normalize every item, and say which could not be read.

    One malformed article is not a reason to lose the others: it is counted and reported
    against its source, and the rest are stored. A provider that starts sending something
    unreadable shows up as a run that stored less, with the reason attached.
    """
    articles: list[NormalizedArticle] = []
    problems: list[str] = []
    for item in raw:
        try:
            articles.append(convert(item))
        except (ValueError, TypeError, KeyError) as exc:
            problems.append(f"{type(exc).__name__}: {exc}")
    return articles, problems


def _store_and_index(
    session: Session,
    *,
    store: VectorStore,
    embedder: Embedder,
    source: str,
    articles: Sequence[NormalizedArticle],
    problems: Sequence[str],
    fetched: int,
) -> SourceResult:
    """Write one source's articles, then index them, in that order.

    Storage commits before indexing begins, so an article that could not be embedded is
    still an article -- stored, readable on the page, and indexed by the next run.
    """
    new = updated = unchanged = 0

    try:
        # One transaction for this source alone. Another source failing cannot roll this
        # back, and this failing cannot roll another back.
        with _own_transaction(session):
            for article in articles:
                tally = _upsert(session, article)
                if tally == "new":
                    new += 1
                elif tally == "updated":
                    updated += 1
                else:
                    unchanged += 1
    except Exception as exc:  # noqa: BLE001 - reported, never raised past this point
        logger.exception("news from %s could not be stored", source)
        return SourceResult(
            source=source,
            status=STATUS_FAILED,
            fetched=fetched,
            error=(
                f"the articles were fetched but could not be stored "
                f"({type(exc).__name__}); nothing from this source was written"
            ),
            last_success_at=_last_success(session, source),
        )

    # Everything just stored, re-offered to the indexer. It decides per article whether
    # that means anything: an article already indexed at this content and configuration is
    # counted as unchanged and never embedded again.
    indexed = failed_index = 0
    index_error: str | None = None
    try:
        summary = index_articles(session, articles=articles, store=store, embedder=embedder)
        indexed = summary.indexed
        failed_index = summary.failed
    except (VectorStoreError, EmbeddingUnavailableError) as exc:
        # The articles are stored and committed. Only the index failed, and a later run
        # will find exactly these articles un-indexed and try again.
        logger.warning("news from %s could not be indexed: %s", source, type(exc).__name__)
        index_error = (
            f"the articles were stored but not indexed ({type(exc).__name__}); a later "
            "run will index them"
        )

    if problems:
        # Reported on an otherwise successful source: these are articles the provider sent
        # that could not be read, not a reason to call the read a failure.
        note = f"{len(problems)} item(s) could not be normalized ({problems[0]})"
        index_error = note if index_error is None else f"{index_error}; {note}"

    return SourceResult(
        source=source,
        status=STATUS_OK,
        fetched=fetched,
        new=new,
        updated=updated,
        unchanged=unchanged,
        indexed=indexed,
        failed_index=failed_index + len(problems),
        error=index_error,
        last_success_at=datetime.now(timezone.utc),
    )


def _upsert(session: Session, article: NormalizedArticle) -> str:
    """Insert or update one article. Returns "new", "updated" or "unchanged".

    The dedupe key is `(provider, provider_article_id)` -- the same one the table's unique
    constraint enforces, so a race could not produce a second row either. The content hash
    decides whether the stored copy changes; `ingested_at` is refreshed only when the words
    did, because it is the time this database last learned something new about the article,
    not the time it last looked at it.
    """
    row = session.scalars(
        select(NewsArticle).where(
            NewsArticle.provider == article.provider,
            NewsArticle.provider_article_id == article.provider_article_id,
        )
    ).first()

    now = datetime.now(timezone.utc)
    if row is None:
        session.add(
            NewsArticle(
                provider=article.provider,
                provider_article_id=article.provider_article_id,
                source=article.source,
                canonical_url=article.canonical_url,
                title=article.title,
                text=article.text,
                symbols=list(article.symbols),
                category=article.category,
                published_at=article.published_at,
                provider_updated_at=article.provider_updated_at,
                ingested_at=now,
                content_sha256=article.content_sha256,
            )
        )
        session.flush()
        return "new"

    if row.content_sha256 == article.content_sha256:
        # The provider may have moved `updated_at`, or re-sent the same story twice in one
        # feed read. Neither is a change to the article.
        return "unchanged"

    row.source = article.source
    row.canonical_url = article.canonical_url
    row.title = article.title
    row.text = article.text
    row.symbols = list(article.symbols)
    row.category = article.category
    row.published_at = article.published_at
    row.provider_updated_at = article.provider_updated_at
    row.ingested_at = now
    row.content_sha256 = article.content_sha256
    # The old index describes text that is no longer stored. Clearing the identity now is
    # what makes the row read as "not indexed" until the replacement's points are written,
    # so a crash in between cannot leave a stale vector being served as current.
    row.indexed_content_sha256 = None
    row.indexed_embedding_model = None
    row.indexed_chunking_version = None
    row.indexed_point_count = None
    row.indexed_at = None
    session.flush()
    return "updated"


@contextmanager
def _own_transaction(session: Session) -> Iterator[None]:
    """A transaction of this module's own, even when the caller's session is already in one.

    A request-scoped session has not begun a transaction until it is first used, so
    `session.begin()` would normally be right -- but these tests, and any future caller that
    wraps a run in its own transaction, already have one open, and `begin()` on a session
    with a live transaction raises. `begin_nested` is a savepoint in that case and a real
    transaction otherwise, which is the behaviour wanted either way: a failure inside rolls
    back this source's writes and leaves the caller's transaction usable.
    """
    with session.begin_nested():
        yield


def _record_run(session: Session, summary: IngestionSummary) -> None:
    """The receipt. Written last, once every source has been tried.

    It is committed separately from the articles because its whole purpose is to record
    success -- including for a source whose read succeeded and changed nothing, which no
    article row could show.
    """
    with _own_transaction(session):
        session.add(
            NewsIngestionRun(
                started_at=summary.started_at,
                completed_at=summary.completed_at,
                sources=[result.source for result in summary.results],
                results={result.source: result.as_dict() for result in summary.results},
            )
        )
        session.flush()


def _last_success(session: Session, source: str) -> datetime | None:
    """When this source last completed a read successfully.

    Read from the receipts rather than from the articles, because a feed that was read and
    had nothing new to say leaves no trace in `news_articles` at all -- and reporting it as
    stale would be wrong about a source that is working perfectly.
    """
    return session.scalar(
        select(NewsIngestionRun.completed_at)
        .where(NewsIngestionRun.results[source]["status"].astext == STATUS_OK)
        .order_by(NewsIngestionRun.completed_at.desc())
        .limit(1)
    )


def last_success_by_source(session: Session) -> dict[str, datetime | None]:
    """Every source's last successful read, for the page to show."""
    sources = [ALPACA_SOURCE] + [f"official_{feed.key}" for feed in FEEDS]
    return {source: _last_success(session, source) for source in sources}


def recent_window(days: int = DEFAULT_DAYS, *, now: datetime | None = None) -> datetime:
    """The oldest publication time a listing should show by default."""
    began = now if now is not None else datetime.now(timezone.utc)
    return began - timedelta(days=days)


__all__ = [
    "ALPACA_SOURCE",
    "DEFAULT_DAYS",
    "DEFAULT_MAX_ARTICLES",
    "DEFAULT_MAX_FEED_ENTRIES",
    "IngestionInProgressError",
    "IngestionSummary",
    "NewsIngestionError",
    "SourceResult",
    "ingest",
    "last_success_by_source",
    "portfolio_symbols",
]
