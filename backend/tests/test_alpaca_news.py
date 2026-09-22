"""The Alpaca news client: what it reads, what it refuses, and what it never leaks.

Every case is a canned HTTP response handed to `httpx.MockTransport`. Nothing here opens a
socket, and no credential in it is real.

Two properties are the reason this client exists in this shape, and both are checked here:

* **it is a different host from the trading client** -- `data.alpaca.markets`, never
  `paper-api.alpaca.markets` or the live host, and it refuses anything else;
* **it does not claim the data is live** -- a 200 with an article dated five minutes ago is
  reported as the provider dated it, and nothing here infers an entitlement from it.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from app import alpaca_news
from app.alpaca_news import (
    DATA_BASE_URL,
    MAX_LIMIT,
    InvalidRequestError,
    LiveHostError,
    MalformedResponseError,
    MissingCredentialsError,
    ProviderAuthError,
    ProviderUnavailableError,
    RateLimitError,
    fetch_news,
)

KEY_ID = "PKTESTKEYIDNOTREAL"
SECRET = "test-secret-not-real"

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)

# `id` is a JSON *number*, which is what Alpaca actually sends -- unlike every money field,
# which it sends as a string. The fixture would otherwise encode the wrong assumption and
# the client would pass its tests while failing against the real endpoint.
ARTICLE = {
    "id": 61923311,
    "headline": "Analyst Sees More Upside for Microsoft Stock",
    "author": "Evette Mitkov",
    "created_at": "2026-09-22T14:39:05Z",
    "updated_at": "2026-09-22T14:39:05Z",
    "summary": "A summary of the story.",
    "content": "<p>The body of the story.</p>",
    "images": [],
    "source": "benzinga",
    "symbols": ["MSFT"],
    "url": "https://www.benzinga.com/trading-ideas/movers/26/09/61923311",
}


def transport_returning(*responses: object) -> httpx.MockTransport:
    queued = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queued:
            raise AssertionError(f"unexpected request to {request.url}")
        nxt = queued.pop(0)
        if isinstance(nxt, httpx.Response):
            return nxt
        return httpx.Response(200, json=nxt)

    return httpx.MockTransport(handler)


def page(*articles: dict, next_page_token: str | None = None) -> dict:
    return {
        "news": list(articles),
        "next_page_token": next_page_token,
    }


def fetch(*responses: object, **overrides):
    return fetch_news(
        overrides.pop("symbols", ["MSFT"]),
        api_key_id=KEY_ID,
        api_secret_key=SECRET,
        transport=transport_returning(*responses),
        now=NOW,
        **overrides,
    )


class ReadingTests(unittest.TestCase):
    def test_every_field_is_read_as_the_provider_stated_it(self):
        (item,) = fetch(page(ARTICLE))

        self.assertEqual(item.provider_article_id, "61923311")
        self.assertEqual(item.headline, "Analyst Sees More Upside for Microsoft Stock")
        self.assertEqual(item.source, "benzinga")
        self.assertEqual(
            item.url, "https://www.benzinga.com/trading-ideas/movers/26/09/61923311"
        )
        self.assertEqual(item.content, "<p>The body of the story.</p>")
        self.assertEqual(item.symbols, ("MSFT",))

    def test_the_publication_time_is_the_providers_and_is_utc(self):
        (item,) = fetch(page(ARTICLE))

        self.assertEqual(item.published_at, datetime(2026, 9, 22, 14, 39, 5, tzinfo=timezone.utc))
        self.assertEqual(item.provider_updated_at, item.published_at)

    def test_an_article_with_no_update_time_is_kept(self):
        """The update time is the provider's claim about revisions, not the story."""
        without = {key: value for key, value in ARTICLE.items() if key != "updated_at"}

        (item,) = fetch(page(without))

        self.assertIsNone(item.provider_updated_at)
        self.assertEqual(item.provider_article_id, "61923311")

    def test_symbols_are_normalized_and_deduplicated(self):
        (item,) = fetch(page({**ARTICLE, "symbols": ["msft", "MSFT", "aapl"]}))

        self.assertEqual(item.symbols, ("AAPL", "MSFT"))

    def test_no_articles_is_a_valid_answer(self):
        self.assertEqual(fetch(page()), [])

    def test_the_window_asked_for_is_sent_explicitly(self):
        """Left to the provider, the default window is the thing that changes with
        entitlement -- a caller that received a different window depending on the plan
        without saying so could not report what it had asked for."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=page())

        fetch_news(
            ["MSFT"],
            days=7,
            api_key_id=KEY_ID,
            api_secret_key=SECRET,
            transport=httpx.MockTransport(handler),
            now=NOW,
        )

        start = seen[0].url.params["start"]
        self.assertEqual(start, "2026-09-15T15:00:00Z")
        self.assertEqual(seen[0].url.params["include_content"], "true")
        self.assertEqual(seen[0].url.params["symbols"], "MSFT")

    def test_symbols_are_sorted_so_the_request_does_not_depend_on_holding_order(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=page())

        fetch_news(
            ["NVDA", "aapl", "MSFT", "AAPL"],
            api_key_id=KEY_ID,
            api_secret_key=SECRET,
            transport=httpx.MockTransport(handler),
            now=NOW,
        )

        self.assertEqual(seen[0].url.params["symbols"], "AAPL,MSFT,NVDA")

    def test_credentials_travel_as_headers_and_never_in_the_url(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=page())

        fetch_news(
            ["MSFT"],
            api_key_id=KEY_ID,
            api_secret_key=SECRET,
            transport=httpx.MockTransport(handler),
            now=NOW,
        )

        self.assertEqual(seen[0].headers["APCA-API-KEY-ID"], KEY_ID)
        self.assertEqual(seen[0].headers["APCA-API-SECRET-KEY"], SECRET)
        self.assertNotIn(KEY_ID, str(seen[0].url))
        self.assertNotIn(SECRET, str(seen[0].url))

    def test_the_request_goes_to_the_market_data_host(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url.copy_with(query=None)))
            return httpx.Response(200, json=page())

        fetch_news(
            ["MSFT"],
            api_key_id=KEY_ID,
            api_secret_key=SECRET,
            transport=httpx.MockTransport(handler),
            now=NOW,
        )

        self.assertEqual(seen, [f"{DATA_BASE_URL}/v1beta1/news"])


class PaginationTests(unittest.TestCase):
    def test_a_second_page_is_followed_and_the_token_is_sent_back(self):
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("page_token"))
            if len(seen) == 1:
                return httpx.Response(
                    200,
                    json=page(ARTICLE, next_page_token="tok-2"),
                )
            return httpx.Response(
                200, json=page({**ARTICLE, "id": "2", "headline": "Second"})
            )

        items = fetch_news(
            ["MSFT"],
            limit=10,
            api_key_id=KEY_ID,
            api_secret_key=SECRET,
            transport=httpx.MockTransport(handler),
            now=NOW,
        )

        self.assertEqual(seen, [None, "tok-2"])
        self.assertEqual([item.provider_article_id for item in items], ["2", "61923311"][::-1])

    def test_no_more_pages_are_read_once_the_limit_is_reached(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            # A page that always has more to give, whether or not more was wanted.
            return httpx.Response(200, json=page(ARTICLE, ARTICLE, next_page_token="more"))

        items = fetch_news(
            ["MSFT"],
            limit=1,
            api_key_id=KEY_ID,
            api_secret_key=SECRET,
            transport=httpx.MockTransport(handler),
            now=NOW,
        )

        self.assertEqual(len(seen), 1)
        self.assertEqual(len(items), 1)

    def test_pagination_stops_at_the_page_cap(self):
        """A token that never stops is not followed forever: this is a bounded read, and a
        caller who wants more asks again."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=page(ARTICLE, next_page_token="always-more"))

        fetch_news(
            ["MSFT"],
            limit=MAX_LIMIT,
            api_key_id=KEY_ID,
            api_secret_key=SECRET,
            transport=httpx.MockTransport(handler),
            now=NOW,
        )

        self.assertEqual(len(seen), alpaca_news.MAX_PAGES)


class HostAndCredentialTests(unittest.TestCase):
    def test_the_paper_trading_host_is_refused(self):
        with self.assertRaises(LiveHostError) as caught:
            fetch_news(
                ["MSFT"],
                api_key_id=KEY_ID,
                api_secret_key=SECRET,
                base_url="https://paper-api.alpaca.markets",
            )

        self.assertIn("data.alpaca.markets", str(caught.exception))

    def test_the_live_host_is_refused_too(self):
        with self.assertRaises(LiveHostError):
            fetch_news(
                ["MSFT"],
                api_key_id=KEY_ID,
                api_secret_key=SECRET,
                base_url="https://api.alpaca.markets",
            )

    def test_a_refused_host_never_reaches_the_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a request was made to a host that should be refused")

        with self.assertRaises(LiveHostError):
            fetch_news(
                ["MSFT"],
                api_key_id=KEY_ID,
                api_secret_key=SECRET,
                base_url="https://api.alpaca.markets",
                transport=httpx.MockTransport(handler),
            )

    def test_missing_credentials_fail_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a request was made without credentials")

        for key_id, secret in (("", SECRET), (KEY_ID, ""), ("  ", "  ")):
            with self.subTest(key_id=key_id, secret=secret):
                with self.assertRaises(MissingCredentialsError):
                    fetch_news(
                        ["MSFT"],
                        api_key_id=key_id,
                        api_secret_key=secret,
                        transport=httpx.MockTransport(handler),
                    )

    def test_nothing_configured_fails_before_any_request(self):
        """Settings are patched rather than left to the environment, so this asserts the
        same thing on a machine that happens to have credentials in `.env`."""
        with (
            mock.patch.object(alpaca_news, "_configured_key_id", return_value=None),
            mock.patch.object(alpaca_news, "_configured_secret_key", return_value=None),
        ):
            with self.assertRaises(MissingCredentialsError):
                fetch_news(["MSFT"], transport=transport_returning(page()))


class RequestValidationTests(unittest.TestCase):
    def test_no_symbols_is_refused(self):
        with self.assertRaises(InvalidRequestError):
            fetch_news([], api_key_id=KEY_ID, api_secret_key=SECRET)

    def test_a_limit_above_the_provider_ceiling_is_refused(self):
        with self.assertRaises(InvalidRequestError):
            fetch_news(
                ["MSFT"],
                limit=MAX_LIMIT + 1,
                api_key_id=KEY_ID,
                api_secret_key=SECRET,
            )

    def test_a_boolean_limit_is_refused_rather_than_read_as_one(self):
        with self.assertRaises(InvalidRequestError):
            fetch_news(
                ["MSFT"], limit=True, api_key_id=KEY_ID, api_secret_key=SECRET
            )


class ProviderErrorTests(unittest.TestCase):
    def _failing(self, response: httpx.Response) -> Exception:
        with self.assertRaises(Exception) as caught:  # noqa: B017 - asserted below
            fetch(*[response])
        return caught.exception

    def test_a_refused_key_is_an_auth_error_naming_the_plan(self):
        error = self._failing(
            httpx.Response(403, json={"message": "subscription does not permit this"})
        )

        self.assertIsInstance(error, ProviderAuthError)
        self.assertIn("plan", str(error))

    def test_a_rate_limit_is_its_own_error(self):
        self.assertIsInstance(
            self._failing(httpx.Response(429, json={"message": "slow down"})),
            RateLimitError,
        )

    def test_a_provider_side_failure_is_an_outage(self):
        self.assertIsInstance(
            self._failing(httpx.Response(503, text="<html>nope</html>")),
            ProviderUnavailableError,
        )

    def test_a_timeout_is_an_outage_that_says_nothing_was_retried(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        with self.assertRaises(ProviderUnavailableError) as caught:
            fetch_news(
                ["MSFT"],
                api_key_id=KEY_ID,
                api_secret_key=SECRET,
                transport=httpx.MockTransport(handler),
                now=NOW,
            )

        self.assertIn("Nothing was retried", str(caught.exception))

    def test_a_credential_echoed_back_in_an_error_is_scrubbed(self):
        error = self._failing(
            httpx.Response(401, json={"message": f"key {KEY_ID} / {SECRET} is not valid"})
        )

        self.assertNotIn(KEY_ID, str(error))
        self.assertNotIn(SECRET, str(error))
        self.assertIn("***", str(error))


class PayloadValidationTests(unittest.TestCase):
    def test_a_body_that_is_not_an_object_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            fetch(httpx.Response(200, json=["not", "an", "object"]))

    def test_a_response_with_no_news_list_is_refused(self):
        with self.assertRaises(MalformedResponseError) as caught:
            fetch({"items": []})

        self.assertIn("news", str(caught.exception))

    def test_an_article_without_an_id_is_refused(self):
        without = {key: value for key, value in ARTICLE.items() if key != "id"}

        with self.assertRaises(MalformedResponseError) as caught:
            fetch(page(without))

        self.assertIn("id", str(caught.exception))

    def test_a_numeric_id_is_read_as_text(self):
        """The shape the real endpoint sends. An identifier is not a quantity, so a JSON
        number carries no precision to lose -- unlike the money fields, which are strings
        and are refused as numbers because by then the digits are no longer the provider's.
        """
        (item,) = fetch(page({**ARTICLE, "id": 61927717}))

        self.assertEqual(item.provider_article_id, "61927717")
        self.assertIsInstance(item.provider_article_id, str)

    def test_a_string_id_is_still_accepted(self):
        (item,) = fetch(page({**ARTICLE, "id": " 61927717 "}))

        self.assertEqual(item.provider_article_id, "61927717")

    def test_a_fractional_id_is_refused(self):
        """An id that has been through a float may already have the wrong last digits, and
        which article this is is not a question to guess at."""
        for bad in (61927717.5, 6.1927717e7, True, {}, []):
            with self.subTest(value=bad):
                with self.assertRaises(MalformedResponseError):
                    fetch(page({**ARTICLE, "id": bad}))

    def test_an_article_without_a_date_is_refused_rather_than_given_ours(self):
        """The one mistake here nothing could detect afterwards: an article stamped with the
        time we read it looks like it just broke."""
        without = {key: value for key, value in ARTICLE.items() if key != "created_at"}

        with self.assertRaises(MalformedResponseError) as caught:
            fetch(page(without))

        self.assertIn("created_at", str(caught.exception))

    def test_an_unreadable_date_is_refused_by_name(self):
        with self.assertRaises(MalformedResponseError) as caught:
            fetch(page({**ARTICLE, "created_at": "last Tuesday"}))

        self.assertIn("created_at", str(caught.exception))

    def test_a_naive_timestamp_is_read_as_utc(self):
        (item,) = fetch(page({**ARTICLE, "created_at": "2026-09-22T14:39:05"}))

        self.assertEqual(item.published_at.tzinfo, timezone.utc)

    def test_symbols_that_are_not_a_list_are_refused(self):
        with self.assertRaises(MalformedResponseError):
            fetch(page({**ARTICLE, "symbols": "MSFT"}))

    def test_a_missing_symbol_list_is_read_as_no_symbols(self):
        without = {key: value for key, value in ARTICLE.items() if key != "symbols"}

        (item,) = fetch(page(without))

        self.assertEqual(item.symbols, ())


if __name__ == "__main__":
    unittest.main()
