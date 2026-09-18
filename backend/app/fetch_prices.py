"""Fetch recent daily bars for one stock from Twelve Data. Read-only.

Prints what it fetched, then exits. It opens no database session, writes no rows, and
touches no portfolio. It is not wired into application startup.

Persisting bars into `daily_prices` is Step 4, after the SEC EDGAR client in Step 3.
This command exists so the client can be exercised by hand, against the real provider,
before anything depends on it.

    docker compose exec backend python -m app.fetch_prices
    docker compose exec backend python -m app.fetch_prices --symbol NVDA --exchange NASDAQ --bars 30

Every decimal is printed as a JSON **string**, never a JSON number, for the same reason
the API schemas are: a JSON number is an IEEE-754 double, and parsing one back would
reintroduce exactly the precision loss `Decimal` and `NUMERIC(18,6)` exist to avoid.
"""

import argparse
import json
import sys

from app.twelvedata import (
    DEFAULT_BARS,
    DailyBar,
    DailyPriceSeries,
    TwelveDataError,
    redact,
)

# How many bars to show. The full series is not printed -- this command answers "does a
# real fetch work, and what does the data look like", not "dump the series".
SAMPLE_BARS = 5

EXIT_OK = 0
EXIT_FAILED = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.fetch_prices",
        description=(
            "Fetch recent daily OHLCV bars for one US stock from Twelve Data and print "
            "them. Read-only: nothing is stored."
        ),
    )
    parser.add_argument("--symbol", default="NVDA", help="ticker (default: NVDA)")
    parser.add_argument(
        "--exchange", default="NASDAQ", help="exchange the ticker lists on (default: NASDAQ)"
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=DEFAULT_BARS,
        help=f"how many completed bars to ask for (default: {DEFAULT_BARS})",
    )
    args = parser.parse_args(argv)

    try:
        series = fetch(args.symbol, args.exchange, args.bars)
    except TwelveDataError as exc:
        # Only anticipated failures are caught. Every one of these already carries a
        # message that has been redacted and stripped of URLs, so printing it is safe --
        # and printing it *instead of* a traceback is the point: a traceback is where a
        # leaked request URL would surface.
        #
        # A genuine bug is deliberately left to raise, because a crash that hides itself
        # behind "something went wrong" is harder to fix than one that shows its trace.
        #
        # The key is scrubbed a second time here, on top of the client's own redaction.
        # This is the last point before a person reads the text, and it is the command's
        # own output -- so it does not delegate the guarantee to a module it does not
        # control the future of.
        print(f"error: {redact(str(exc), _configured_key())}", file=sys.stderr)
        return EXIT_FAILED

    json.dump(_document(series), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


def fetch(symbol: str, exchange: str, bars: int) -> DailyPriceSeries:
    """Split out so tests can replace it without reaching the network."""
    from app.twelvedata import fetch_daily_bars

    return fetch_daily_bars(symbol, exchange, bars)


def _configured_key() -> str | None:
    """The configured key, used only to scrub it back out of anything printed.

    Imported here rather than at module level so this command stays runnable, and its
    helpers importable, without a database URL in the environment.
    """
    from app.config import settings

    return settings.twelve_data_api_key


def _document(series: DailyPriceSeries) -> dict:
    bars = series.bars
    return {
        "symbol": series.symbol,
        "exchange": series.exchange,
        "currency": series.currency,
        "provider": series.provider,
        "interval": series.interval,
        "adjustment_basis": series.adjustment_basis,
        "provider_adjust_mode": series.provider_adjust_mode,
        "exchange_timezone": series.exchange_timezone,
        "retrieved_at": series.retrieved_at.isoformat(),
        "requested_bars": series.requested_bars,
        "returned_bars": series.returned_bars,
        "first_date": bars[0].trading_date.isoformat() if bars else None,
        "last_date": bars[-1].trading_date.isoformat() if bars else None,
        "sample_bars": [_bar_document(bar) for bar in bars[-SAMPLE_BARS:]],
    }


def _bar_document(bar: DailyBar) -> dict:
    # str() on each Decimal, rather than letting json fall back to something. The
    # conversion to text is a decision, not an accident of serialisation.
    return {
        "date": bar.trading_date.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
    }


if __name__ == "__main__":
    raise SystemExit(main())