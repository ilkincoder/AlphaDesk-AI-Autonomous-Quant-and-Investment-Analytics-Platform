"""Ingesting news: what gets stored, what gets embedded again, and what survives a failure.

Against a real PostgreSQL (migrated, rolled back per test) and a real in-process Qdrant, with
the *providers* replaced by canned responses. What is not replaced is anything this milestone
is about: the dedupe key is the table's own unique constraint, the collection is validated at
384 dimensions, and the embedder is a deterministic double rather than a mock.

The properties this file exists for:

* a repeated ingestion creates no second row and embeds nothing again;
* changed content replaces its chunks rather than adding to them;
* an indexing failure leaves an article stored and unindexed, and a retry indexes it without
  duplicating a single point;
* one source failing stores the others and deletes nothing;
* a source that succeeds with nothing new still records a success time.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import dataclasses
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from qdrant_client import QdrantClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import news_ingestion
from app.alpaca_news import AlpacaNewsError, NewsItem
from app.models import Holding, NewsArticle, NewsIngestionRun, Portfolio
from app.news import from_alpaca
from app.news_index import index_articles, search_news
from app.news_ingestion import ingest, last_success_by_source, portfolio_symbols
from app.official_feeds import FEEDS_BY_KEY, FeedEntry, OfficialFeedError
from app.portfolio_identity import DEMO_PORTFOLIO_NAME
from app.vector_store import VectorStore, VectorStoreError
from tests.search_doubles import StubEmbedder
from tests.testdb import test_engine

PUBLISHED = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
FED = FEEDS_BY_KEY["fed_monetary"]
CPI = FEEDS_BY_KEY["bls_cpi"]


class CountingEmbedder(StubEmbedder):
    """A stub embedder that says how much work it was asked to do.

    The count is the whole point of several tests below: "unchanged content is not embedded
    again" is not visible in the database or in Qdrant, only here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.passages_embedded = 0

    def embed_passages(self, texts):
        items = list(texts)
        self.passages_embedded += len(items)
        return super().embed_passages(items)


class FailingEmbedder(StubEmbedder):
    """An embedder whose passages cannot be produced, for the storage/indexing split."""

    def embed_passages(self, texts):
        from app.embeddings import EmbeddingUnavailableError

        raise EmbeddingUnavailableError("the model is unavailable in this test")


def alpaca_item(
    article_id: str = "61923311",
    *,
    headline: str = "Analyst Sees More Upside for Microsoft",
    content: str = "<p>A body about MSFT and data centres.</p>",
    symbols: tuple[str, ...] = ("MSFT",),
    published_at: datetime = PUBLISHED,
) -> NewsItem:
    return NewsItem(
        provider_article_id=article_id,
        headline=headline,
        source="benzinga",
        url=f"https://www.benzinga.com/story/{article_id}",
        summary="A summary.",
        content=content,
        symbols=symbols,
        published_at=published_at,
        provider_updated_at=published_at,
    )


def feed_entry(entry_id: str = "monetary20260916a", *, title: str = "FOMC statement") -> FeedEntry:
    return FeedEntry(
        entry_id=entry_id,
        title=title,
        url=f"https://www.federalreserve.gov/newsevents/pressreleases/{entry_id}.htm",
        summary="The Federal Reserve issued a statement about monetary policy and the "
        "economic outlook, which this summary states at a length that is worth indexing.",
        published_at=PUBLISHED,
        provider_updated_at=None,
        text=None,
    )


class IngestionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def setUp(self) -> None:
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)

        # Registered here rather than in tearDown, and that is not a style preference.
        # unittest does NOT call tearDown when setUp itself raises -- and this class exists
        # to be subclassed, so a subclass's setUp runs seeding *and* a whole ingestion after
        # this one. Any of that failing would leave the transaction below open, holding the
        # lock on the portfolio's unique name; the next test's setUp inserts that same name
        # and blocks forever rather than failing, and every test behind it opens another
        # transaction. One setUp error would deadlock the file.
        #
        # addCleanup runs whatever happened, so no transaction can outlive its test.
        # addCleanup is last-in-first-out, so these are registered in the reverse of the
        # order they must run: session, then rollback, then connection.
        self.addCleanup(self.connection.close)
        self.addCleanup(self.transaction.rollback)
        self.addCleanup(self.session.close)

        self.client = QdrantClient(location=":memory:")
        self.addCleanup(self.client.close)
        self.store = VectorStore(
            url="memory://", collection_name="test_news", client=self.client
        )
        self.embedder = CountingEmbedder()

    # --- seeding ---------------------------------------------------------------------

    def add_portfolio(self, *symbols: str) -> Portfolio:
        portfolio = Portfolio(
            name=DEMO_PORTFOLIO_NAME, currency="USD", cash_balance=Decimal("1000.00")
        )
        self.session.add(portfolio)
        self.session.flush()
        for symbol in symbols:
            self.session.add(
                Holding(
                    portfolio_id=portfolio.id,
                    symbol=symbol,
                    quantity=Decimal("1"),
                    average_buy_price=Decimal("1.00"),
                )
            )
        self.session.flush()
        return portfolio

    def articles(self) -> list[NewsArticle]:
        return list(self.session.scalars(select(NewsArticle)))

    def point_count(self) -> int:
        return self.store.count()

    # --- running ---------------------------------------------------------------------

    def run_ingest(self, **overrides):
        """One ingestion, with the providers replaced.

        `enrich_with_release_text` is replaced too, and that is not optional: it is the
        third thing in this path that leaves the process. A feed entry whose own summary is
        short makes the ingestion fetch the linked release from the agency's site, which is
        right in production and a live request to a .gov domain from a test. Leaving it in
        makes the suite depend on two government websites being up.
        """
        with mock.patch.object(
            news_ingestion,
            "enrich_with_release_text",
            side_effect=lambda entries, **kwargs: entries,
        ):
            return ingest(
                self.session,
                store=self.store,
                embedder=self.embedder,
                **overrides,
            )


class StorageTests(IngestionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_portfolio("MSFT", "AAPL")

    def test_every_provider_is_read_and_every_article_stored(self):
        with (
            mock.patch.object(
                news_ingestion, "fetch_news", return_value=[alpaca_item()]
            ),
            mock.patch.object(
                news_ingestion, "fetch_feed", return_value=[feed_entry()]
            ),
        ):
            summary = self.run_ingest()

        self.assertEqual(len(self.articles()), len(news_ingestion.FEEDS) * 1 + 1)
        self.assertEqual(summary.stored, len(self.articles()))
        self.assertEqual(summary.failed_sources, ())
        self.assertTrue(summary.any_succeeded)

    def test_the_symbols_come_from_the_portfolio_not_from_a_constant(self):
        seen: list[list[str]] = []

        def fake_fetch(symbols, **kwargs):
            seen.append(list(symbols))
            return []

        with (
            mock.patch.object(news_ingestion, "fetch_news", side_effect=fake_fetch),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            self.run_ingest()

        self.assertEqual(seen, [["AAPL", "MSFT"]])
        self.assertEqual(portfolio_symbols(self.session), ("AAPL", "MSFT"))

    def test_the_macro_feeds_are_read_whether_or_not_anything_is_held(self):
        """A rate decision is not about a ticker. Gating it on holdings is how a macro event
        goes missing."""
        self.session.execute(Holding.__table__.delete())
        self.session.flush()

        with (
            mock.patch.object(news_ingestion, "fetch_news", return_value=[]) as company,
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[feed_entry()]) as macro,
        ):
            self.run_ingest()

        company.assert_not_called()
        self.assertEqual(macro.call_count, len(news_ingestion.FEEDS))
        self.assertEqual(
            {article.category for article in self.articles()}, {"macro"}
        )

    def test_the_publication_time_stored_is_the_publishers(self):
        long_ago = datetime(2019, 3, 4, 9, 0, tzinfo=timezone.utc)
        with (
            mock.patch.object(
                news_ingestion, "fetch_news", return_value=[alpaca_item(published_at=long_ago)]
            ),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            self.run_ingest()

        (article,) = self.articles()
        self.assertEqual(article.published_at, long_ago)
        # And our own time is later than it, and separate.
        self.assertGreater(article.ingested_at, long_ago)

    def test_a_second_run_stores_no_second_row_and_embeds_nothing_again(self):
        with (
            mock.patch.object(news_ingestion, "fetch_news", return_value=[alpaca_item()]),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[feed_entry()]),
        ):
            self.run_ingest()
            stored = len(self.articles())
            points = self.point_count()
            embedded = self.embedder.passages_embedded
            self.assertGreater(embedded, 0)

            summary = self.run_ingest(now=PUBLISHED + timedelta(hours=1))

        self.assertEqual(len(self.articles()), stored)
        self.assertEqual(self.point_count(), points)
        self.assertEqual(
            self.embedder.passages_embedded,
            embedded,
            "unchanged articles were embedded a second time",
        )
        self.assertEqual(summary.stored, 0)
        self.assertEqual(sum(1 for r in summary.results if r.ok), len(summary.results))


class ChangeTests(IngestionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_portfolio("MSFT")

    def _run(self, item: NewsItem):
        with (
            mock.patch.object(news_ingestion, "fetch_news", return_value=[item]),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            return self.run_ingest()

    def test_a_revised_article_updates_its_row_rather_than_adding_one(self):
        self._run(alpaca_item(content="<p>The original wording.</p>"))
        summary = self._run(
            alpaca_item(content="<p>The corrected wording.</p>", headline="Corrected")
        )

        (article,) = self.articles()
        self.assertEqual(article.title, "Corrected")
        self.assertEqual(article.text, "The corrected wording.")
        self.assertEqual(summary.stored, 1)
        self.assertEqual(summary.results[0].updated, 1)
        self.assertEqual(summary.results[0].new, 0)

    def test_a_longer_replacement_leaves_no_chunk_of_the_old_version_behind(self):
        """The reason the previous points are deleted by filter rather than overwritten:
        a version that produced more chunks than its replacement would otherwise leave a
        tail that no query would ever mention."""
        long_body = "<p>" + " ".join(["sentence about MSFT"] * 400) + "</p>"
        self._run(alpaca_item(content=long_body))
        many = self.point_count()
        self.assertGreater(many, 1)

        self._run(alpaca_item(content="<p>Short.</p>"))

        self.assertEqual(self.point_count(), 1)
        self.assertLess(self.point_count(), many)

    def test_the_replacement_is_what_a_search_returns(self):
        self._run(alpaca_item(content="<p>Original text about semiconductors.</p>"))
        self._run(alpaca_item(content="<p>Replacement text about dividends.</p>"))

        result = search_news(
            self.session,
            query="dividends",
            top_k=5,
            store=self.store,
            embedder=self.embedder,
        )

        self.assertTrue(result.found)
        self.assertIn("Replacement", result.passages[0].text)
        self.assertNotIn("Original", result.passages[0].text)

    def test_a_revision_clears_the_index_identity_before_the_new_points_land(self):
        """A crash between the two must leave the article reading as not indexed, not as
        indexed at text that is no longer stored."""
        self._run(alpaca_item(content="<p>First.</p>"))
        (article,) = self.articles()
        self.assertIsNotNone(article.indexed_at)

        # A *expected* index failure -- an unreachable collection. An unexpected one is
        # deliberately left to raise, because a crash that hides itself behind "something
        # went wrong" is harder to fix than one that shows its trace.
        with mock.patch.object(
            news_ingestion,
            "index_articles",
            side_effect=VectorStoreError("the collection is unreachable"),
        ):
            summary = self._run(alpaca_item(content="<p>Second.</p>"))

        self.session.refresh(article)
        self.assertIsNone(article.indexed_at)
        self.assertIsNotNone(summary.results[0].error)
        self.assertEqual(summary.results[0].status, "ok")


class IndexingFailureTests(IngestionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_portfolio("MSFT")

    def _run(self, **kwargs):
        with (
            mock.patch.object(news_ingestion, "fetch_news", return_value=[alpaca_item()]),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            return self.run_ingest(**kwargs)

    def test_an_embedding_failure_leaves_the_article_stored_and_unindexed(self):
        self.embedder = FailingEmbedder()

        summary = self._run()

        (article,) = self.articles()
        self.assertEqual(article.text, "A body about MSFT and data centres.")
        self.assertIsNone(article.indexed_at)
        self.assertEqual(self.point_count(), 0)
        # The run itself succeeded: the read worked and the article is stored.
        self.assertEqual(summary.results[0].status, "ok")
        self.assertGreaterEqual(summary.results[0].failed_index, 1)

    def test_a_retry_indexes_exactly_what_the_failure_left_behind(self):
        self.embedder = FailingEmbedder()
        self._run()

        self.embedder = CountingEmbedder()
        self._run()
        embedded_once = self.embedder.passages_embedded

        (article,) = self.articles()
        self.assertIsNotNone(article.indexed_at)
        self.assertEqual(self.point_count(), article.indexed_point_count)

        # And a third run adds nothing: the retry converged rather than accumulating.
        self._run()
        self.assertEqual(self.embedder.passages_embedded, embedded_once)
        self.assertEqual(self.point_count(), article.indexed_point_count)

    def test_a_collection_that_cannot_be_reached_still_stores_the_articles(self):
        self.store = VectorStore(
            url="memory://",
            collection_name="test_news",
            client=_BrokenClient(),
        )

        summary = self._run()

        self.assertEqual(len(self.articles()), 1)
        self.assertEqual(summary.results[0].status, "ok")
        self.assertIsNotNone(summary.results[0].error)


class PartialFailureTests(IngestionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_portfolio("MSFT")

    def test_an_unreachable_source_stores_the_others_and_deletes_nothing(self):
        with (
            mock.patch.object(
                news_ingestion, "fetch_news", side_effect=AlpacaNewsError("no route")
            ),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[feed_entry()]),
        ):
            summary = self.run_ingest()

        stored = self.articles()
        self.assertEqual(len(stored), len(news_ingestion.FEEDS))
        self.assertEqual(summary.failed_sources, ("alpaca_news",))
        self.assertTrue(summary.any_succeeded)

        company = next(r for r in summary.results if r.source == "alpaca_news")
        self.assertEqual(company.status, "failed")
        self.assertIn("no route", company.error or "")

    def test_a_failed_run_does_not_remove_articles_that_were_already_held(self):
        with (
            mock.patch.object(news_ingestion, "fetch_news", return_value=[alpaca_item()]),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            self.run_ingest()
        before = len(self.articles())

        with (
            mock.patch.object(
                news_ingestion, "fetch_news", side_effect=AlpacaNewsError("down")
            ),
            mock.patch.object(
                news_ingestion, "fetch_feed", side_effect=OfficialFeedError("down")
            ),
        ):
            summary = self.run_ingest()

        self.assertEqual(len(self.articles()), before)
        self.assertFalse(summary.any_succeeded)
        self.assertEqual(len(summary.failed_sources), 1 + len(news_ingestion.FEEDS))

    def test_one_feed_failing_does_not_stop_another(self):
        def only_cpi(feed, **kwargs):
            if feed.key != CPI.key:
                raise OfficialFeedError("feed is down")
            return [feed_entry()]

        with (
            mock.patch.object(news_ingestion, "fetch_news", return_value=[]),
            mock.patch.object(news_ingestion, "fetch_feed", side_effect=only_cpi),
        ):
            summary = self.run_ingest()

        self.assertEqual([a.provider for a in self.articles()], ["official_bls_cpi"])
        self.assertIn("official_fed_monetary", summary.failed_sources)
        self.assertNotIn("official_bls_cpi", summary.failed_sources)

    def test_an_unreadable_article_is_counted_without_losing_the_readable_ones(self):
        """A provider that starts sending a field of the wrong shape is a bug on their side,
        and it should cost the article rather than the whole run."""
        with (
            mock.patch.object(
                news_ingestion,
                "fetch_news",
                return_value=[alpaca_item(), unreadable_item()],
            ),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            summary = self.run_ingest()

        self.assertEqual(len(self.articles()), 1)
        company = summary.results[0]
        self.assertEqual(company.status, "ok")
        self.assertGreaterEqual(company.failed_index, 1)
        self.assertIn("could not be normalized", company.error or "")


class ReceiptTests(IngestionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_portfolio("MSFT")

    def test_a_source_that_read_nothing_new_still_records_a_success(self):
        """The reason the receipt table exists: a feed read successfully that had nothing to
        say changes no article row, and deriving staleness from the articles would report a
        working source as dead."""
        with (
            mock.patch.object(news_ingestion, "fetch_news", return_value=[]),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            self.run_ingest()

        self.assertEqual(self.session.scalar(select(func.count()).select_from(NewsArticle)), 0)
        statuses = last_success_by_source(self.session)

        self.assertEqual(
            set(statuses),
            {"alpaca_news"} | {f"official_{feed.key}" for feed in news_ingestion.FEEDS},
        )
        for source, when in statuses.items():
            with self.subTest(source=source):
                self.assertIsNotNone(when)

    def test_a_failed_source_records_no_success(self):
        with (
            mock.patch.object(
                news_ingestion, "fetch_news", side_effect=AlpacaNewsError("down")
            ),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            self.run_ingest()

        self.assertIsNone(last_success_by_source(self.session)["alpaca_news"])

    def test_every_run_writes_exactly_one_receipt(self):
        with (
            mock.patch.object(news_ingestion, "fetch_news", return_value=[]),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            self.run_ingest()
            self.run_ingest()

        runs = self.session.scalars(select(NewsIngestionRun)).all()
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(runs[0].sources), 1 + len(news_ingestion.FEEDS))


class OverlapTests(IngestionTestCase):
    def test_a_second_ingestion_is_refused_rather_than_queued(self):
        self.assertTrue(news_ingestion._INGESTION_LOCK.acquire(blocking=False))
        self.addCleanup(news_ingestion._INGESTION_LOCK.release)

        with mock.patch.object(
            news_ingestion, "fetch_news", side_effect=AssertionError("read a provider")
        ):
            with self.assertRaises(news_ingestion.IngestionInProgressError):
                self.run_ingest()


class SearchTests(IngestionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_portfolio("MSFT")
        with (
            mock.patch.object(
                news_ingestion,
                "fetch_news",
                return_value=[
                    alpaca_item("1", headline="MSFT data centre spending", content="<p>Cloud and data centres.</p>"),
                    alpaca_item("2", headline="AAPL services revenue", content="<p>Services revenue grew.</p>", symbols=("AAPL",)),
                ],
            ),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[feed_entry()]),
        ):
            self.run_ingest()

    def search(self, query: str, **kwargs):
        return search_news(
            self.session,
            query=query,
            top_k=5,
            store=self.store,
            embedder=self.embedder,
            **kwargs,
        )

    def test_a_query_ranks_the_passage_that_matches_it_first(self):
        result = self.search("data centres")

        self.assertTrue(result.found)
        self.assertIn("data centres", result.passages[0].text)
        self.assertEqual(result.passages[0].source, "benzinga")

    def test_a_similarity_is_reported_for_every_passage(self):
        """A ranking, not a probability. What the page shows is the order and the number,
        and the number is a cosine similarity rather than a percentage of anything."""
        result = self.search("data centres")

        scores = [passage.similarity for passage in result.passages]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(-1.0 <= score <= 1.0 for score in scores))

    def test_a_symbol_filter_excludes_other_companies(self):
        result = self.search("revenue", symbols=["AAPL"])

        self.assertEqual(
            [passage.symbols for passage in result.passages], [("AAPL",)]
        )

    def test_a_macro_release_is_findable_with_no_symbol_filter(self):
        result = self.search("monetary policy economic outlook")

        self.assertEqual(result.passages[0].category, "macro")
        self.assertEqual(result.passages[0].symbols, ())

    def test_a_category_filter_selects_the_corpus_asked_for(self):
        """Every passage returned is from the category asked for -- the filter is applied
        by Qdrant before it chooses, not afterwards in Python."""
        macro = self.search("revenue", category="macro").passages
        company = self.search("revenue", category="company").passages

        self.assertTrue(macro and company)
        self.assertEqual({passage.category for passage in macro}, {"macro"})
        self.assertEqual({passage.category for passage in company}, {"company"})

    def test_an_unknown_category_is_refused_rather_than_ignored(self):
        from app.news_index import NewsIndexingError

        with self.assertRaises(NewsIndexingError):
            self.search("revenue", category="sport")

    def test_a_date_filter_excludes_what_was_published_outside_it(self):
        after = self.search("data", published_after=PUBLISHED + timedelta(days=1))
        before = self.search("data", published_before=PUBLISHED - timedelta(days=1))

        self.assertFalse(after.found)
        self.assertFalse(before.found)
        self.assertTrue(self.search("data", published_after=PUBLISHED - timedelta(days=1)).found)

    def test_an_article_revised_since_it_was_indexed_is_not_served(self):
        """The payload is a claim and the row is the record. A point whose vector was built
        from text that has since changed is dropped rather than shown -- and the others are
        still returned, so one revision does not empty the result."""
        article = self.session.scalars(
            select(NewsArticle).where(NewsArticle.provider_article_id == "1")
        ).one()
        article.indexed_content_sha256 = "a hash that is not what the points carry"
        self.session.flush()

        result = self.search("data centres")

        self.assertNotIn(
            "data centres", [passage.text for passage in result.passages]
        )
        self.assertTrue(any("changed since it was indexed" in w for w in result.warnings))

    def test_nothing_indexed_is_its_own_status_not_an_empty_result(self):
        """An index that is not there and a search that matched nothing are different
        answers, and a page that showed the same thing for both would be lying about one."""
        empty = VectorStore(
            url="memory://", collection_name="never_indexed", client=self.client
        )

        result = search_news(
            self.session,
            query="anything",
            top_k=5,
            store=empty,
            embedder=self.embedder,
        )

        self.assertEqual(result.status, "nothing_indexed")
        self.assertFalse(result.found)


class VectorStoreDeleteTests(IngestionTestCase):
    def test_deleting_by_filter_removes_only_what_the_filter_matches(self):
        self.add_portfolio("MSFT")
        with (
            mock.patch.object(
                news_ingestion,
                "fetch_news",
                return_value=[alpaca_item("1"), alpaca_item("2", symbols=("AAPL",))],
            ),
            mock.patch.object(news_ingestion, "fetch_feed", return_value=[]),
        ):
            self.run_ingest()
        self.assertEqual(self.point_count(), 2)

        (first,) = self.session.scalars(
            select(NewsArticle).where(NewsArticle.provider_article_id == "1")
        ).all()
        from app.news_index import _article_filter

        self.store.delete(query_filter=_article_filter(first.id))

        self.assertEqual(self.point_count(), 1)


class _BrokenClient:
    """A Qdrant client that cannot answer anything, for the index-unavailable path."""

    def collection_exists(self, name):  # noqa: ANN001
        raise ConnectionError("no route to qdrant")

    def create_collection(self, **kwargs):
        raise ConnectionError("no route to qdrant")

    def close(self):
        pass


def unreadable_item() -> NewsItem:
    """An article whose headline is not text.

    The clients validate their own payloads, so this cannot come from today's providers --
    which is exactly why the ingestion still has to survive it: a provider that revises its
    schema is a thing that happens, and it must cost one article rather than the run.
    """
    return dataclasses.replace(alpaca_item("broken"), headline=12345)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
