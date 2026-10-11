"""The client's date-bounded fetch and its instrument search, against canned HTTP.

No network. The transport helpers and the fixed clock come from `test_twelvedata`, so both
halves of this client are exercised against the same canned responses and the same fake key --
one of which is a symbol that fails to resolve, which is a case the search has to get right
rather than one it can assume away.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

import httpx

from app.twelvedata import (
    ADJUST_MODE,
    INTERVAL,
    PROVIDER,
    DataValidationError,
    InvalidRequestError,
    MalformedResponseError,
    MissingApiKeyError,
    RateLimitError,
    SymbolNotFoundError,
    fetch_daily_bars_range,
    search_instruments,
)
from tests.test_twelvedata import (
    API_KEY,
    FIXED_NOW,
    LAST_CLOSED_SESSION,
    make_bar,
    make_payload,
    transport,
)


def run_range(
    mock_transport: httpx.MockTransport,
    *,
    start: date = date(2026, 9, 1),
    end: date = date(2026, 9, 17),
    symbol: str = "NVDA",
    exchange: str = "NASDAQ",
    api_key: str | None = API_KEY,
):
    return fetch_daily_bars_range(
        symbol,
        exchange,
        start,
        end,
        api_key=api_key,
        transport=mock_transport,
        now=FIXED_NOW,
    )


def search_payload(*rows: dict) -> dict:
    return {"data": list(rows), "status": "ok"}


def a_row(
    symbol: str = "AAPL",
    *,
    name: str = "Apple Inc.",
    exchange: str = "NASDAQ",
    instrument_type: str = "Common Stock",
    currency: str = "USD",
    country: str = "United States",
) -> dict:
    return {
        "symbol": symbol,
        "instrument_name": name,
        "exchange": exchange,
        "mic_code": "XNGS",
        "exchange_timezone": "America/New_York",
        "instrument_type": instrument_type,
        "country": country,
        "currency": currency,
    }


class RangeRequestTests(unittest.TestCase):
    def test_the_request_carries_the_dates_and_no_bar_count(self):
        requests: list = []
        run_range(
            transport(body=make_payload([make_bar("2026-09-17")]), requests=requests)
        )

        params = requests[0].url.params
        self.assertEqual(params["start_date"], "2026-09-01")
        self.assertEqual(params["end_date"], "2026-09-17")
        self.assertEqual(params["interval"], INTERVAL)
        self.assertEqual(params["adjust"], ADJUST_MODE)
        self.assertEqual(params["symbol"], "NVDA")
        self.assertEqual(params["exchange"], "NASDAQ")
        # A count would cap a range at a number nobody asked for; the whole point of asking by
        # date is that there is no such number.
        self.assertNotIn("outputsize", params)
        self.assertNotIn("order", params)

    def test_the_key_travels_in_the_authorization_header_and_never_in_the_url(self):
        requests: list = []
        run_range(
            transport(body=make_payload([make_bar("2026-09-17")]), requests=requests)
        )

        self.assertEqual(requests[0].headers["Authorization"], f"apikey {API_KEY}")
        self.assertNotIn(API_KEY, str(requests[0].url))

    def test_bars_are_returned_oldest_first_however_the_provider_ordered_them(self):
        # The provider answers newest-first.
        series = run_range(
            transport(
                body=make_payload(
                    [make_bar("2026-09-16"), make_bar("2026-09-15"), make_bar("2026-09-14")]
                )
            )
        )

        self.assertEqual(
            [bar.trading_date for bar in series.bars],
            [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)],
        )

    def test_the_current_session_is_dropped_like_the_count_based_fetch_drops_it(self):
        # FIXED_NOW is 2026-09-18 17:00 in New York, so that session has not finished.
        series = run_range(
            transport(
                body=make_payload([make_bar("2026-09-18"), make_bar("2026-09-17")])
            ),
            end=date(2026, 9, 18),
        )

        self.assertEqual([bar.trading_date for bar in series.bars], [LAST_CLOSED_SESSION])

    def test_a_bar_outside_the_requested_dates_is_not_returned(self):
        # The caller reporting "the sessions this run used" has to be able to trust that the
        # tuple it was handed is inside the period it named.
        series = run_range(
            transport(
                body=make_payload([make_bar("2026-09-17"), make_bar("2026-08-20")])
            )
        )

        self.assertEqual([bar.trading_date for bar in series.bars], [LAST_CLOSED_SESSION])

    def test_a_range_with_no_sessions_is_an_empty_series_and_not_an_error(self):
        series = run_range(
            transport(body=make_payload([])), start=date(2026, 9, 5), end=date(2026, 9, 6)
        )

        self.assertEqual(series.bars, ())
        self.assertEqual(series.returned_bars, 0)

    def test_the_requested_dates_are_kept_beside_the_ones_that_came_back(self):
        series = run_range(
            transport(body=make_payload([make_bar("2026-09-17")])),
            start=date(2026, 9, 1),
            end=date(2026, 9, 20),
        )

        # Asked for a Sunday-to-Sunday window and got one session out of it. Both facts are
        # kept, because a caller reporting coverage has to say which one it means.
        self.assertEqual(series.requested_start, date(2026, 9, 1))
        self.assertEqual(series.requested_end, date(2026, 9, 20))
        self.assertEqual(series.bars[0].trading_date, LAST_CLOSED_SESSION)

    def test_the_adjustment_metadata_says_what_the_prices_are(self):
        series = run_range(transport(body=make_payload([make_bar("2026-09-17")])))

        self.assertEqual(series.provider, PROVIDER)
        self.assertEqual(series.adjustment_basis, "adjusted")
        self.assertEqual(series.provider_adjust_mode, "splits")
        self.assertEqual(series.currency, "USD")

    def test_prices_arrive_as_exact_decimals_at_the_providers_own_precision(self):
        series = run_range(
            transport(
                body=make_payload(
                    [make_bar("2026-09-17", low="190.0099945", open_="200.0000000",
                              close="225.0099945", high="226.0000000")]
                )
            )
        )

        self.assertEqual(series.bars[0].close, Decimal("225.0099945"))


class RangeValidationTests(unittest.TestCase):
    def test_a_start_on_or_after_the_end_is_refused_before_any_request(self):
        requests: list = []
        mock = transport(body=make_payload([]), requests=requests)

        for start, end in (
            (date(2026, 9, 17), date(2026, 9, 17)),
            (date(2026, 9, 18), date(2026, 9, 17)),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(InvalidRequestError):
                    run_range(mock, start=start, end=end)

        self.assertEqual(requests, [])

    def test_a_missing_key_is_reported_before_any_request(self):
        # `api_key=None` means "use the configured one", so it is the configuration that has
        # to be empty for this case -- which is why it is patched rather than passed.
        requests: list = []
        mock_transport = transport(body=make_payload([]), requests=requests)

        with mock.patch("app.twelvedata._configured_api_key", return_value=None):
            with self.assertRaises(MissingApiKeyError):
                run_range(mock_transport, api_key=None)
        with self.assertRaises(MissingApiKeyError):
            run_range(mock_transport, api_key="   ")

        self.assertEqual(requests, [])

    def test_an_unknown_ticker_arrives_as_a_symbol_error(self):
        with self.assertRaises(SymbolNotFoundError):
            run_range(
                transport(
                    body={"code": 404, "message": "symbol not found", "status": "error"}
                )
            )

    def test_a_response_about_another_instrument_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            run_range(transport(body=make_payload([make_bar("2026-09-17")], symbol="AAPL")))

    def test_a_bad_bar_is_refused_rather_than_returned(self):
        bad = make_bar("2026-09-17", open_="300.00", high="226.00")

        with self.assertRaises(DataValidationError):
            run_range(transport(body=make_payload([bad])))

    def test_an_exhausted_credit_allowance_is_its_own_error(self):
        with self.assertRaises(RateLimitError):
            run_range(transport(body={"code": 429, "message": "limit reached"}))


class SearchTests(unittest.TestCase):
    def test_only_united_states_dollar_listings_are_offered(self):
        # What the provider really returns for "apple": the same ticker in Mexico, Colombia,
        # Argentina and London. None of them is the instrument this application backtests.
        series = search_instruments(
            "apple",
            transport=transport(
                body=search_payload(
                    a_row("AAPL", exchange="NASDAQ"),
                    a_row("AAPL", name="Apple Inc.", exchange="BMV", country="Mexico",
                          currency="MXN"),
                    a_row("0R2V", name="Apple Inc.", exchange="LSE",
                          country="United Kingdom", currency="USD"),
                    a_row("AAPL", name="Apple Inc. CEDEAR", exchange="BCBA",
                          country="Argentina", currency="ARS"),
                    a_row("AAPL", name="Apple Inc.", exchange="IEX"),
                )
            ),
            api_key=API_KEY,
        )

        self.assertEqual([match.exchange for match in series], ["NASDAQ", "IEX"])
        self.assertTrue(all(match.currency == "USD" for match in series))

    def test_a_row_missing_the_fields_that_identify_it_is_dropped(self):
        incomplete = a_row("AAPL")
        del incomplete["country"]
        nameless = a_row("MSFT")
        nameless["instrument_name"] = "  "

        series = search_instruments(
            "a",
            transport=transport(body=search_payload(incomplete, nameless, a_row("NVDA"))),
            api_key=API_KEY,
        )

        self.assertEqual([match.symbol for match in series], ["NVDA"])

    def test_the_providers_own_relevance_order_is_kept(self):
        series = search_instruments(
            "a",
            transport=transport(
                body=search_payload(a_row("AAPL"), a_row("APLE"), a_row("MSFT"))
            ),
            api_key=API_KEY,
        )

        self.assertEqual([match.symbol for match in series], ["AAPL", "APLE", "MSFT"])

    def test_no_matches_is_an_empty_tuple_rather_than_an_error(self):
        series = search_instruments(
            "zzzzqqqq", transport=transport(body=search_payload()), api_key=API_KEY
        )

        self.assertEqual(series, ())

    def test_a_name_and_a_type_come_back_with_every_match(self):
        series = search_instruments(
            "nvda",
            transport=transport(
                body=search_payload(
                    a_row("NVDA", name="NVIDIA Corporation", exchange="NASDAQ")
                )
            ),
            api_key=API_KEY,
        )

        match = series[0]
        self.assertEqual(match.symbol, "NVDA")
        self.assertEqual(match.name, "NVIDIA Corporation")
        self.assertEqual(match.exchange, "NASDAQ")
        self.assertEqual(match.instrument_type, "Common Stock")
        self.assertEqual(match.country, "United States")

    def test_the_query_reaches_the_provider_untouched(self):
        requests: list = []
        search_instruments(
            "  Nvidia  ",
            transport=transport(body=search_payload(), requests=requests),
            api_key=API_KEY,
        )

        # Trimmed, and otherwise exactly what was typed: a company name is not a ticker and
        # must not be upper-cased into looking like one.
        self.assertEqual(requests[0].url.params["symbol"], "Nvidia")

    def test_an_empty_query_is_refused_before_any_request(self):
        requests: list = []
        mock = transport(body=search_payload(), requests=requests)

        for query in ("", "   "):
            with self.subTest(query=query):
                with self.assertRaises(InvalidRequestError):
                    search_instruments(query, transport=mock, api_key=API_KEY)

        self.assertEqual(requests, [])

    def test_the_limit_is_bounded(self):
        mock = transport(body=search_payload(a_row()))

        for limit in (0, 21, -1):
            with self.subTest(limit=limit):
                with self.assertRaises(InvalidRequestError):
                    search_instruments("a", limit=limit, transport=mock, api_key=API_KEY)

    def test_at_most_the_limit_is_returned_however_many_matched(self):
        rows = [a_row(f"SYM{index}") for index in range(10)]
        series = search_instruments(
            "sym", limit=3, transport=transport(body=search_payload(*rows)), api_key=API_KEY
        )

        self.assertEqual(len(series), 3)

    def test_a_missing_key_is_reported_before_any_request(self):
        with mock.patch("app.twelvedata._configured_api_key", return_value=None):
            with self.assertRaises(MissingApiKeyError):
                search_instruments(
                    "apple", transport=transport(body=search_payload()), api_key=None
                )

    def test_an_error_payload_is_raised_rather_than_read_as_no_matches(self):
        # "Nothing matched" and "the request failed" are different answers and the page says
        # them differently, so an error must not arrive as an empty list.
        with self.assertRaises(RateLimitError):
            search_instruments(
                "apple",
                transport=transport(body={"code": 429, "message": "limit reached"}),
                api_key=API_KEY,
            )

    def test_a_response_with_no_data_list_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            search_instruments("apple", transport=transport(body={"status": "ok"}),
                               api_key=API_KEY)


class ClockTests(unittest.TestCase):
    def test_the_library_defaults_to_the_real_clock_when_no_clock_is_given(self):
        # Everything above passes a fixed `now`; this checks that the parameter is optional
        # and that leaving it out does not crash on a naive datetime.
        series = fetch_daily_bars_range(
            "NVDA",
            "NASDAQ",
            date(2026, 9, 1),
            date(2026, 9, 17),
            api_key=API_KEY,
            transport=transport(body=make_payload([make_bar("2026-09-17")])),
        )

        self.assertIsNotNone(series.retrieved_at)
        self.assertLess(series.retrieved_at, datetime.now(timezone.utc))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
