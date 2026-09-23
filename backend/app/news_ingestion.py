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

**A release page that was read once is not read again on every run.** The feed is always
re-read -- that is how a new entry is found -- but the release *behind* an entry already held
is a request to an agency's website, and repeating it for an article nothing has changed is
work with no result. `_keep_stored` decides per entry: a body is already stored, the feed's
own markers do not say the entry changed, and the page was read inside
`RELEASE_RECHECK_INTERVAL`. Anything else is read, and a page whose read fails leaves no
`release_checked_at`, so the next run tries it again rather than treating the failure as a
fresh copy.

**The symbols come from the portfolio, and the macro feeds do not.** Company news is read for
what is actually held, looked up from PostgreSQL rather than written down here. The Fed and
BLS feeds are read regardless of what anybody holds: a rate decision or a CPI print is not
about a ticker, and gating it on holdings is how a macro event goes missing.

No scheduler, and nothing here runs on its own. One call is one bounded run.
"""

import logging
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
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
from app.official_feeds import FEEDS, MIN_SUMMARY_CHARS, Feed, FeedEntry, OfficialFeedError
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

# How long a release page that was read once is trusted before it is read again.
#
# Reading it is a request to a public service, and an official release is a published
# document rather than a live one: the Fed's RSS entries carry no revision marker at all, so
# without an interval the only two choices are "read the page on every run" and "never read it
# again". A day is the bound -- long enough that pressing Refresh costs nothing, short enough
# that a correction is picked up the next day.
#
# A feed that *does* mark revisions is not waited on this long: an entry whose update marker
# or title has changed is read immediately, because the publisher has said it changed.
RELEASE_RECHECK_INTERVAL = timedelta(hours=24)

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
    the database; `unchanged` is what it did not. `releases_read` is how many release pages
    were actually retrieved on top of the feed itself -- the number that a repeat run should
    drive to zero for entries it already holds. `error` is a sentence fit to show, and never
    a credential or a traceback.
    """

    source: str
    status: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    indexed: int = 0
    failed_index: int = 0
    releases_read: int = 0
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
            "releases_read": self.releases_read,
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
                    now=began,
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
    now: datetime,
) -> SourceResult:
    source = f"official_{feed.key}"
    try:
        entries = fetch_feed(feed, limit=max_entries)
        stored = _stored_by_entry(session, source, entries)
        to_read, reused = _plan_releases(entries, stored, now)
        read = enrich_with_release_text(to_read, max_releases=max_releases)
    except OfficialFeedError as exc:
        logger.warning("feed %s could not be read: %s", feed.key, type(exc).__name__)
        return SourceResult(
            source=source,
            status=STATUS_FAILED,
            error=str(exc),
            last_success_at=_last_success(session, source),
        )

    articles, problems = _normalize(read, lambda entry: from_feed_entry(entry, feed))
    # The entries kept as stored are offered to storage as the article they already are,
    # rather than being rebuilt from the feed's own summary -- see `_stored_article`.
    articles.extend(_stored_article(row) for row in reused)

    return _store_and_index(
        session,
        store=store,
        embedder=embedder,
        source=source,
        articles=articles,
        problems=problems,
        fetched=len(entries),
        # Only an entry that came back with a body was actually read. `enrich_with_release_text`
        # reports a page it could not retrieve by handing the entry back as the feed published
        # it, so a missing `text` there means nothing was fetched -- and an entry whose own
        # summary was substantial enough was never fetched either.
        releases_read=frozenset(
            entry.entry_id for entry in read if entry.text is not None
        ),
    )


def _stored_by_entry(
    session: Session, source: str, entries: Sequence[FeedEntry]
) -> dict[str, NewsArticle]:
    """The stored rows for these entries, keyed the way the table identifies an article.

    One query for the whole feed rather than one per entry: ten entries asked about one at a
    time is ten round trips to answer a question a single `IN` answers.

    Keyed by `provider_article_id`, which for a feed entry is the entry's own id -- the
    fallback to the URL cannot apply, because `fetch_feed` refuses an entry that has no id.
    """
    ids = [entry.entry_id for entry in entries if entry.entry_id]
    if not ids:
        return {}
    rows = session.scalars(
        select(NewsArticle).where(
            NewsArticle.provider == source,
            NewsArticle.provider_article_id.in_(ids),
        )
    ).all()
    return {row.provider_article_id: row for row in rows}


def _plan_releases(
    entries: Sequence[FeedEntry], stored: Mapping[str, NewsArticle], now: datetime
) -> tuple[list[FeedEntry], list[NewsArticle]]:
    """Split a feed's entries into the release pages to read and the articles to keep as stored.

    An entry that has never been seen is offered to enrichment and its own rule decides:
    `MIN_SUMMARY_CHARS` is what says whether the feed's summary is worth indexing on its own.
    An entry that *has* been seen is decided here, because the answer depends on what is
    stored rather than on the feed alone.
    """
    to_read: list[FeedEntry] = []
    reused: list[NewsArticle] = []
    for entry in entries:
        row = stored.get(entry.entry_id)
        if row is not None and _keep_stored(entry, row, now):
            reused.append(row)
        else:
            to_read.append(entry)
    return to_read, reused


def _keep_stored(entry: FeedEntry, stored: NewsArticle, now: datetime) -> bool:
    """Whether the stored article is kept instead of this entry's release page being read.

    True only when there is a body to keep, the page was read once already, and neither the
    feed nor the clock says to look again.
    """
    if not stored.text.strip():
        # Stored with nothing readable in it -- an earlier read that came back empty. There
        # is nothing to reuse, so the entry goes back to enrichment and is read if its own
        # summary is thin.
        return False
    if stored.release_checked_at is None:
        # The page has never been read for this article, so this is the run that reads it.
        # NULL is also what a *failed* read leaves behind, which is what makes one retryable.
        return False
    if len(entry.summary) >= MIN_SUMMARY_CHARS:
        # The article's body came from a release page, and the feed's own text has since
        # grown to a length worth indexing. Keeping the stored body is the deliberate
        # choice: a release page is never traded for the feed's summary of it.
        return True
    if _entry_changed(entry, stored):
        return False
    return now - stored.release_checked_at < RELEASE_RECHECK_INTERVAL


def _entry_changed(entry: FeedEntry, stored: NewsArticle) -> bool:
    """Whether the feed itself says this entry is no longer what was stored.

    Two markers, and neither is a guess. An Atom feed's `updated` is the publisher saying the
    entry changed -- the Fed's RSS has no such field, and `provider_updated_at` is NULL for
    every one of its entries. A title that no longer matches is the entry being a different
    entry under an id that was reused. Nothing is inferred from the summary text, which is
    exactly the field that changes without the article changing.
    """
    if (
        entry.provider_updated_at is not None
        and entry.provider_updated_at != stored.provider_updated_at
    ):
        return True
    return not _same_title(entry, stored)


def _same_title(entry: FeedEntry, stored: NewsArticle) -> bool:
    """Compared the way the title was normalized when it was stored, so a difference in
    whitespace alone is not reported as a change."""
    return " ".join(entry.title.split()) == stored.title


def _stored_article(row: NewsArticle) -> NormalizedArticle:
    """A stored row re-offered to storage as the article it already is.

    Reusing the row rather than rebuilding it from the feed entry is what makes "unchanged"
    exact. The content hash is carried across, so `_upsert` cannot mistake a body that was
    read from a release page for the one-line summary the feed is carrying today, and the
    comparison is a comparison of what is actually stored.
    """
    return NormalizedArticle(
        provider=row.provider,
        provider_article_id=row.provider_article_id,
        source=row.source,
        canonical_url=row.canonical_url,
        title=row.title,
        text=row.text,
        symbols=tuple(row.symbols or ()),
        category=row.category,
        published_at=row.published_at,
        provider_updated_at=row.provider_updated_at,
        content_sha256=row.content_sha256,
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
    releases_read: Collection[str] = (),
) -> SourceResult:
    """Write one source's articles, then index them, in that order.

    Storage commits before indexing begins, so an article that could not be embedded is
    still an article -- stored, readable on the page, and indexed by the next run.

    `releases_read` names the articles whose release page was retrieved during this run, by
    `provider_article_id`. It is what stamps `release_checked_at`, the time the recheck
    interval is measured from.
    """
    new = updated = unchanged = 0

    try:
        # One transaction for this source alone. Another source failing cannot roll this
        # back, and this failing cannot roll another back.
        with _own_transaction(session):
            for article in articles:
                tally = _upsert(
                    session,
                    article,
                    release_read=article.provider_article_id in releases_read,
                )
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
        releases_read=sum(
            1
            for article in articles
            if article.provider_article_id in releases_read
        ),
        error=index_error,
        last_success_at=datetime.now(timezone.utc),
    )


def _upsert(
    session: Session, article: NormalizedArticle, *, release_read: bool = False
) -> str:
    """Insert or update one article. Returns "new", "updated" or "unchanged".

    The dedupe key is `(provider, provider_article_id)` -- the same one the table's unique
    constraint enforces, so a race could not produce a second row either. The content hash
    decides whether the stored copy changes; `ingested_at` is refreshed only when the words
    did, because it is the time this database last learned something new about the article,
    not the time it last looked at it.

    `release_read` is the other half of that distinction, and is why an unchanged article can
    still write a row: `release_checked_at` records when the release page was last retrieved,
    which is true whether or not the page said anything new.
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
                release_checked_at=now if release_read else None,
            )
        )
        session.flush()
        return "new"

    if row.content_sha256 == article.content_sha256:
        # The provider may have moved `updated_at`, or re-sent the same story twice in one
        # feed read. Neither is a change to the article -- but a release page that was read
        # and said the same thing is still a page that was read, and the time it was read is
        # what the next run's recheck interval is measured from.
        if release_read:
            row.release_checked_at = now
            session.flush()
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
    if release_read:
        row.release_checked_at = now
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
