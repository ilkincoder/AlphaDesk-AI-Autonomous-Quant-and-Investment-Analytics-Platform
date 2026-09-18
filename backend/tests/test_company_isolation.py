"""Adding a second company must leave the first one exactly as it was.

Every table that holds company data is keyed by `company_id`, and every command resolves its
company by ticker — so isolation is mostly a consequence of the schema rather than something
the code works at. But "mostly a consequence" is how a shared key becomes a shared row, and
the failure would be silent: an Apple run quietly rewriting an NVIDIA price, or a search
returning the other company's filings.

Tested against the isolated database, inside a transaction that is rolled back.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingestion import ingest
from app.models import Company, DailyPrice, IngestionRun, InsiderTransaction, SecFiling
from app.sec_edgar import DiscoveryResult, SecFilingRef
from app.twelvedata import DailyBar, DailyPriceSeries
from tests.testdb import test_engine

NVDA_CIK = "0001045810"
AAPL_CIK = "0000320193"
ACCEPTED = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)


def series(symbol: str, cik: str, close: str) -> DailyPriceSeries:
    return DailyPriceSeries(
        symbol=symbol,
        exchange="NASDAQ",
        currency="USD",
        exchange_timezone="America/New_York",
        provider="twelve_data",
        interval="1day",
        adjustment_basis="adjusted",
        provider_adjust_mode="splits",
        retrieved_at=ACCEPTED,
        requested_bars=1,
        bars=(
            DailyBar(
                trading_date=date(2026, 9, 17),
                open=Decimal(close), high=Decimal(close),
                low=Decimal(close), close=Decimal(close),
                volume=1000,
            ),
        ),
    )


def discovery(symbol: str, cik: str, name: str) -> DiscoveryResult:
    return DiscoveryResult(
        issuer_cik=cik,
        issuer_name=name,
        tickers=(symbol,),
        exchanges=("Nasdaq",),
        recent_filings_scanned=1001,
        older_filing_files_not_searched=1478,
        filings=(),
    )


class CompanyIsolationTests(unittest.TestCase):
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

    def ingest(self, symbol: str, cik: str, name: str, close: str) -> None:
        ingest(
            self.session,
            series=series(symbol, cik, close),
            discovery=discovery(symbol, cik, name),
            records=(),
            parameters={"ticker": symbol},
            started_at=ACCEPTED,
        )

    def snapshot(self) -> dict:
        """Every row that belongs to a company, as plain values."""
        def rows(model, *columns):
            return [
                tuple(row)
                for row in self.session.execute(select(*columns).order_by(model.id)).all()
            ]

        return {
            "companies": rows(Company, Company.id, Company.ticker, Company.sec_issuer_cik),
            "prices": rows(DailyPrice, DailyPrice.id, DailyPrice.company_id,
                           DailyPrice.trading_date, DailyPrice.close),
            "filings": rows(SecFiling, SecFiling.id, SecFiling.company_id,
                            SecFiling.accession_number),
            "runs": rows(IngestionRun, IngestionRun.id, IngestionRun.company_id,
                         IngestionRun.scope),
        }

    def test_a_second_company_leaves_the_first_untouched(self):
        self.ingest("NVDA", NVDA_CIK, "NVIDIA CORP", "150.00")
        before = self.snapshot()

        self.ingest("AAPL", AAPL_CIK, "Apple Inc.", "230.00")

        after = self.snapshot()
        self.assertEqual(before["companies"], after["companies"][: len(before["companies"])])
        self.assertEqual(before["prices"], after["prices"][: len(before["prices"])])
        self.assertEqual(before["filings"], after["filings"][: len(before["filings"])])

    def test_row_ids_are_not_reused_across_companies(self):
        """The strongest form of "nothing was rewritten": the ids are still distinct."""
        self.ingest("NVDA", NVDA_CIK, "NVIDIA CORP", "150.00")
        self.ingest("AAPL", AAPL_CIK, "Apple Inc.", "230.00")

        ids = self.session.scalars(select(DailyPrice.id).order_by(DailyPrice.id)).all()
        self.assertEqual(len(ids), len(set(ids)))

    def test_each_run_is_recorded_against_its_own_company(self):
        self.ingest("NVDA", NVDA_CIK, "NVIDIA CORP", "150.00")
        self.ingest("AAPL", AAPL_CIK, "Apple Inc.", "230.00")

        by_ticker = dict(
            self.session.execute(
                select(Company.ticker, func.count(IngestionRun.id))
                .join(IngestionRun, IngestionRun.company_id == Company.id)
                .group_by(Company.ticker)
            ).all()
        )
        self.assertEqual(by_ticker, {"NVDA": 1, "AAPL": 1})

    def test_prices_are_scoped_to_their_company(self):
        self.ingest("NVDA", NVDA_CIK, "NVIDIA CORP", "150.00")
        self.ingest("AAPL", AAPL_CIK, "Apple Inc.", "230.00")

        nvda = self.session.scalar(
            select(DailyPrice.close).join(Company).where(Company.ticker == "NVDA")
        )
        aapl = self.session.scalar(
            select(DailyPrice.close).join(Company).where(Company.ticker == "AAPL")
        )
        self.assertEqual(nvda, Decimal("150.00"))
        self.assertEqual(aapl, Decimal("230.00"))

    def test_a_transaction_belongs_to_one_company_only(self):
        """`insider_transactions` hangs off a filing, and filings off a company."""
        self.ingest("NVDA", NVDA_CIK, "NVIDIA CORP", "150.00")
        nvda_filings = self.session.scalars(
            select(SecFiling.id).join(Company).where(Company.ticker == "NVDA")
        ).all()

        self.ingest("AAPL", AAPL_CIK, "Apple Inc.", "230.00")

        self.assertEqual(
            self.session.scalar(
                select(func.count())
                .select_from(InsiderTransaction)
                .where(InsiderTransaction.filing_id.in_(nvda_filings))
            ),
            0,
        )

    def test_re_running_one_company_does_not_touch_the_other(self):
        self.ingest("NVDA", NVDA_CIK, "NVIDIA CORP", "150.00")
        self.ingest("AAPL", AAPL_CIK, "Apple Inc.", "230.00")
        aapl_before = self.session.scalar(
            select(DailyPrice.close).join(Company).where(Company.ticker == "AAPL")
        )

        self.ingest("NVDA", NVDA_CIK, "NVIDIA CORP", "150.00")

        self.assertEqual(
            self.session.scalar(
                select(DailyPrice.close).join(Company).where(Company.ticker == "AAPL")
            ),
            aapl_before,
        )
        self.assertEqual(
            self.session.scalar(
                select(func.count()).select_from(DailyPrice).join(Company).where(
                    Company.ticker == "AAPL"
                )
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
