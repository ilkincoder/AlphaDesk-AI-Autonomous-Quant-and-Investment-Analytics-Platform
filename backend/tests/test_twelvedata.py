"""The Twelve Data client, against canned HTTP responses.

No network. `httpx.MockTransport` hands the client whatever response a test wants --
including a read timeout, a truncated body, or an error delivered under HTTP 200 -- and
records the request so the parameters can be asserted.

The clock is fixed. Every date assertion is relative to `FIXED_NOW`, never to the wall
clock, so this suite behaves the same on a trading day, at a weekend, and in a different
timezone from the exchange.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import httpx

from app.twelvedata import (
    ADJUST_MODE,
    ADJUSTMENT_BASIS,
    INTERVAL,
    MAX_BARS,
    PROVIDER,
    DataValidationError,
    InvalidRequestError,
    MalformedResponseError,
    MissingApiKeyError,
    ProviderAuthError,
    ProviderUnavailableError,
    RateLimitError,
    SymbolNotFoundError,
    TwelveDataError,
    fetch_daily_bars,
    redact,
)

# Stands in for a real key. Any assertion that this string is absent is meaningful
# precisely because a test knows what to look for.
API_KEY = "test-key-not-a-real-secret"

# 2026-09-18 21:00 UTC is 17:00 in New York, so the exchange's own calendar date is
# 2026-09-18 -- which is the date the policy must drop.
FIXED_NOW = datetime(2026, 9, 18, 21, 0, tzinfo=timezone.utc)
LAST_CLOSED_SESSION = date(2026, 9, 17)

# A coherent bar carrying the kind of price a live NVDA run actually returned: seven
# decimal places, where numeric(18,6) holds six. Coherent, so the OHLC checks are
# satisfied and the precision behaviour is what is under test.
SEVEN_DP_BAR = {
    "datetime_text": "2026-09-17",
    "low": "190.0099945",
    "open_": "200.0000000",
    "close": "225.0099945",
    "high": "226.0000000",
}


def make_bar(
    datetime_text: str,
    *,
    open_: str = "100.00",
    high: str = "105.00",
    low: str = "99.00",
    close: str = "104.00",
    volume: str = "1000000",
) -> dict:
    return {
        "datetime": datetime_text,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def make_payload(
    values: list,
    *,
    symbol: str = "NVDA",
    exchange: str = "NASDAQ",
    interval: str = INTERVAL,
    currency: str = "USD",
    exchange_timezone: str = "America/New_York",
    status: str = "ok",
) -> dict:
    return {
        "meta": {
            "symbol": symbol,
            "interval": interval,
            "currency": currency,
            "exchange": exchange,
            "exchange_timezone": exchange_timezone,
            "mic_code": "XNGS",
            "type": "Common Stock",
        },
        "values": values,
        "status": status,
    }


def transport(
    *,
    body: dict | None = None,
    text: str | None = None,
    status_code: int = 200,
    requests: list | None = None,
    raises: Exception | None = None,
) -> httpx.MockTransport:
    """A transport that answers with one canned response, or fails in one canned way."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if raises is not None:
            raise raises
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=body)

    return httpx.MockTransport(handler)


def run_fetch(
    mock_transport: httpx.MockTransport,
    *,
    bars: int = 5,
    symbol: str = "NVDA",
    exchange: str = "NASDAQ",
    api_key: str | None = API_KEY,
):
    return fetch_daily_bars(
        symbol,
        exchange,
        bars,
        api_key=api_key,
        transport=mock_transport,
        now=FIXED_NOW,
    )


class RequestTests(unittest.TestCase):
    def test_sends_the_documented_parameters(self):
        requests: list = []
        run_fetch(
            transport(body=make_payload([make_bar("2026-09-17")]), requests=requests)
        )

        self.assertEqual(len(requests), 1)
        params = dict(requests[0].url.params)
        self.assertEqual(params["symbol"], "NVDA")
        self.assertEqual(params["exchange"], "NASDAQ")
        self.assertEqual(params["interval"], "1day")
        self.assertEqual(params["adjust"], "splits")
        self.assertEqual(params["order"], "desc")
        self.assertEqual(params["format"], "JSON")
        self.assertEqual(requests[0].url.path, "/time_series")

    def test_asks_for_a_allowance_beyond_the_requested_count(self):
        """The current session is dropped after the fact, so a spare bar is needed."""
        requests: list = []
        run_fetch(
            transport(body=make_payload([make_bar("2026-09-17")]), requests=requests),
            bars=30,
        )
        self.assertEqual(dict(requests[0].url.params)["outputsize"], "32")

    def test_does_not_send_a_timezone_or_decimal_places_parameter(self):
        """`timezone` is intraday-only, and `dp` would round what the provider sent."""
        requests: list = []
        run_fetch(
            transport(body=make_payload([make_bar("2026-09-17")]), requests=requests)
        )
        params = dict(requests[0].url.params)
        self.assertNotIn("timezone", params)
        self.assertNotIn("dp", params)
        self.assertNotIn("start_date", params)
        self.assertNotIn("end_date", params)

    def test_the_api_key_travels_in_the_header_and_never_in_the_url(self):
        requests: list = []
        run_fetch(
            transport(body=make_payload([make_bar("2026-09-17")]), requests=requests)
        )

        self.assertEqual(
            requests[0].headers["Authorization"], f"apikey {API_KEY}"
        )
        self.assertNotIn("apikey", requests[0].url.params)
        self.assertNotIn(API_KEY, str(requests[0].url))

    def test_returns_the_response_metadata(self):
        series = run_fetch(transport(body=make_payload([make_bar("2026-09-17")])))

        self.assertEqual(series.symbol, "NVDA")
        self.assertEqual(series.exchange, "NASDAQ")
        self.assertEqual(series.currency, "USD")
        self.assertEqual(series.provider, PROVIDER)
        self.assertEqual(series.interval, INTERVAL)
        self.assertEqual(series.adjustment_basis, ADJUSTMENT_BASIS)
        self.assertEqual(series.provider_adjust_mode, ADJUST_MODE)
        self.assertEqual(series.exchange_timezone, "America/New_York")

    def test_the_retrieval_timestamp_is_timezone_aware_utc(self):
        series = run_fetch(transport(body=make_payload([make_bar("2026-09-17")])))

        self.assertEqual(series.retrieved_at, FIXED_NOW)
        self.assertIsNotNone(series.retrieved_at.tzinfo)
        self.assertEqual(series.retrieved_at.utcoffset(), timezone.utc.utcoffset(None))

    def test_bars_are_sorted_oldest_first(self):
        """The provider answers newest-first under order=desc; the result is not."""
        series = run_fetch(
            transport(
                body=make_payload(
                    [
                        make_bar("2026-09-17"),
                        make_bar("2026-09-15"),
                        make_bar("2026-09-16"),
                    ]
                )
            )
        )

        self.assertEqual(
            [bar.trading_date for bar in series.bars],
            [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)],
        )


class DatePolicyTests(unittest.TestCase):
    def test_the_current_session_is_excluded(self):
        """Today's bar is a session still in progress, so it is not a historical fact."""
        series = run_fetch(
            transport(
                body=make_payload(
                    [make_bar("2026-09-18"), make_bar("2026-09-17")]
                )
            )
        )

        self.assertEqual(
            [bar.trading_date for bar in series.bars], [LAST_CLOSED_SESSION]
        )

    def test_future_dates_are_excluded(self):
        series = run_fetch(
            transport(
                body=make_payload(
                    [make_bar("2026-09-21"), make_bar("2026-09-17")]
                )
            )
        )

        self.assertEqual(
            [bar.trading_date for bar in series.bars], [LAST_CLOSED_SESSION]
        )

    def test_a_completed_session_is_excluded_for_the_rest_of_its_own_day(self):
        """The documented cost of the rule, asserted so it cannot drift unnoticed.

        At 14:00 New York the exchange's calendar date is still 2026-09-18, so a bar
        dated 2026-09-18 is dropped -- even though the session closed at 16:00 the
        previous day. Conservative by design.
        """
        midday = datetime(2026, 9, 18, 18, 0, tzinfo=timezone.utc)  # 14:00 in New York
        series = fetch_daily_bars(
            "NVDA",
            "NASDAQ",
            5,
            api_key=API_KEY,
            transport=transport(
                body=make_payload(
                    [make_bar("2026-09-18"), make_bar("2026-09-17")]
                )
            ),
            now=midday,
        )

        self.assertEqual(
            [bar.trading_date for bar in series.bars], [LAST_CLOSED_SESSION]
        )

    def test_the_exchange_timezone_decides_what_today_is_not_utc(self):
        """23:30 UTC is already the next day in Tokyo, and still the same day in New York."""
        late_utc = datetime(2026, 9, 18, 23, 30, tzinfo=timezone.utc)

        new_york = fetch_daily_bars(
            "NVDA",
            "NASDAQ",
            5,
            api_key=API_KEY,
            transport=transport(body=make_payload([make_bar("2026-09-18")])),
            now=late_utc,
        )
        self.assertEqual(len(new_york.bars), 0, "18th is still 'today' in New York")

        tokyo = fetch_daily_bars(
            "NVDA",
            "NASDAQ",
            5,
            api_key=API_KEY,
            transport=transport(
                body=make_payload(
                    [make_bar("2026-09-18")], exchange_timezone="Asia/Tokyo"
                )
            ),
            now=late_utc,
        )
        self.assertEqual(
            len(tokyo.bars), 1, "in Tokyo the 18th has already passed at 23:30 UTC"
        )

    def test_an_unknown_exchange_timezone_is_reported(self):
        with self.assertRaises(MalformedResponseError) as caught:
            run_fetch(
                transport(
                    body=make_payload(
                        [make_bar("2026-09-17")], exchange_timezone="Mars/Olympus"
                    )
                )
            )
        self.assertIn("Mars/Olympus", str(caught.exception))

    def test_more_available_bars_than_requested_are_trimmed_to_the_request(self):
        """The spare bars exist to cover the dropped session, not to be returned.

        A live run asked for 30 and got 31 before this was fixed.
        """
        available = [
            make_bar((date(2026, 8, 3) + timedelta(days=offset)).isoformat())
            for offset in range(18)
        ]

        series = run_fetch(transport(body=make_payload(available)), bars=5)

        self.assertEqual(series.requested_bars, 5)
        self.assertEqual(series.returned_bars, 5)
        # The most recent five, not the oldest five.
        self.assertEqual(series.bars[0].trading_date, date(2026, 8, 16))
        self.assertEqual(series.bars[-1].trading_date, date(2026, 8, 20))

    def test_returned_never_exceeds_requested(self):
        available = [
            make_bar((date(2026, 8, 3) + timedelta(days=offset)).isoformat())
            for offset in range(18)
        ]
        for asked in (1, 5, 18, 30):
            with self.subTest(asked=asked):
                series = run_fetch(transport(body=make_payload(available)), bars=asked)
                self.assertLessEqual(series.returned_bars, series.requested_bars)

    def test_fewer_bars_than_requested_are_reported_honestly(self):
        series = run_fetch(
            transport(
                body=make_payload(
                    [
                        make_bar("2026-09-17"),
                        make_bar("2026-09-16"),
                        make_bar("2026-09-15"),
                    ]
                )
            ),
            bars=30,
        )

        self.assertEqual(series.requested_bars, 30)
        self.assertEqual(series.returned_bars, 3)
        self.assertEqual(len(series.bars), 3)

    def test_a_provider_date_with_a_time_component_is_read_as_a_date(self):
        series = run_fetch(
            transport(body=make_payload([make_bar("2026-09-17 09:30:00")]))
        )

        self.assertEqual(series.bars[0].trading_date, LAST_CLOSED_SESSION)


class DecimalTests(unittest.TestCase):
    def test_prices_keep_the_exact_digits_the_provider_sent(self):
        series = run_fetch(
            transport(
                body=make_payload(
                    [
                        make_bar(
                            "2026-09-17",
                            open_="218.38000",
                            high="219.91000",
                            low="217.14999",
                            close="219.34000",
                        )
                    ]
                )
            )
        )

        bar = series.bars[0]
        self.assertEqual(bar.open, Decimal("218.38000"))
        self.assertEqual(bar.high, Decimal("219.91000"))
        self.assertEqual(bar.low, Decimal("217.14999"))
        self.assertEqual(bar.close, Decimal("219.34000"))
        # The trailing zeros survive: they are what the provider said, and dropping them
        # would misrepresent the precision on offer.
        self.assertEqual(str(bar.close), "219.34000")

    def test_a_price_with_more_decimals_than_the_column_holds_survives_unchanged(self):
        """The real artifact, taken from a live NVDA run.

        numeric(18,6) cannot hold seven decimal places. The client returns the value
        exactly as the provider sent it rather than rounding it away or refusing the
        bar: which of those is right is a storage decision, and it belongs to Step 4.
        """
        series = run_fetch(transport(body=make_payload([make_bar(**SEVEN_DP_BAR)])))

        bar = series.bars[0]
        self.assertEqual(bar.close, Decimal("225.0099945"))
        self.assertEqual(bar.low, Decimal("190.0099945"))
        # Exact through the round trip, not merely close to it.
        self.assertEqual(str(bar.close), "225.0099945")
        self.assertEqual(len(bar.close.as_tuple().digits), 10)

    def test_the_value_is_not_silently_shortened_to_the_column_scale(self):
        """Guards the specific regression: a value quietly quantised to 6 places."""
        series = run_fetch(transport(body=make_payload([make_bar(**SEVEN_DP_BAR)])))

        self.assertNotEqual(series.bars[0].close, Decimal("225.009995"))
        self.assertNotEqual(series.bars[0].close, Decimal("225.010000"))

    def test_a_price_sent_as_a_json_number_is_refused(self):
        """A number would already have been through a float before this client saw it."""
        with self.assertRaises(DataValidationError) as caught:
            run_fetch(
                transport(
                    body=make_payload([make_bar("2026-09-17", close=104.12)])
                )
            )
        self.assertIn("float", str(caught.exception))


class BarValidationTests(unittest.TestCase):
    def assert_bar_refused(self, **overrides):
        with self.assertRaises(DataValidationError) as caught:
            run_fetch(
                transport(body=make_payload([make_bar("2026-09-17", **overrides)]))
            )
        return str(caught.exception)

    def test_high_below_low_is_refused(self):
        self.assertIn("high", self.assert_bar_refused(high="98.00", open_="99.00"))

    def test_an_open_above_the_high_is_refused(self):
        self.assertIn("open", self.assert_bar_refused(high="100.00", open_="101.00"))

    def test_a_close_below_the_low_is_refused(self):
        self.assertIn("close", self.assert_bar_refused(low="100.00", close="99.00"))

    def test_a_zero_price_is_refused(self):
        self.assertIn("positive", self.assert_bar_refused(low="0.00"))

    def test_a_negative_price_is_refused(self):
        self.assertIn("positive", self.assert_bar_refused(low="-1.00"))

    def test_a_non_finite_price_is_refused(self):
        self.assertIn("finite", self.assert_bar_refused(low="NaN"))

    def test_a_missing_price_is_refused(self):
        self.assertIn("missing", self.assert_bar_refused(low=None))

    def test_a_missing_volume_is_refused_and_never_becomes_zero(self):
        message = self.assert_bar_refused(volume=None)

        self.assertIn("missing", message)
        self.assertIn("not be stored as zero", message)

    def test_an_empty_volume_is_refused(self):
        self.assertIn("missing", self.assert_bar_refused(volume=""))

    def test_a_zero_volume_is_accepted(self):
        """A halt, or a session with no trades. Zero is a reading, not a gap."""
        series = run_fetch(
            transport(body=make_payload([make_bar("2026-09-17", volume="0")]))
        )
        self.assertEqual(series.bars[0].volume, 0)

    def test_a_negative_volume_is_refused(self):
        self.assertIn("negative", self.assert_bar_refused(volume="-1"))

    def test_a_fractional_volume_is_refused(self):
        self.assertIn("whole number", self.assert_bar_refused(volume="1000.5"))

    def test_volume_is_returned_as_an_integer(self):
        series = run_fetch(
            transport(body=make_payload([make_bar("2026-09-17", volume="93960500")]))
        )
        self.assertEqual(series.bars[0].volume, 93_960_500)
        self.assertIsInstance(series.bars[0].volume, int)


class DuplicateDateTests(unittest.TestCase):
    def test_identical_duplicates_collapse(self):
        series = run_fetch(
            transport(
                body=make_payload(
                    [make_bar("2026-09-17"), make_bar("2026-09-17")]
                )
            )
        )

        self.assertEqual(series.returned_bars, 1)

    def test_conflicting_duplicates_are_refused(self):
        """Two different bars for one session: nothing can pick between them."""
        with self.assertRaises(DataValidationError) as caught:
            run_fetch(
                transport(
                    body=make_payload(
                        [
                            make_bar("2026-09-17", close="104.00"),
                            make_bar("2026-09-17", close="108.00"),
                        ]
                    )
                )
            )

        self.assertIn("2026-09-17", str(caught.exception))


class MetadataValidationTests(unittest.TestCase):
    def test_a_different_symbol_in_the_response_is_refused(self):
        with self.assertRaises(MalformedResponseError) as caught:
            run_fetch(
                transport(
                    body=make_payload([make_bar("2026-09-17")], symbol="AMD")
                )
            )
        self.assertIn("AMD", str(caught.exception))

    def test_a_different_exchange_in_the_response_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            run_fetch(
                transport(
                    body=make_payload([make_bar("2026-09-17")], exchange="NYSE")
                )
            )

    def test_a_non_daily_interval_in_the_response_is_refused(self):
        with self.assertRaises(MalformedResponseError) as caught:
            run_fetch(
                transport(
                    body=make_payload([make_bar("2026-09-17")], interval="1week")
                )
            )
        self.assertIn("1week", str(caught.exception))

    def test_a_response_without_meta_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            run_fetch(transport(body={"values": [make_bar("2026-09-17")]}))

    def test_a_response_without_values_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            run_fetch(transport(body={"meta": make_payload([])["meta"], "status": "ok"}))

    def test_a_missing_currency_is_refused(self):
        with self.assertRaises(MalformedResponseError) as caught:
            run_fetch(
                transport(
                    body=make_payload([make_bar("2026-09-17")], currency=None)
                )
            )
        self.assertIn("currency", str(caught.exception))


class ConfigurationTests(unittest.TestCase):
    def test_a_missing_key_fails_without_making_a_request(self):
        """`api_key=None` means "use the configured one", so the configuration is what
        has to be empty here -- and the transport asserts nothing was sent."""

        def refuse(request):  # pragma: no cover - reaching this is the failure
            raise AssertionError("no HTTP request may be made without an API key")

        with mock.patch("app.twelvedata._configured_api_key", return_value=None):
            with self.assertRaises(MissingApiKeyError) as caught:
                run_fetch(httpx.MockTransport(refuse), api_key=None)

        self.assertIn("TWELVE_DATA_API_KEY", str(caught.exception))

    def test_a_blank_key_fails_without_making_a_request(self):
        def refuse(request):  # pragma: no cover - reaching this is the failure
            raise AssertionError("no HTTP request may be made with a blank API key")

        with self.assertRaises(MissingApiKeyError):
            run_fetch(httpx.MockTransport(refuse), api_key="   ")

    def test_every_provider_failure_is_a_twelvedata_error(self):
        """So a caller can catch one base class and know it has covered them all."""
        cases = [
            transport(body={"code": 401, "status": "error"}, status_code=401),
            transport(body={"code": 429, "status": "error"}, status_code=429),
            transport(body={"code": 404, "status": "error"}, status_code=404),
            transport(raises=httpx.ReadTimeout("slow")),
            transport(text="nonsense", status_code=200),
        ]
        for case in cases:
            with self.subTest(case=str(case)):
                with self.assertRaises(TwelveDataError):
                    run_fetch(case)


class ArgumentValidationTests(unittest.TestCase):
    def assert_arguments_refused(self, **kwargs):
        with self.assertRaises(InvalidRequestError) as caught:
            run_fetch(transport(body=make_payload([])), **kwargs)
        return str(caught.exception)

    def test_an_empty_symbol_is_refused(self):
        self.assertIn("symbol", self.assert_arguments_refused(symbol=""))

    def test_a_blank_symbol_is_refused(self):
        self.assertIn("symbol", self.assert_arguments_refused(symbol="   "))

    def test_a_symbol_with_spaces_is_refused(self):
        self.assertIn("ticker", self.assert_arguments_refused(symbol="NV DA"))

    def test_an_empty_exchange_is_refused(self):
        self.assertIn("exchange", self.assert_arguments_refused(exchange=""))

    def test_zero_bars_is_refused(self):
        self.assertIn("between 1 and", self.assert_arguments_refused(bars=0))

    def test_more_than_the_maximum_bars_is_refused(self):
        self.assertIn(
            "between 1 and", self.assert_arguments_refused(bars=MAX_BARS + 1)
        )

    def test_a_boolean_bar_count_is_refused(self):
        """bool is an int subclass, and True would quietly mean one bar."""
        self.assertIn("whole number", self.assert_arguments_refused(bars=True))

    def test_a_lowercase_symbol_is_accepted_and_normalised(self):
        requests: list = []
        series = run_fetch(
            transport(body=make_payload([make_bar("2026-09-17")]), requests=requests),
            symbol="nvda",
        )

        self.assertEqual(series.symbol, "NVDA")
        self.assertEqual(dict(requests[0].url.params)["symbol"], "NVDA")


class ProviderFailureTests(unittest.TestCase):
    def test_an_unauthorised_key_is_reported_as_an_auth_failure(self):
        with self.assertRaises(ProviderAuthError):
            run_fetch(
                transport(
                    body={"code": 401, "message": "Invalid API key", "status": "error"},
                    status_code=401,
                )
            )

    def test_a_forbidden_plan_is_reported_as_an_auth_failure(self):
        with self.assertRaises(ProviderAuthError):
            run_fetch(
                transport(
                    body={
                        "code": 403,
                        "message": "This endpoint requires a paid plan",
                        "status": "error",
                    },
                    status_code=403,
                )
            )

    def test_an_unknown_symbol_is_reported_as_not_found(self):
        with self.assertRaises(SymbolNotFoundError):
            run_fetch(
                transport(
                    body={
                        "code": 404,
                        "message": "symbol not found: ZZZZ",
                        "status": "error",
                    },
                    status_code=404,
                )
            )

    def test_a_bad_request_about_a_missing_symbol_is_reported_as_not_found(self):
        """Twelve Data sometimes says 400 where the meaning is 'no such symbol'."""
        with self.assertRaises(SymbolNotFoundError):
            run_fetch(
                transport(
                    body={
                        "code": 400,
                        "message": "**symbol** not found: ZZZZ",
                        "status": "error",
                    },
                    status_code=400,
                )
            )

    def test_a_rate_limit_is_reported_as_itself(self):
        with self.assertRaises(RateLimitError) as caught:
            run_fetch(
                transport(
                    body={
                        "code": 429,
                        "message": "You have reached the API credits quota",
                        "status": "error",
                    },
                    status_code=429,
                )
            )

        # The message has to say no retry happened: the whole point of surfacing a rate
        # limit distinctly is that the caller knows credits were not spent in a loop.
        self.assertIn("no further credits", str(caught.exception))

    def test_a_server_error_is_reported_as_unavailable(self):
        with self.assertRaises(ProviderUnavailableError):
            run_fetch(transport(text="<html>502</html>", status_code=502))

    def test_a_read_timeout_is_reported_as_unavailable(self):
        with self.assertRaises(ProviderUnavailableError) as caught:
            run_fetch(transport(raises=httpx.ReadTimeout("read timed out")))

        self.assertIn("did not respond in time", str(caught.exception))

    def test_a_connection_failure_is_reported_as_unavailable(self):
        with self.assertRaises(ProviderUnavailableError) as caught:
            run_fetch(transport(raises=httpx.ConnectError("connection refused")))

        self.assertIn("Could not reach", str(caught.exception))

    def test_a_transport_exception_carrying_a_url_does_not_leak_it(self):
        """httpx puts the request URL in its messages. None of that text is re-raised.

        The URL is what would carry a key if the key were ever sent as a query
        parameter, so the guarantee is that the raw text never reaches the caller --
        not that it is scrubbed after the fact.
        """
        noisy = httpx.ConnectError(
            "failed: GET https://api.twelvedata.com/time_series?symbol=NVDA&apikey=X"
        )

        with self.assertRaises(ProviderUnavailableError) as caught:
            run_fetch(transport(raises=noisy))

        message = str(caught.exception)
        self.assertNotIn("https://", message)
        self.assertNotIn("time_series", message)
        self.assertNotIn("apikey=X", message)

    def test_the_original_exception_is_suppressed_not_chained(self):
        """`from None` sets __suppress_context__, which is what stops a traceback from
        printing the transport exception underneath -- and the URL inside it.

        __context__ itself still holds the original, because Python always records it;
        suppressing it is the mechanism, and that is what is asserted here.
        """
        with self.assertRaises(ProviderUnavailableError) as caught:
            run_fetch(transport(raises=httpx.ConnectError("refused")))

        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_an_error_delivered_under_http_200_is_still_an_error(self):
        """The status code says 200; the payload says otherwise, and the payload wins."""
        with self.assertRaises(ProviderAuthError):
            run_fetch(
                transport(
                    body={
                        "code": 401,
                        "message": "Invalid API key",
                        "status": "error",
                    },
                    status_code=200,
                )
            )

    def test_a_body_that_is_not_json_is_reported(self):
        with self.assertRaises((MalformedResponseError, ProviderUnavailableError)):
            run_fetch(transport(text="<html>not json at all</html>", status_code=200))

    def test_a_json_body_that_is_not_an_object_is_reported(self):
        with self.assertRaises(MalformedResponseError):
            run_fetch(transport(text="[1, 2, 3]", status_code=200))

class RedactionTests(unittest.TestCase):
    def test_a_key_echoed_back_in_a_provider_message_is_removed(self):
        with self.assertRaises(ProviderAuthError) as caught:
            run_fetch(
                transport(
                    body={
                        "code": 401,
                        "message": f"Invalid API key {API_KEY} supplied",
                        "status": "error",
                    },
                    status_code=401,
                )
            )

        self.assertNotIn(API_KEY, str(caught.exception))
        self.assertIn("***", str(caught.exception))

    def test_redact_replaces_every_occurrence(self):
        self.assertEqual(
            redact(f"{API_KEY} and again {API_KEY}", API_KEY), "*** and again ***"
        )

    def test_redact_ignores_an_absent_secret(self):
        self.assertEqual(redact("nothing to hide", None), "nothing to hide")
        self.assertEqual(redact("nothing to hide", "   "), "nothing to hide")

    def test_no_error_message_contains_the_url_or_the_key(self):
        """The strongest form of the guarantee: across every failure mode at once."""
        cases = [
            transport(body={"code": 401, "status": "error"}, status_code=401),
            transport(body={"code": 403, "status": "error"}, status_code=403),
            transport(body={"code": 404, "status": "error"}, status_code=404),
            transport(body={"code": 429, "status": "error"}, status_code=429),
            transport(body={"code": 500, "status": "error"}, status_code=500),
            transport(raises=httpx.ReadTimeout("read timed out")),
            transport(raises=httpx.ConnectError("connection refused")),
            transport(text="not json", status_code=200),
            transport(body=make_payload([make_bar("2026-09-17", high="98.00")])),
            transport(body=make_payload([make_bar("2026-09-17", volume=None)])),
        ]
        for case in cases:
            with self.subTest(case=str(case)):
                with self.assertRaises(TwelveDataError) as caught:
                    run_fetch(case)
                message = str(caught.exception)
                self.assertNotIn(API_KEY, message)
                # A URL is what would carry a key if it were ever sent as a query
                # parameter. Naming the bare host is fine and useful; a full URL is not.
                self.assertNotIn("https://", message)
                self.assertNotIn("time_series", message)


if __name__ == "__main__":
    unittest.main()