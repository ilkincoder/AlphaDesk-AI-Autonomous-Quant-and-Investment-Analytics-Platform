"""The read-only fetch command's contract.

What is checked here is the command's own behaviour -- the JSON it prints, the exit code
it returns, and what it refuses to print -- with `fetch` replaced, so none of it reaches
the network or depends on a live quote.

The client's behaviour is covered in test_twelvedata.py.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import contextlib
import io
import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

from app.fetch_prices import SAMPLE_BARS, main
from app.twelvedata import (
    ADJUST_MODE,
    ADJUSTMENT_BASIS,
    DEFAULT_BARS,
    INTERVAL,
    PROVIDER,
    DailyBar,
    DailyPriceSeries,
    MissingApiKeyError,
    RateLimitError,
)

API_KEY = "test-key-not-a-real-secret"
RETRIEVED_AT = datetime(2026, 9, 18, 21, 0, tzinfo=timezone.utc)

# Eight closed sessions, skipping the 12th and 13th so the fixture does not pretend
# weekends are trading days.
SESSION_DATES = [
    date(2026, 9, day) for day in (8, 9, 10, 11, 14, 15, 16, 17)
]


def make_series(bars: int = len(SESSION_DATES)) -> DailyPriceSeries:
    return DailyPriceSeries(
        symbol="NVDA",
        exchange="NASDAQ",
        currency="USD",
        exchange_timezone="America/New_York",
        provider=PROVIDER,
        interval=INTERVAL,
        adjustment_basis=ADJUSTMENT_BASIS,
        provider_adjust_mode=ADJUST_MODE,
        retrieved_at=RETRIEVED_AT,
        requested_bars=30,
        bars=tuple(
            DailyBar(
                trading_date=session,
                open=Decimal("214.14000"),
                high=Decimal("216.75999"),
                low=Decimal("212.50000"),
                close=Decimal("213.89999"),
                volume=96_563_600,
            )
            for session in SESSION_DATES[:bars]
        ),
    )


def invoke(argv: list[str], *, series=None, error: Exception | None = None):
    """Run the command with the network replaced, capturing both output streams.

    `_configured_key` is replaced as well, so the command's own redaction can be
    observed against a key this test knows. The container's real key is not the one
    these fixtures put into error messages, so leaving it in place would make the
    redaction test pass without proving anything.
    """
    stdout, stderr = io.StringIO(), io.StringIO()

    with mock.patch("app.fetch_prices.fetch") as patched, mock.patch(
        "app.fetch_prices._configured_key", return_value=API_KEY
    ):
        if error is not None:
            patched.side_effect = error
        else:
            patched.return_value = series if series is not None else make_series()

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)

    return code, stdout.getvalue(), stderr.getvalue(), patched


class SuccessTests(unittest.TestCase):
    def test_prints_json_and_exits_zero(self):
        code, out, err, _ = invoke([])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["symbol"], "NVDA")

    def test_uses_the_documented_defaults(self):
        _, _, _, patched = invoke([])

        patched.assert_called_once_with("NVDA", "NASDAQ", DEFAULT_BARS)

    def test_passes_arguments_through(self):
        argv = ["--symbol", "AMD", "--exchange", "NYSE", "--bars", "12"]

        _, _, _, patched = invoke(argv)

        patched.assert_called_once_with("AMD", "NYSE", 12)

    def test_reports_the_identity_and_policies(self):
        _, out, _, _ = invoke([])
        document = json.loads(out)

        self.assertEqual(document["symbol"], "NVDA")
        self.assertEqual(document["exchange"], "NASDAQ")
        self.assertEqual(document["currency"], "USD")
        self.assertEqual(document["provider"], PROVIDER)
        self.assertEqual(document["interval"], INTERVAL)
        self.assertEqual(document["adjustment_basis"], ADJUSTMENT_BASIS)
        self.assertEqual(document["provider_adjust_mode"], ADJUST_MODE)
        self.assertEqual(document["exchange_timezone"], "America/New_York")

    def test_reports_the_retrieval_time_as_an_iso_timestamp(self):
        _, out, _, _ = invoke([])

        self.assertEqual(
            json.loads(out)["retrieved_at"], "2026-09-18T21:00:00+00:00"
        )

    def test_reports_both_bar_counts_and_the_date_range(self):
        _, out, _, _ = invoke([])
        document = json.loads(out)

        self.assertEqual(document["requested_bars"], 30)
        self.assertEqual(document["returned_bars"], 8)
        self.assertEqual(document["first_date"], "2026-09-08")
        self.assertEqual(document["last_date"], "2026-09-17")


class DecimalSerialisationTests(unittest.TestCase):
    def test_prices_are_json_strings_not_numbers(self):
        """A JSON number is a double, and parsing one back would lose the precision."""
        _, out, _, _ = invoke([])
        sample = json.loads(out)["sample_bars"][0]

        for field in ("open", "high", "low", "close"):
            self.assertIsInstance(sample[field], str, f"{field} must be a string")

    def test_the_exact_digits_survive_the_round_trip(self):
        _, out, _, _ = invoke([])
        sample = json.loads(out)["sample_bars"][0]

        self.assertEqual(sample["open"], "214.14000")
        self.assertEqual(sample["high"], "216.75999")
        self.assertEqual(sample["low"], "212.50000")
        self.assertEqual(sample["close"], "213.89999")

    def test_volume_is_a_json_number_not_a_string(self):
        """Volume is a count of shares, so it stays an integer."""
        _, out, _, _ = invoke([])
        sample = json.loads(out)["sample_bars"][0]

        self.assertIsInstance(sample["volume"], int)
        self.assertEqual(sample["volume"], 96_563_600)

    def test_a_price_beyond_the_column_scale_is_printed_unchanged(self):
        """`225.0099945` is a real value from a live NVDA run.

        The command is the last place this could be quietly rounded, and it is not.
        """
        series = make_series(bars=1)
        artifact = replace(
            series, bars=(replace(series.bars[0], close=Decimal("225.0099945")),)
        )

        _, out, _, _ = invoke([], series=artifact)
        printed = json.loads(out)["sample_bars"][0]["close"]

        self.assertEqual(printed, "225.0099945")
        self.assertIsInstance(printed, str)

    def test_dates_are_plain_calendar_dates(self):
        """The sample is the tail of the series, so its first entry is not the first
        session overall -- the range is reported separately for that."""
        _, out, _, _ = invoke([])
        document = json.loads(out)

        self.assertEqual(document["sample_bars"][0]["date"], "2026-09-11")
        self.assertEqual(document["first_date"], "2026-09-08")


class SampleTests(unittest.TestCase):
    def test_only_a_few_bars_are_shown(self):
        _, out, _, _ = invoke([])

        self.assertEqual(len(json.loads(out)["sample_bars"]), SAMPLE_BARS)

    def test_the_sample_is_the_most_recent_bars(self):
        _, out, _, _ = invoke([])
        sample = json.loads(out)["sample_bars"]

        self.assertEqual(
            [bar["date"] for bar in sample],
            [session.isoformat() for session in SESSION_DATES[-SAMPLE_BARS:]],
        )
        self.assertEqual(sample[-1]["date"], "2026-09-17")

    def test_an_empty_series_still_prints_and_exits_zero(self):
        """Zero completed sessions is a result, not a crash. It is reported as it is."""
        code, out, err, _ = invoke([], series=make_series(bars=0))
        document = json.loads(out)

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(document["returned_bars"], 0)
        self.assertIsNone(document["first_date"])
        self.assertIsNone(document["last_date"])
        self.assertEqual(document["sample_bars"], [])


class FailureTests(unittest.TestCase):
    def test_a_provider_failure_exits_nonzero(self):
        code, out, err, _ = invoke([], error=RateLimitError("quota exhausted"))

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("quota exhausted", err)

    def test_the_failure_message_is_labelled_as_an_error(self):
        _, _, err, _ = invoke([], error=MissingApiKeyError("no key configured"))

        self.assertTrue(err.startswith("error: "), err)

    def test_a_failure_prints_no_traceback(self):
        """A traceback is exactly where a leaked request URL would surface."""
        _, _, err, _ = invoke([], error=RateLimitError("quota exhausted"))

        self.assertNotIn("Traceback", err)
        self.assertNotIn("File \"", err)

    def test_no_key_material_reaches_either_stream(self):
        code, out, err, _ = invoke([], error=MissingApiKeyError(f"key {API_KEY} is bad"))

        self.assertEqual(code, 1)
        # The message here deliberately contains the key, standing in for a provider
        # that echoes one back. Whatever the client does with it, the command must not
        # be the thing that finally prints it.
        self.assertNotIn(API_KEY, out)
        self.assertNotIn(API_KEY, err)


if __name__ == "__main__":
    unittest.main()