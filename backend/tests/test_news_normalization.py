"""Turning three sources' output into one kind of article, and the four things that matter.

Pure: no database, no network, no model. What is checked here is what `app.news` decides --
markup removed, identity chosen, category derived, and the publication time left exactly as
the publisher stated it.

The timestamp cases carry the most weight. `published_at` is the one field where a quiet
mistake would be invisible: an article stamped with the time we read it looks like breaking
news, and nothing downstream could tell that it was not.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.alpaca_news import NewsItem
from app.news import (
    CATEGORY_COMPANY,
    CATEGORY_MACRO,
    EXCERPT_CHARS,
    as_document,
    excerpt,
    from_alpaca,
    from_feed_entry,
)
from app.official_feeds import FEEDS_BY_KEY, FeedEntry

PUBLISHED = datetime(2026, 9, 22, 14, 39, 5, tzinfo=timezone.utc)
UPDATED = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
FED = FEEDS_BY_KEY["fed_monetary"]


def alpaca(**overrides) -> NewsItem:
    values = {
        "provider_article_id": "61923311",
        "headline": "Analyst Sees More Upside for Microsoft Stock",
        "source": "benzinga",
        "url": "https://www.benzinga.com/trading-ideas/movers/26/09/61923311",
        "summary": "A summary.",
        "content": "<p>A body with <b>markup</b> in it.</p>",
        "symbols": ("MSFT",),
        "published_at": PUBLISHED,
        "provider_updated_at": UPDATED,
    }
    values.update(overrides)
    return NewsItem(**values)


def feed_entry(**overrides) -> FeedEntry:
    values = {
        "entry_id": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm",
        "title": "Federal Reserve issues FOMC statement",
        "url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm",
        "summary": "Federal Reserve issues FOMC statement",
        "published_at": PUBLISHED,
        "provider_updated_at": None,
        "text": None,
    }
    values.update(overrides)
    return FeedEntry(**values)


class AlpacaNormalizationTests(unittest.TestCase):
    def test_markup_is_stripped_from_the_body(self):
        article = from_alpaca(alpaca())

        self.assertEqual(article.text, "A body with markup in it.")
        self.assertNotIn("<", article.text)

    def test_a_script_in_the_body_is_removed_rather_than_escaped(self):
        article = from_alpaca(
            alpaca(content="<p>Text.</p><script>alert('x')</script>")
        )

        self.assertEqual(article.text, "Text.")
        self.assertNotIn("alert", article.text)

    def test_the_summary_is_the_body_when_there_is_no_content(self):
        article = from_alpaca(alpaca(content=""))

        self.assertEqual(article.text, "A summary.")

    def test_the_providers_identity_is_kept_as_given(self):
        article = from_alpaca(alpaca())

        self.assertEqual(article.provider, "alpaca_news")
        self.assertEqual(article.provider_article_id, "61923311")
        self.assertEqual(article.source, "benzinga")

    def test_a_story_with_symbols_is_company_news(self):
        article = from_alpaca(alpaca(symbols=("MSFT", "AAPL")))

        self.assertEqual(article.category, CATEGORY_COMPANY)
        self.assertEqual(article.symbols, ("AAPL", "MSFT"))
        self.assertFalse(article.is_macro)

    def test_a_story_with_no_symbols_is_not_claimed_to_be_about_a_company(self):
        article = from_alpaca(alpaca(symbols=()))

        self.assertEqual(article.category, CATEGORY_MACRO)
        self.assertEqual(article.symbols, ())

    def test_publication_and_update_times_are_carried_through_untouched(self):
        article = from_alpaca(alpaca())

        self.assertEqual(article.published_at, PUBLISHED)
        self.assertEqual(article.provider_updated_at, UPDATED)

    def test_a_missing_source_falls_back_to_the_provider_name(self):
        article = from_alpaca(alpaca(source=""))

        self.assertEqual(article.source, "alpaca_news")


class FeedNormalizationTests(unittest.TestCase):
    def test_a_release_is_macro_by_construction(self):
        article = from_feed_entry(feed_entry(), FED)

        self.assertEqual(article.category, CATEGORY_MACRO)
        self.assertEqual(article.symbols, ())
        self.assertTrue(article.is_macro)
        self.assertEqual(article.source, "Federal Reserve")

    def test_the_release_page_text_is_preferred_over_the_feeds_summary(self):
        article = from_feed_entry(
            feed_entry(summary="Headline repeated", text="<p>The actual release.</p>"),
            FED,
        )

        self.assertEqual(article.text, "The actual release.")

    def test_the_feeds_summary_is_used_when_no_release_page_was_read(self):
        article = from_feed_entry(feed_entry(summary="<p>A short summary.</p>"), FED)

        self.assertEqual(article.text, "A short summary.")

    def test_the_publishers_identifier_is_the_entries_own(self):
        article = from_feed_entry(feed_entry(entry_id="cpi-2026_09_11__07_50_40"), FED)

        self.assertEqual(article.provider_article_id, "cpi-2026_09_11__07_50_40")
        self.assertEqual(article.provider, "official_fed_monetary")

    def test_an_entry_with_no_id_falls_back_to_its_url(self):
        """The canonical-URL fallback, which is the second half of the dedupe rule and the
        reason there is only one rule."""
        article = from_feed_entry(
            feed_entry(
                entry_id="",
                url="https://www.bls.gov/news.release/archives/cpi_09112026.htm",
            ),
            FED,
        )

        self.assertEqual(
            article.provider_article_id,
            "https://www.bls.gov/news.release/archives/cpi_09112026.htm",
        )


class TimestampTests(unittest.TestCase):
    """The field where a quiet mistake would never be noticed again."""

    def test_the_publication_time_is_the_publishers_not_the_moment_we_read_it(self):
        long_ago = datetime(2019, 3, 4, 9, 0, tzinfo=timezone.utc)

        article = from_feed_entry(feed_entry(published_at=long_ago), FED)

        self.assertEqual(article.published_at, long_ago)
        # And nothing in the article claims a reading time at all -- `ingested_at` is the
        # database's column, written when the row is, and cannot be confused for this.
        self.assertFalse(hasattr(article, "ingested_at"))

    def test_a_revision_time_does_not_move_the_publication_time(self):
        article = from_alpaca(
            alpaca(published_at=PUBLISHED, provider_updated_at=PUBLISHED + timedelta(days=2))
        )

        self.assertEqual(article.published_at, PUBLISHED)
        self.assertEqual(article.provider_updated_at, PUBLISHED + timedelta(days=2))

    def test_the_content_hash_covers_the_words_and_not_the_providers_timestamp(self):
        """A provider that touches `updated_at` without changing a word has not changed the
        article, and must not cause it to be embedded again."""
        first = from_alpaca(alpaca(provider_updated_at=PUBLISHED))
        second = from_alpaca(alpaca(provider_updated_at=UPDATED))

        self.assertEqual(first.content_sha256, second.content_sha256)

    def test_a_changed_word_changes_the_hash(self):
        first = from_alpaca(alpaca(content="<p>One thing.</p>"))
        second = from_alpaca(alpaca(content="<p>Another thing.</p>"))

        self.assertNotEqual(first.content_sha256, second.content_sha256)

    def test_a_changed_headline_changes_the_hash(self):
        first = from_alpaca(alpaca())
        second = from_alpaca(alpaca(headline="A different headline"))

        self.assertNotEqual(first.content_sha256, second.content_sha256)

    def test_the_same_article_normalized_twice_hashes_the_same(self):
        self.assertEqual(
            from_alpaca(alpaca()).content_sha256, from_alpaca(alpaca()).content_sha256
        )


class CanonicalUrlTests(unittest.TestCase):
    def test_a_fragment_is_removed_because_it_names_a_place_in_a_page(self):
        article = from_alpaca(alpaca(url="https://example.com/story#section-2"))

        self.assertEqual(article.canonical_url, "https://example.com/story")

    def test_a_link_that_is_not_http_is_refused_rather_than_stored(self):
        """A stored link becomes an `href`. `javascript:` in one is the oldest way there is
        to turn a news feed into an attack."""
        for url in ("javascript:alert(1)", "data:text/html,<script>x</script>", "  "):
            with self.subTest(url=url):
                self.assertEqual(from_alpaca(alpaca(url=url)).canonical_url, "")


class ExcerptTests(unittest.TestCase):
    def test_a_short_body_is_its_own_excerpt(self):
        self.assertEqual(excerpt("Two words."), "Two words.")

    def test_a_long_body_is_cut_on_a_word_boundary_and_marked(self):
        text = " ".join(["word"] * 200)

        result = excerpt(text, limit=50)

        self.assertLessEqual(len(result), 51)
        self.assertTrue(result.endswith("…"))
        self.assertNotIn("  ", result)

    def test_whitespace_in_the_body_does_not_survive_into_the_excerpt(self):
        self.assertEqual(excerpt("a\n\n  b\tc"), "a b c")

    def test_the_default_excerpt_is_short_enough_for_a_listing(self):
        self.assertEqual(EXCERPT_CHARS, 280)


class DocumentTests(unittest.TestCase):
    def test_the_document_carries_the_times_as_iso_strings(self):
        document = as_document(from_alpaca(alpaca()))

        self.assertEqual(document["published_at"], PUBLISHED.isoformat())
        self.assertEqual(document["provider_updated_at"], UPDATED.isoformat())
        self.assertEqual(document["category"], CATEGORY_COMPANY)
        self.assertEqual(document["symbols"], ["MSFT"])


if __name__ == "__main__":
    unittest.main()
