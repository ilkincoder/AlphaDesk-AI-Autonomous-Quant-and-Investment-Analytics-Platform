"""The Federal Reserve's and BLS's own feeds: parsing them, and refusing everything else.

Every case is a canned response handed to `httpx.MockTransport`. The fixtures are trimmed
copies of what the three real feeds return -- an RSS `item` shaped like the Fed's, an Atom
`entry` shaped like the BLS's, including their quirks: the Fed repeats its headline as the
description, and the BLS writes its link as element text rather than an href.

The allowlist is the other half of this file. What is checked is not only that an official
URL is accepted but that everything else is refused *before* a request is made, because a
fetcher that can be pointed anywhere is a crawler.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import datetime, timezone

import httpx

from app import official_feeds
from app.official_feeds import (
    BLS_CPI,
    BLS_EMPLOYMENT_SITUATION,
    FED_MONETARY_POLICY,
    FEEDS_BY_KEY,
    MalformedResponseError,
    ProviderUnavailableError,
    UnknownFeedError,
    UnsafeUrlError,
    enrich_with_release_text,
    fetch_feed,
    fetch_release_text,
)

FED = FEEDS_BY_KEY["fed_monetary"]
CPI = FEEDS_BY_KEY["bls_cpi"]

FED_FEED = """<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0">
  <channel>
    <title>FRB: Press Release - Monetary Policy</title>
    <item>
      <title>Federal Reserve issues FOMC statement</title>
      <link><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm]]></link>
      <guid><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm]]></guid>
      <description><![CDATA[Federal Reserve issues FOMC statement]]></description>
      <category>Monetary Policy</category>
      <pubDate>Wed, 16 Sep 2026 18:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Minutes of the Federal Open Market Committee</title>
      <link><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/monetary20260902a.htm]]></link>
      <guid><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/monetary20260902a.htm]]></guid>
      <description><![CDATA[Minutes of the Federal Open Market Committee]]></description>
      <category>Monetary Policy</category>
      <pubDate>Wed, 02 Sep 2026 18:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

BLS_FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>bls.gov:feed:cpi</id>
  <title>Consumer Price Index</title>
  <updated>2026-09-11T07:50:40.968-04:00</updated>
  <entry>
    <title>CPI for all items increases 0.4% in August; gasoline rises</title>
    <link>https://www.bls.gov/news.release/archives/cpi_09112026.htm</link>
    <id>cpi-2026_09_11__07_50_40</id>
    <content>In August, the Consumer Price Index for All Urban Consumers rose 0.4 percent.</content>
    <published>2026-09-11T07:50:40.968-04:00</published>
    <updated>2026-09-11T07:50:40.968-04:00</updated>
    <category>News Release</category>
  </entry>
</feed>
"""

RELEASE_PAGE = """<html><head><title>FOMC</title></head><body>
<script>var tracking = 1;</script>
<p>Recent indicators suggest that economic activity has continued to expand.</p>
<p>Job gains have slowed.</p>
</body></html>"""


def responder(by_url: dict[str, object]) -> httpx.MockTransport:
    """A transport that answers by URL, so a test can serve a feed and a release."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in by_url:
            raise AssertionError(f"unexpected request to {url}")
        body = by_url[url]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


class RssTests(unittest.TestCase):
    def test_an_rss_item_is_read_as_the_fed_wrote_it(self):
        (newest, older) = fetch_feed(
            FED, transport=responder({FED_MONETARY_POLICY: FED_FEED})
        )

        self.assertEqual(newest.title, "Federal Reserve issues FOMC statement")
        self.assertEqual(
            newest.url,
            "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm",
        )
        self.assertEqual(newest.entry_id, newest.url)
        self.assertEqual(
            newest.published_at,
            datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(newest.provider_updated_at)
        self.assertEqual(older.title, "Minutes of the Federal Open Market Committee")

    def test_entries_come_back_newest_first(self):
        entries = fetch_feed(FED, transport=responder({FED_MONETARY_POLICY: FED_FEED}))

        self.assertEqual(
            [entry.published_at for entry in entries],
            sorted((entry.published_at for entry in entries), reverse=True),
        )

    def test_the_limit_is_applied_after_ordering_not_before(self):
        entries = fetch_feed(
            FED, limit=1, transport=responder({FED_MONETARY_POLICY: FED_FEED})
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Federal Reserve issues FOMC statement")

    def test_the_feds_headline_repeated_as_its_description_is_kept_verbatim(self):
        """It is what the publisher supplied. That it says nothing new is a fact the
        ingestion acts on by fetching the release, not one this parser hides."""
        (entry, _) = fetch_feed(FED, transport=responder({FED_MONETARY_POLICY: FED_FEED}))

        self.assertEqual(entry.summary, "Federal Reserve issues FOMC statement")
        self.assertLess(len(entry.summary), official_feeds.MIN_SUMMARY_CHARS)

    def test_an_item_with_no_pubdate_is_refused(self):
        broken = FED_FEED.replace(
            "<pubDate>Wed, 16 Sep 2026 18:00:00 GMT</pubDate>", ""
        )

        with self.assertRaises(MalformedResponseError) as caught:
            fetch_feed(FED, transport=responder({FED_MONETARY_POLICY: broken}))

        self.assertIn("pubDate", str(caught.exception))

    def test_an_unreadable_pubdate_is_refused_rather_than_guessed_at(self):
        broken = FED_FEED.replace(
            "Wed, 16 Sep 2026 18:00:00 GMT", "sometime last week"
        )

        with self.assertRaises(MalformedResponseError):
            fetch_feed(FED, transport=responder({FED_MONETARY_POLICY: broken}))


class AtomTests(unittest.TestCase):
    def test_an_atom_entry_is_read_as_the_bls_wrote_it(self):
        (entry,) = fetch_feed(CPI, transport=responder({BLS_CPI: BLS_FEED}))

        self.assertEqual(
            entry.title, "CPI for all items increases 0.4% in August; gasoline rises"
        )
        # The link is element text here, not an href attribute -- both appear in the wild.
        self.assertEqual(
            entry.url, "https://www.bls.gov/news.release/archives/cpi_09112026.htm"
        )
        self.assertEqual(entry.entry_id, "cpi-2026_09_11__07_50_40")
        self.assertEqual(
            entry.published_at,
            datetime(2026, 9, 11, 11, 50, 40, 968000, tzinfo=timezone.utc),
        )

    def test_an_update_time_equal_to_the_publication_time_is_not_an_update(self):
        """The BLS repeats `updated` on a new entry. Storing that as a revision would say a
        release had been revised when it had only been published."""
        (entry,) = fetch_feed(CPI, transport=responder({BLS_CPI: BLS_FEED}))

        self.assertIsNone(entry.provider_updated_at)

    def test_a_genuinely_later_update_time_is_kept(self):
        revised = BLS_FEED.replace(
            "<updated>2026-09-11T07:50:40.968-04:00</updated>",
            "<updated>2026-09-12T09:00:00.000-04:00</updated>",
        )

        (entry,) = fetch_feed(CPI, transport=responder({BLS_CPI: revised}))

        self.assertEqual(
            entry.provider_updated_at,
            datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc),
        )

    def test_an_entry_with_no_published_time_is_refused(self):
        broken = BLS_FEED.replace(
            "<published>2026-09-11T07:50:40.968-04:00</published>", ""
        )

        with self.assertRaises(MalformedResponseError) as caught:
            fetch_feed(CPI, transport=responder({BLS_CPI: broken}))

        self.assertIn("published", str(caught.exception))

    def test_a_feed_that_is_not_well_formed_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            fetch_feed(CPI, transport=responder({BLS_CPI: "<feed><entry>"}))

    def test_a_document_that_is_neither_atom_nor_rss_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            fetch_feed(CPI, transport=responder({BLS_CPI: "<html><body>hi</body></html>"}))


class AllowlistTests(unittest.TestCase):
    """Nothing is fetched that is not one of the three feeds or a release on the two
    agencies' own domains."""

    def test_an_unknown_feed_key_is_refused(self):
        unknown = official_feeds.Feed(
            key="not_a_feed", url=BLS_CPI, title="?", source="?"
        )

        with self.assertRaises(UnknownFeedError):
            fetch_feed(unknown)

    def test_a_release_on_another_domain_is_refused(self):
        for url in (
            "https://example.com/release.htm",
            "https://www.bls.gov.evil.example/cpi.htm",
            "http://www.bls.gov/release.htm",
            "https://benzinga.com/article",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    fetch_release_text(url)

    def test_a_refused_url_never_reaches_the_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a request was made to a refused URL")

        with self.assertRaises(UnsafeUrlError):
            fetch_release_text(
                "https://example.com/release.htm",
                transport=httpx.MockTransport(handler),
            )

    def test_every_configured_feed_is_on_an_allowed_domain(self):
        """A feed added to the list without being checked would otherwise fail at the first
        request rather than here."""
        for feed in official_feeds.FEEDS:
            with self.subTest(feed=feed.key):
                official_feeds._require_official_url(feed.url)

    def test_the_three_feeds_are_the_ones_verified_against_the_agencies(self):
        self.assertEqual(
            {feed.url for feed in official_feeds.FEEDS},
            {FED_MONETARY_POLICY, BLS_CPI, BLS_EMPLOYMENT_SITUATION},
        )


class ReleaseEnrichmentTests(unittest.TestCase):
    def test_a_thin_summary_gets_the_release_page_fetched_for_it(self):
        entries = fetch_feed(FED, transport=responder({FED_MONETARY_POLICY: FED_FEED}))

        enriched = enrich_with_release_text(
            entries,
            max_releases=1,
            transport=responder(
                {
                    "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm": RELEASE_PAGE
                }
            ),
        )

        self.assertIn("economic activity has continued to expand", enriched[0].text or "")
        # Markup is gone, and so is the script.
        self.assertNotIn("<p>", enriched[0].text or "")
        self.assertNotIn("var tracking", enriched[0].text or "")
        # The entry whose release was not read keeps the summary the feed gave it, and is
        # not left with nothing.
        self.assertIsNone(enriched[1].text)
        self.assertEqual(enriched[1].summary, "Minutes of the Federal Open Market Committee")

    def test_at_most_max_releases_pages_are_fetched(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, text=RELEASE_PAGE)

        entries = fetch_feed(FED, transport=responder({FED_MONETARY_POLICY: FED_FEED}))
        enrich_with_release_text(
            entries, max_releases=1, transport=httpx.MockTransport(handler)
        )

        self.assertEqual(len(seen), 1)

    def test_a_release_that_cannot_be_read_keeps_the_entry(self):
        """A page that would not load is not a reason to lose a release the agency
        published; the article is shorter, and still the publisher's own words."""
        entries = fetch_feed(FED, transport=responder({FED_MONETARY_POLICY: FED_FEED}))

        enriched = enrich_with_release_text(
            entries,
            max_releases=2,
            transport=responder(
                {
                    "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm": httpx.Response(
                        503, text="down"
                    ),
                    "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260902a.htm": httpx.Response(
                        503, text="down"
                    ),
                }
            ),
        )

        self.assertEqual(len(enriched), 2)
        self.assertIsNone(enriched[0].text)

    def test_a_long_summary_is_left_alone(self):
        """The whole point of the enrichment is that it is bounded and only where needed."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, text=RELEASE_PAGE)

        long_summary = "x" * (official_feeds.MIN_SUMMARY_CHARS + 1)
        rich = official_feeds.FeedEntry(
            entry_id="id",
            title="t",
            url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm",
            summary=long_summary,
            published_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
            provider_updated_at=None,
        )

        enriched = enrich_with_release_text(
            [rich], max_releases=5, transport=httpx.MockTransport(handler)
        )

        self.assertEqual(seen, [])
        self.assertIsNone(enriched[0].text)

    def test_zero_releases_fetches_nothing(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, text=RELEASE_PAGE)

        entries = fetch_feed(FED, transport=responder({FED_MONETARY_POLICY: FED_FEED}))
        enrich_with_release_text(
            entries, max_releases=0, transport=httpx.MockTransport(handler)
        )

        self.assertEqual(seen, [])


class TransportTests(unittest.TestCase):
    def test_a_feed_that_answers_an_error_status_is_an_outage(self):
        with self.assertRaises(ProviderUnavailableError) as caught:
            fetch_feed(
                CPI, transport=responder({BLS_CPI: httpx.Response(503, text="down")})
            )

        self.assertIn("503", str(caught.exception))

    def test_a_timeout_is_an_outage_that_says_nothing_was_retried(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        with self.assertRaises(ProviderUnavailableError) as caught:
            fetch_feed(CPI, transport=httpx.MockTransport(handler))

        self.assertIn("Nothing was retried", str(caught.exception))

    def test_the_application_identifies_itself(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text=BLS_FEED)

        fetch_feed(CPI, transport=httpx.MockTransport(handler))

        self.assertEqual(seen[0].headers["User-Agent"], official_feeds.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
