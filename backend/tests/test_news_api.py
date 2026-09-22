"""The news routes: what a listing returns, how a search reports itself, and the status codes.

Against a real PostgreSQL and a real in-process Qdrant, with the providers replaced. The
interesting cases are the ones where a caller has to be told something specific:

* a partial ingestion is a **200** with per-source results, because the run completed and
  naming which sources failed is the useful answer;
* a run in which *nothing* was read is a **503**;
* "nothing indexed" and "nothing matched" are different statuses on a search, because a page
  that showed the same thing for both would be lying about one of them;
* a listing is ordered by the publisher's publication time, never by when we stored it.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from fastapi import HTTPException
from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import news_ingestion
from app.alpaca_news import AlpacaNewsError, NewsItem
from app.models import Holding, NewsArticle, Portfolio
from app.news_api import get_news, get_news_search, post_news_ingest
from app.news_ingestion import IngestionInProgressError, IngestionSummary, SourceResult
from app.official_feeds import FEEDS_BY_KEY, FeedEntry
from app.portfolio_identity import DEMO_PORTFOLIO_NAME
from app.vector_store import VectorStore
from tests.search_doubles import StubEmbedder
from tests.testdb import test_engine

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
FED = FEEDS_BY_KEY["fed_monetary"]


def _unchanged(entries, **kwargs):
    """Stand in for the release-page fetch: hand the entries back as they arrived."""
    return entries


def alpaca_item(article_id: str, *, published_at: datetime, symbols=("MSFT",), content=""):
    return NewsItem(
        provider_article_id=article_id,
        headline=f"Story {article_id}",
        source="benzinga",
        url=f"https://www.benzinga.com/story/{article_id}",
        summary="A summary.",
        content=content or f"<p>Body of story {article_id} about semiconductors.</p>",
        symbols=tuple(symbols),
        published_at=published_at,
        provider_updated_at=None,
    )


def feed_entry(entry_id: str, *, published_at: datetime) -> FeedEntry:
    return FeedEntry(
        entry_id=entry_id,
        title=f"Release {entry_id}",
        url=f"https://www.federalreserve.gov/newsevents/pressreleases/{entry_id}.htm",
        summary="The Federal Reserve issued a statement on monetary policy and the "
        "economic outlook, stated at a length worth indexing on its own.",
        published_at=published_at,
        provider_updated_at=None,
        text=None,
    )


class NewsApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def setUp(self) -> None:
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)
        self.client = QdrantClient(location=":memory:")
        self.store = VectorStore(
            url="memory://", collection_name="test_news_api", client=self.client
        )
        self.embedder = StubEmbedder()

        # Registered here rather than in tearDown, and that is not a style preference.
        # unittest does NOT call tearDown when setUp itself raises -- so this setUp, which
        # seeds a portfolio and holdings through several statements that can fail, would
        # leave its transaction open on any failure. An open transaction holds the lock on
        # the portfolio's unique name, and the *next* test's setUp inserts that same name:
        # it blocks forever rather than failing, and every blocked test opens another
        # transaction behind the last. One setUp error therefore deadlocks the whole file.
        # addCleanup runs whatever happened, so the transaction cannot outlive the test.
        self.addCleanup(self.client.close)
        self.addCleanup(self.connection.close)
        self.addCleanup(self.transaction.rollback)
        self.addCleanup(self.session.close)

        # The routes close the store they are handed, which is right: `news_store()` builds
        # one per request and closing it releases the connection it opened. Here the store
        # is this test's own and its in-memory client *holds the collection*, so a route
        # closing it would empty the index between two calls in one test. The close is
        # stubbed and this test's own cleanup closes the client instead.
        closer = mock.patch.object(VectorStore, "close", lambda self: None)
        closer.start()
        self.addCleanup(closer.stop)

        self.portfolio = Portfolio(
            name=DEMO_PORTFOLIO_NAME, currency="USD", cash_balance=Decimal("1000.00")
        )
        self.session.add(self.portfolio)
        self.session.flush()
        for symbol in ("MSFT", "AAPL"):
            self.session.add(
                Holding(
                    portfolio_id=self.portfolio.id,
                    symbol=symbol,
                    quantity=Decimal("1"),
                    average_buy_price=Decimal("1.00"),
                )
            )
        self.session.flush()

    def ingest(self, *, items=(), entries=(), failing=()):
        """Run a real ingestion with canned provider responses.

        The providers and the embedding model are the only things replaced: the dedupe key
        is still the table's unique constraint, the collection is still a real Qdrant, and
        the chunker still runs.

        `app.news_api.get_embedder` is the patched name, and it has to be: the route looks
        the function up in its own module, so patching `app.embeddings`' copy would leave
        the route calling the real one. Patching a name that does not exist is worse still
        -- `mock.patch.object` raises `AttributeError` when the context is *entered*, which
        is inside setUp here, and unittest skips tearDown when setUp raises.
        """
        from app import news_ingestion as module

        def fetch_news(symbols, **kwargs):
            if "alpaca_news" in failing:
                raise AlpacaNewsError("alpaca is down")
            return list(items)

        def fetch_feed(feed, **kwargs):
            if f"official_{feed.key}" in failing:
                raise news_ingestion.OfficialFeedError("feed is down")
            return list(entries) if feed.key == FED.key else []

        with (
            mock.patch.object(module, "fetch_news", side_effect=fetch_news),
            mock.patch.object(module, "fetch_feed", side_effect=fetch_feed),
            # The third thing that leaves the process. A feed entry whose own summary is
            # short makes the ingestion fetch the linked release from the agency's site --
            # right in production, and a live HTTP request to a .gov domain from a test
            # unless it is replaced. The entries here are returned unchanged, so the
            # article is the feed's own summary. Release fetching has its own tests, with
            # a mock transport, in `test_official_feeds`.
            mock.patch.object(module, "enrich_with_release_text", side_effect=_unchanged),
            mock.patch("app.news_api.get_embedder", return_value=self.embedder),
        ):
            return post_news_ingest(session=self.session, store=self.store)


class ListingTests(NewsApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ingest(
            items=[
                alpaca_item("1", published_at=NOW - timedelta(hours=1)),
                alpaca_item("2", published_at=NOW - timedelta(days=3), symbols=("AAPL",)),
            ],
            entries=[feed_entry("a", published_at=NOW - timedelta(hours=2))],
        )

    def test_articles_come_back_newest_first_by_publication_time(self):
        listing = get_news(session=self.session)

        times = [article.published_at for article in listing.articles]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(listing.articles[0].title, "Story 1")

    def test_the_order_is_publication_time_not_the_order_we_stored_them(self):
        """An old release ingested today belongs where it was published, not at the top of
        the page."""
        old = datetime(2019, 3, 4, 9, 0, tzinfo=timezone.utc)
        self.ingest(items=[alpaca_item("old", published_at=old)])

        listing = get_news(session=self.session)

        self.assertEqual(listing.articles[-1].title, "Story old")
        self.assertEqual(listing.articles[-1].published_at, old)
        # And the time we stored it is later, and reported separately.
        self.assertGreater(listing.articles[-1].ingested_at, old)

    def test_every_article_carries_both_times_and_an_excerpt(self):
        article = get_news(session=self.session).articles[0]

        self.assertIsNotNone(article.published_at)
        self.assertIsNotNone(article.ingested_at)
        self.assertIn("Story 1", article.title)
        self.assertTrue(article.excerpt)
        self.assertNotIn("<p>", article.excerpt)
        self.assertTrue(article.indexed)

    def test_the_listing_says_the_feed_is_delayed_and_does_not_claim_otherwise(self):
        listing = get_news(session=self.session)

        self.assertIn("delayed", listing.timeliness)
        self.assertNotIn("real-time", listing.timeliness.lower().replace("real-time entitlement", ""))

    def test_the_source_block_reports_every_source_whether_or_not_it_has_articles(self):
        listing = get_news(session=self.session)

        sources = {status.source for status in listing.sources}
        self.assertEqual(
            sources,
            {"alpaca_news"} | {f"official_{feed.key}" for feed in news_ingestion.FEEDS},
        )
        by_source = {status.source: status.last_success_at for status in listing.sources}
        self.assertIsNotNone(by_source["alpaca_news"])

    def test_a_symbol_filter_returns_only_that_company(self):
        listing = get_news(session=self.session, symbol="AAPL")

        self.assertEqual([a.title for a in listing.articles], ["Story 2"])

    def test_a_symbol_filter_is_whole_symbol_not_a_substring(self):
        """"A" must not match "AAPL": a filter that matched substrings would return most of
        the feed for a one-letter symbol."""
        self.assertEqual(get_news(session=self.session, symbol="A").articles, [])

    def test_a_category_filter_separates_companies_from_the_economy(self):
        macro = get_news(session=self.session, category="macro")

        self.assertEqual([a.category for a in macro.articles], ["macro"])
        self.assertEqual(macro.articles[0].symbols, [])

    def test_an_unknown_category_is_refused_rather_than_ignored(self):
        with self.assertRaises(HTTPException) as caught:
            get_news(session=self.session, category="sport")

        self.assertEqual(caught.exception.status_code, 422)

    def test_a_source_filter_selects_one_provider(self):
        listing = get_news(session=self.session, source="Federal Reserve")

        self.assertEqual([a.title for a in listing.articles], ["Release a"])

    def test_a_date_window_excludes_what_was_published_outside_it(self):
        listing = get_news(session=self.session, published_after=NOW - timedelta(days=1))

        self.assertEqual([a.title for a in listing.articles], ["Story 1", "Release a"])

    def test_paging_reports_the_total_not_only_the_page(self):
        listing = get_news(session=self.session, limit=1, offset=0)

        self.assertEqual(listing.limit, 1)
        self.assertEqual(len(listing.articles), 1)
        self.assertEqual(listing.total, 3)

        second = get_news(session=self.session, limit=1, offset=1)
        self.assertNotEqual(second.articles[0].id, listing.articles[0].id)

    def test_an_empty_feed_is_an_empty_list_not_an_error(self):
        self.session.execute(NewsArticle.__table__.delete())
        self.session.flush()

        listing = get_news(session=self.session)

        self.assertEqual(listing.total, 0)
        self.assertEqual(listing.articles, [])
        # And the sources are still reported, so the page can say when each last worked.
        self.assertEqual(len(listing.sources), 1 + len(news_ingestion.FEEDS))


class SearchEndpointTests(NewsApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ingest(
            items=[
                alpaca_item(
                    "1",
                    published_at=NOW,
                    content="<p>Microsoft raised its data centre spending forecast.</p>",
                )
            ],
            entries=[feed_entry("a", published_at=NOW - timedelta(hours=1))],
        )

    def search(self, query: str, **kwargs):
        """The endpoint, with the model replaced and nothing else."""
        with mock.patch("app.news_api.get_embedder", return_value=self.embedder):
            return get_news_search(
                session=self.session, store=self.store, q=query, **kwargs
            )

    def test_a_search_returns_passages_with_the_article_they_came_from(self):
        result = self.search("data centre spending")

        self.assertEqual(result.status, "ok")
        self.assertGreaterEqual(result.returned, 1)
        first = result.passages[0]
        self.assertIn("data centre", first.text)
        self.assertEqual(first.article.source, "benzinga")
        self.assertTrue(first.article.url.startswith("https://"))

    def test_a_search_carries_the_caveat_that_returning_is_not_answering(self):
        result = self.search("data centre spending")

        self.assertTrue(any("similarity" in warning for warning in result.warnings))

    def test_nothing_indexed_is_reported_as_its_own_status(self):
        empty = VectorStore(
            url="memory://", collection_name="never_indexed", client=self.client
        )

        with mock.patch("app.news_api.get_embedder", return_value=self.embedder):
            result = get_news_search(session=self.session, store=empty, q="anything")

        self.assertEqual(result.status, "nothing_indexed")
        self.assertEqual(result.returned, 0)

    def test_an_unavailable_model_is_reported_as_its_own_status(self):
        """Called directly rather than through `search`, which applies its own embedder
        patch -- an inner patch would shadow this one and the test would silently exercise
        a working model."""
        from tests.search_doubles import FailingEmbedder

        with mock.patch("app.news_api.get_embedder", return_value=FailingEmbedder()):
            result = get_news_search(
                session=self.session, store=self.store, q="data centre spending"
            )

        self.assertEqual(result.status, "model_unavailable")
        self.assertEqual(result.returned, 0)

    def test_a_blank_query_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self.search("   ")

        self.assertEqual(caught.exception.status_code, 422)

    def test_an_unknown_category_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self.search("anything", category="sport")

        self.assertEqual(caught.exception.status_code, 422)


class IngestEndpointTests(NewsApiTestCase):
    def test_a_successful_run_reports_what_each_source_did(self):
        response = self.ingest(
            items=[alpaca_item("1", published_at=NOW)],
            entries=[feed_entry("a", published_at=NOW)],
        )

        self.assertEqual(response.stored, 2)
        self.assertEqual(response.indexed, 2)
        self.assertEqual(response.failed_sources, [])
        self.assertEqual(response.symbols, ["AAPL", "MSFT"])
        by_source = {result.source: result for result in response.sources}
        self.assertEqual(by_source["alpaca_news"].new, 1)
        self.assertEqual(by_source["official_fed_monetary"].new, 1)

    def test_the_run_is_committed_and_not_left_in_the_request_transaction(self):
        """The regression this file could not otherwise see.

        Every other test here reads back through the same session it wrote with, and an
        uncommitted write is perfectly visible to the session that made it -- so they all
        pass whether or not the route ever commits. Against a real request the session is
        closed when the response is sent, and an uncommitted ingestion is rolled back
        entirely, leaving a summary that reads like success, an empty table, and an index
        full of points whose articles no longer exist.
        """
        with mock.patch.object(
            self.session, "commit", wraps=self.session.commit
        ) as commit:
            self.ingest(items=[alpaca_item("1", published_at=NOW)])

        commit.assert_called_once()

    def test_a_partial_failure_is_a_two_hundred_with_the_failing_source_named(self):
        response = self.ingest(
            items=[alpaca_item("1", published_at=NOW)],
            failing=("official_fed_monetary",),
        )

        self.assertEqual(response.stored, 1)
        self.assertEqual(response.failed_sources, ["official_fed_monetary"])
        failed = next(r for r in response.sources if r.source == "official_fed_monetary")
        self.assertEqual(failed.status, "failed")
        self.assertIn("down", failed.error)

    def test_only_a_run_where_nothing_was_read_is_a_five_hundred_and_three(self):
        every_source = ("alpaca_news",) + tuple(
            f"official_{feed.key}" for feed in news_ingestion.FEEDS
        )

        with self.assertRaises(HTTPException) as caught:
            self.ingest(failing=every_source)

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("unchanged", caught.exception.detail)

    def test_an_overlapping_run_is_a_four_oh_nine(self):
        with mock.patch(
            "app.news_api.ingest", side_effect=IngestionInProgressError("already running")
        ):
            with self.assertRaises(HTTPException) as caught:
                post_news_ingest(session=self.session, store=self.store)

        self.assertEqual(caught.exception.status_code, 409)

    def test_the_second_run_stores_nothing_new_and_says_so(self):
        items = [alpaca_item("1", published_at=NOW)]
        entries = [feed_entry("a", published_at=NOW)]
        self.ingest(items=items, entries=entries)

        response = self.ingest(items=items, entries=entries)

        self.assertEqual(response.stored, 0)
        self.assertEqual(response.indexed, 0)
        by_source = {result.source: result for result in response.sources}
        self.assertEqual(by_source["alpaca_news"].unchanged, 1)
        # And the source is still recorded as having succeeded, which is the whole reason
        # the receipt exists.
        self.assertIsNotNone(by_source["alpaca_news"].last_success_at)


class TimelinessTests(unittest.TestCase):
    def test_the_claim_does_not_call_the_feed_real_time(self):
        from app.news_api import TIMELINESS

        self.assertIn("delayed", TIMELINESS)
        self.assertNotIn("live market feed is", TIMELINESS)
        self.assertIn("not", TIMELINESS.lower())


if __name__ == "__main__":
    unittest.main()
