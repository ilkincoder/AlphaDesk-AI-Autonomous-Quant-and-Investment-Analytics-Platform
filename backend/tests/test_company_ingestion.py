"""Company identity, and the refusals that keep a run pointed at the right issuer.

Most of this needs no database and no network: identity is validated from values the caller
supplied and from what the providers report, both of which are fixtures here. The rules that
depend on what is already stored use the isolated database.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.company_ingestion import (
    CompanyIdentity,
    build_dataset,
    check_sources_agree,
    check_stored_company,
    parse_identity,
)
from app.ingestion import IngestionIdentityError
from app.models import Company
from app.sec_edgar import DiscoveryResult, InvalidRequestError
from app.twelvedata import DailyBar, DailyPriceSeries
from tests.testdb import test_engine

NVDA = CompanyIdentity(symbol="NVDA", exchange="NASDAQ", cik="0001045810")
APPLE = CompanyIdentity(symbol="AAPL", exchange="NASDAQ", cik="0000320193")


def series(symbol: str = "AAPL", exchange: str = "NASDAQ") -> DailyPriceSeries:
    return DailyPriceSeries(
        symbol=symbol,
        exchange=exchange,
        currency="USD",
        exchange_timezone="America/New_York",
        provider="twelve_data",
        interval="1day",
        adjustment_basis="adjusted",
        provider_adjust_mode="splits",
        retrieved_at=datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc),
        requested_bars=1,
        bars=(
            DailyBar(
                trading_date=date(2026, 9, 17),
                open=Decimal("100"), high=Decimal("101"),
                low=Decimal("99"), close=Decimal("100"),
                volume=1000,
            ),
        ),
    )


def discovery(
    *,
    name: str = "Apple Inc.",
    tickers: tuple[str, ...] = ("AAPL",),
    exchanges: tuple[str, ...] = ("Nasdaq",),
) -> DiscoveryResult:
    return DiscoveryResult(
        issuer_cik="0000320193",
        issuer_name=name,
        tickers=tickers,
        exchanges=exchanges,
        recent_filings_scanned=1001,
        older_filing_files_not_searched=1478,
        filings=(),
    )


class ParseIdentityTests(unittest.TestCase):
    def test_a_short_cik_is_padded_to_ten_digits(self):
        """What preserves a leading zero: 320193 and 0000320193 are the same issuer."""
        self.assertEqual(parse_identity("aapl", "nasdaq", "320193").cik, "0000320193")

    def test_an_already_padded_cik_is_unchanged(self):
        self.assertEqual(parse_identity("AAPL", "NASDAQ", "0000320193").cik, "0000320193")

    def test_symbol_and_exchange_are_normalized(self):
        identity = parse_identity(" aapl ", "nasdaq ", "0000320193")

        self.assertEqual(identity.symbol, "AAPL")
        self.assertEqual(identity.exchange, "NASDAQ")

    def test_an_empty_symbol_is_refused(self):
        for value in ("", "   "):
            with self.subTest(value=repr(value)):
                with self.assertRaises(IngestionIdentityError):
                    parse_identity(value, "NASDAQ", "0000320193")

    def test_an_empty_exchange_is_refused(self):
        with self.assertRaises(IngestionIdentityError):
            parse_identity("AAPL", "  ", "0000320193")

    def test_a_non_numeric_cik_is_refused(self):
        with self.assertRaises(InvalidRequestError):
            parse_identity("AAPL", "NASDAQ", "AAPL")

    def test_an_over_long_cik_is_refused(self):
        with self.assertRaises(InvalidRequestError):
            parse_identity("AAPL", "NASDAQ", "00003201930")


class SourceAgreementTests(unittest.TestCase):
    def test_agreeing_sources_pass(self):
        check_sources_agree(APPLE, series=series(), discovery=discovery())

    def test_a_price_series_for_another_symbol_is_refused(self):
        with self.assertRaises(IngestionIdentityError) as caught:
            check_sources_agree(APPLE, series=series(symbol="MSFT"), discovery=discovery())

        self.assertIn("MSFT", str(caught.exception))
        self.assertIn("AAPL", str(caught.exception))

    def test_a_price_series_on_another_exchange_is_refused(self):
        with self.assertRaises(IngestionIdentityError):
            check_sources_agree(APPLE, series=series(exchange="NYSE"), discovery=discovery())

    def test_a_ticker_the_issuer_does_not_report_is_refused(self):
        with self.assertRaises(IngestionIdentityError) as caught:
            check_sources_agree(APPLE, series=series(), discovery=discovery(tickers=("MSFT",)))

        message = str(caught.exception)
        self.assertIn("MSFT", message, "the message must say what the CIK does report")
        self.assertIn("Apple Inc.", message)

    def test_an_issuer_reporting_no_tickers_is_refused(self):
        with self.assertRaises(IngestionIdentityError):
            check_sources_agree(APPLE, series=series(), discovery=discovery(tickers=()))

    def test_the_exchange_comparison_ignores_case(self):
        """EDGAR writes "Nasdaq"; the command is given "NASDAQ"."""
        check_sources_agree(
            APPLE, series=series(), discovery=discovery(exchanges=("Nasdaq",))
        )

    def test_a_different_exchange_is_refused(self):
        with self.assertRaises(IngestionIdentityError) as caught:
            check_sources_agree(
                APPLE, series=series(), discovery=discovery(exchanges=("NYSE",))
            )

        self.assertIn("NYSE", str(caught.exception))

    def test_an_issuer_with_several_listings_is_refused(self):
        """Alphabet's shape: four tickers under one CIK, which the schema cannot hold.

        `companies` is unique on `sec_issuer_cik`, so a second listing would have to replace
        the first. Refused against the feed, so even a first attempt fails before anything
        exists to overwrite.
        """
        with self.assertRaises(IngestionIdentityError) as caught:
            check_sources_agree(
                CompanyIdentity(symbol="GOOGL", exchange="NASDAQ", cik="0001652044"),
                series=series(symbol="GOOGL"),
                discovery=discovery(
                    name="Alphabet Inc.",
                    tickers=("GOOGL", "GOOG", "GOOGM", "GOOGN"),
                    exchanges=("Nasdaq", "Nasdaq", "Nasdaq", "Nasdaq"),
                ),
            )

        message = str(caught.exception)
        for ticker in ("GOOG", "GOOGM", "GOOGN"):
            self.assertIn(ticker, message, "every listing must be named")
        self.assertIn("one listing per issuer", message)


class _FakeSecClient:
    """Answers discovery from a fixture, and records the order it was asked in."""

    def __init__(self, discovery_result: DiscoveryResult, log: list[str]) -> None:
        self._discovery = discovery_result
        self._log = log

    def discover(self, cik: str, limit: int) -> DiscoveryResult:
        self._log.append("sec:discover")
        return self._discovery

    def fetch_filings(self, discovery: DiscoveryResult) -> tuple:
        self._log.append("sec:filings")
        return ()


class BuildDatasetOrderTests(unittest.TestCase):
    """The SEC is asked, and its answer checked, before the price provider is called.

    An issuer this schema cannot hold must not spend a request from the price quota to find
    that out, which is why the two checks are interleaved with the two fetches rather than
    run together at the end.
    """

    def build(self, identity: CompanyIdentity, *, sec_discovery: DiscoveryResult, log: list):
        def price_request(symbol: str, exchange: str, bars: int) -> DailyPriceSeries:
            log.append("prices")
            return series(symbol=symbol, exchange=exchange)

        with mock.patch("app.company_ingestion.fetch_daily_bars", price_request):
            return build_dataset(
                _FakeSecClient(sec_discovery, log), identity=identity, bars=7, filings=3
            )

    def test_the_sec_is_asked_before_the_price_provider(self):
        log: list[str] = []

        dataset = self.build(APPLE, sec_discovery=discovery(), log=log)

        self.assertEqual(log, ["sec:discover", "prices", "sec:filings"])
        self.assertEqual(dataset.identity, APPLE)

    def test_a_refused_issuer_costs_no_price_request(self):
        """Alphabet's shape: refused from SEC metadata, so the price provider is never asked."""
        log: list[str] = []

        with self.assertRaises(IngestionIdentityError):
            self.build(
                CompanyIdentity(symbol="GOOGL", exchange="NASDAQ", cik="0001652044"),
                sec_discovery=discovery(
                    name="Alphabet Inc.",
                    tickers=("GOOGL", "GOOG", "GOOGM", "GOOGN"),
                    exchanges=("Nasdaq", "Nasdaq", "Nasdaq", "Nasdaq"),
                ),
                log=log,
            )

        self.assertNotIn("prices", log)


class StoredCompanyTests(unittest.TestCase):
    """The rules that depend on what is already in the database."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def setUp(self) -> None:
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)

    def tearDown(self) -> None:
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def add_company(self, ticker: str, cik: str) -> None:
        self.session.add(
            Company(
                name=f"{ticker} Inc.", ticker=ticker, exchange="NASDAQ",
                currency="USD", sec_issuer_cik=cik,
            )
        )
        self.session.flush()

    def test_an_empty_database_accepts_anything(self):
        check_stored_company(self.session, APPLE)

    def test_the_same_company_twice_is_a_re_run(self):
        self.add_company("AAPL", "0000320193")

        check_stored_company(self.session, APPLE)

    def test_a_second_listing_for_a_stored_issuer_is_refused(self):
        self.add_company("NVDA", "0001045810")

        with self.assertRaises(IngestionIdentityError) as caught:
            check_stored_company(
                self.session,
                CompanyIdentity(symbol="NVDA.A", exchange="NASDAQ", cik="0001045810"),
            )

        message = str(caught.exception)
        self.assertIn("NVDA", message)
        self.assertIn("one listing per issuer", message)

    def test_re_pointing_a_stored_ticker_at_another_issuer_is_refused(self):
        self.add_company("AAPL", "0000320193")

        with self.assertRaises(IngestionIdentityError) as caught:
            check_stored_company(
                self.session,
                CompanyIdentity(symbol="AAPL", exchange="NASDAQ", cik="0001045810"),
            )

        message = str(caught.exception)
        self.assertIn("0000320193", message)
        self.assertIn("0001045810", message)

    def test_a_refusal_writes_nothing(self):
        self.add_company("AAPL", "0000320193")

        with self.assertRaises(IngestionIdentityError):
            check_stored_company(
                self.session,
                CompanyIdentity(symbol="AAPL", exchange="NASDAQ", cik="0001045810"),
            )

        self.session.rollback()
        self.assertEqual(
            self.session.scalar(select(Company).where(Company.ticker == "NVDA")), None
        )


if __name__ == "__main__":
    unittest.main()
