"""The analyze_insiders command, against a real PostgreSQL.

The rules are covered in test_analysis.py, from fixtures. What is checked here is the part
fixtures cannot reach: the database reads. In particular that transactions are read without
joining owners, which is the difference between counting a trade once and counting it once
per person who signed the filing.

Every test seeds rows inside a transaction that is rolled back, so `alphadesk_test` is left
exactly as it was found.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import contextlib
import io
import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analyze_insiders import main
from app.models import (
    Company,
    DailyPrice,
    IngestionRun,
    InsiderReportingOwner,
    InsiderTransaction,
    SecFiling,
)
from tests.testdb import test_engine

CIK = "0001045810"
TICKER = "NVDA"
ACCEPTED = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)


class SessionProxy:
    """Hands the command the test's own session without closing it afterwards.

    Closing it would return the connection the outer rollback depends on, so the command's
    `with SessionLocal() as session:` gets this instead.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *exc_info) -> bool:
        return False


class AnalyzeCommandTestCase(unittest.TestCase):
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

    # --- seeding ---------------------------------------------------------------------

    def add_company(self, ticker: str = TICKER, cik: str = CIK) -> Company:
        company = Company(
            name="NVIDIA CORP",
            ticker=ticker,
            exchange="NASDAQ",
            currency="USD",
            sec_issuer_cik=cik,
        )
        self.session.add(company)
        self.session.flush()
        return company

    def add_price(
        self,
        company: Company,
        trading_date: date,
        close: str,
        *,
        provider: str = "twelve_data",
        basis: str = "adjusted",
        mode: str = "splits",
    ) -> None:
        self.session.add(
            DailyPrice(
                company_id=company.id,
                trading_date=trading_date,
                open=Decimal(close),
                high=Decimal(close),
                low=Decimal(close),
                close=Decimal(close),
                volume=1000,
                provider=provider,
                currency="USD",
                adjustment_basis=basis,
                provider_adjust_mode=mode,
            )
        )
        self.session.flush()

    def add_filing(
        self,
        company: Company,
        accession: str = "0001199039-26-000014",
        *,
        acceptance: datetime | None = ACCEPTED,
        is_amendment: bool = False,
        filing_date: date = date(2026, 9, 8),
    ) -> SecFiling:
        filing = SecFiling(
            company_id=company.id,
            accession_number=accession,
            form_type="4/A" if is_amendment else "4",
            filing_date=filing_date,
            acceptance_datetime=acceptance,
            source_document_url=f"https://www.sec.gov/Archives/edgar/data/1045810/{accession}.xml",
            is_amendment=is_amendment,
            document_type="4/A" if is_amendment else "4",
            holding_rows_skipped=0,
            footnotes={},
            source_xml="<ownershipDocument/>",
            source_xml_sha256="0" * 64,
        )
        self.session.add(filing)
        self.session.flush()
        return filing

    def add_owner(self, filing: SecFiling, cik: str, name: str) -> None:
        self.session.add(
            InsiderReportingOwner(
                filing_id=filing.id,
                reporting_owner_cik=cik,
                owner_name=name,
            )
        )
        self.session.flush()

    def add_transaction(
        self,
        filing: SecFiling,
        *,
        row_position: int = 0,
        code: str = "S",
        direction: str = "D",
        shares: str = "100",
        price: str | None = "200",
        transaction_date: date = date(2026, 9, 3),
        security_title: str = "Common Stock",
        source_table: str = "nonDerivativeTable",
    ) -> None:
        self.session.add(
            InsiderTransaction(
                filing_id=filing.id,
                source_table=source_table,
                row_position=row_position,
                is_derivative=source_table == "derivativeTable",
                transaction_date=transaction_date,
                security_title=security_title,
                transaction_code=code,
                acquired_disposed=direction,
                shares=Decimal(shares),
                price_per_share=None if price is None else Decimal(price),
                footnote_refs=[],
            )
        )
        self.session.flush()

    def add_run(self, company: Company) -> None:
        self.session.add(
            IngestionRun(
                company_id=company.id,
                started_at=ACCEPTED,
                completed_at=ACCEPTED,
                parameters={"bars": 30, "filings": 3},
                summary={
                    "filings": {
                        "requested_filings": 3,
                        "returned_filings": 3,
                        "discovery_scope": "recent submissions list only",
                    }
                },
            )
        )
        self.session.flush()

    # --- running ---------------------------------------------------------------------

    def run_command(self, argv: list[str]):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch(
            "app.analyze_insiders.SessionLocal",
            return_value=SessionProxy(self.session),
        ):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(argv)

        parsed = json.loads(stdout.getvalue()) if stdout.getvalue() else None
        return code, parsed, stderr.getvalue()

    def count(self, model: type) -> int:
        return self.session.scalar(select(func.count()).select_from(model))


class ReaderTests(AnalyzeCommandTestCase):
    def test_two_owners_with_one_transaction_count_once(self):
        """The fan-out, checked at the layer that could actually cause it."""
        company = self.add_company()
        for offset, close in enumerate(("100", "110")):
            self.add_price(company, date(2026, 9, 1 + offset * 29), close)
        filing = self.add_filing(company)
        self.add_owner(filing, "0000000001", "ALPHA HOLDINGS LLC")
        self.add_owner(filing, "0000000002", "BETA MANAGEMENT GP")
        self.add_transaction(filing)

        code, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(code, 0)
        self.assertEqual(document["transactions"]["sale_count"], 1)
        self.assertEqual(document["transactions"]["sale_value_known"], "20000")
        self.assertEqual(len(document["transactions"]["included"]), 1)

    def test_two_distinct_rows_with_identical_values_stay_distinct(self):
        company = self.add_company()
        for offset, close in enumerate(("100", "110")):
            self.add_price(company, date(2026, 9, 1 + offset * 29), close)
        filing = self.add_filing(company)
        self.add_transaction(filing, row_position=0)
        self.add_transaction(filing, row_position=1)

        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(document["transactions"]["sale_count"], 2)
        self.assertEqual(document["transactions"]["sale_value_known"], "40000")
        positions = [row["row_position"] for row in document["transactions"]["included"]]
        self.assertEqual(positions, [0, 1])

    def test_the_same_position_in_the_other_table_is_a_separate_row(self):
        company = self.add_company()
        for offset, close in enumerate(("100", "110")):
            self.add_price(company, date(2026, 9, 1 + offset * 29), close)
        filing = self.add_filing(company)
        self.add_transaction(filing, row_position=0)
        self.add_transaction(filing, row_position=0, source_table="derivativeTable")

        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(document["transactions"]["sale_count"], 1)
        reasons = {e["reason"]: e["count"] for e in document["exclusions"]}
        self.assertEqual(reasons, {"derivative_security": 1})

    def test_a_filing_accepted_after_the_cutoff_is_excluded(self):
        company = self.add_company()
        for offset, close in enumerate(("100", "110")):
            self.add_price(company, date(2026, 9, 1 + offset * 29), close)
        filing = self.add_filing(
            company,
            acceptance=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc),
        )
        self.add_transaction(filing)

        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(document["transactions"]["sale_count"], 0)
        reasons = {e["reason"]: e["count"] for e in document["exclusions"]}
        self.assertEqual(reasons, {"filed_after_cutoff": 1})

    def test_a_missing_acceptance_timestamp_is_excluded_and_counted(self):
        company = self.add_company()
        for offset, close in enumerate(("100", "110")):
            self.add_price(company, date(2026, 9, 1 + offset * 29), close)
        filing = self.add_filing(company, acceptance=None)
        self.add_transaction(filing)

        _, document, _ = self.run_command(["--symbol", TICKER])

        reasons = {e["reason"]: e["count"] for e in document["exclusions"]}
        self.assertEqual(reasons, {"acceptance_time_unknown": 1})

    def test_an_amendment_accepted_before_the_cutoff_blocks_the_comparison(self):
        company = self.add_company()
        for offset, close in enumerate(("100", "110")):
            self.add_price(company, date(2026, 9, 1 + offset * 29), close)
        self.add_filing(company, "0001199039-26-000014")
        amendment = self.add_filing(
            company, "0001045810-23-000242", is_amendment=True
        )
        self.add_transaction(amendment, code="A", direction="A")

        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertTrue(document["amendments"]["uncertainty"])
        self.assertEqual(
            document["amendments"]["accessions"], ["0001045810-23-000242"]
        )
        self.assertEqual(document["sample_comparison"], "unavailable")

    def test_the_stored_price_series_is_reported(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "100")
        self.add_price(company, date(2026, 9, 30), "110")

        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(
            document["prices"]["series"],
            {
                "provider": "twelve_data",
                "adjustment_basis": "adjusted",
                "provider_adjust_mode": "splits",
            },
        )

    def test_two_price_series_are_refused_rather_than_combined(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "100")
        self.add_price(company, date(2026, 9, 30), "110")
        self.add_price(company, date(2026, 9, 1), "99", basis="raw", mode="none")

        code, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(code, 0)
        self.assertIsNone(document["prices"]["series"])
        self.assertEqual(
            document["prices"]["unavailable_reason"], "more_than_one_price_series_stored"
        )
        self.assertIn("Refusing to pick one", " ".join(document["limitations"]))


class CoverageTests(AnalyzeCommandTestCase):
    def test_ingestion_records_produce_partial_coverage(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "100")
        self.add_price(company, date(2026, 9, 30), "110")
        self.add_run(company)

        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(document["coverage"]["status"], "partial")
        self.assertEqual(document["coverage"]["ingestion_run_count"], 1)
        self.assertEqual(document["coverage"]["latest_stored_filings"], 3)
        self.assertEqual(
            document["coverage"]["discovery_scope"], "recent submissions list only"
        )

    def test_no_ingestion_records_means_unknown_not_complete(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "100")
        self.add_price(company, date(2026, 9, 30), "110")

        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(document["coverage"]["status"], "unknown")
        self.assertEqual(document["overall_conclusion"], "insufficient_coverage")


class PeriodTests(AnalyzeCommandTestCase):
    def setUp(self) -> None:
        super().setUp()
        company = self.add_company()
        self.add_price(company, date(2026, 8, 6), "218.99001")
        self.add_price(company, date(2026, 9, 17), "219.34000")

    def test_the_defaults_come_from_the_stored_range_and_are_printed(self):
        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(document["period"]["requested_start"], "2026-08-06")
        self.assertEqual(document["period"]["requested_end"], "2026-09-17")
        self.assertEqual(document["prices"]["first_date"], "2026-08-06")
        self.assertEqual(document["prices"]["last_date"], "2026-09-17")

    def test_explicit_dates_are_used_and_reported(self):
        _, document, _ = self.run_command(
            ["--symbol", TICKER, "--start", "2026-08-06", "--end", "2026-09-17"]
        )

        self.assertEqual(document["period"]["requested_start"], "2026-08-06")
        self.assertEqual(
            document["period"]["information_cutoff"], "2026-09-18T04:00:00+00:00"
        )

    def test_a_start_after_the_end_exits_nonzero(self):
        code, document, err = self.run_command(
            ["--symbol", TICKER, "--start", "2026-09-30", "--end", "2026-09-01"]
        )

        self.assertEqual(code, 1)
        self.assertIsNone(document)
        self.assertIn("after", err)

    def test_an_unparseable_date_exits_nonzero(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_command(["--symbol", TICKER, "--start", "not-a-date"])

        self.assertEqual(caught.exception.code, 2)

    def test_precision_survives_the_round_trip_through_the_database(self):
        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(document["prices"]["first_close"], "218.99001")
        self.assertEqual(document["display"]["price_change_percent"], "0.16")


class UnavailableTests(AnalyzeCommandTestCase):
    def test_a_symbol_with_no_stored_company_is_a_typed_result_not_an_exception(self):
        self.add_company()

        code, document, err = self.run_command(["--symbol", "AAPL"])

        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            document["unavailable_reason"], "no_company_stored_under_that_symbol"
        )
        self.assertEqual(document["sample_comparison"], "unavailable")
        self.assertIn("NVDA", " ".join(document["limitations"]))

    def test_a_company_with_no_prices_is_unavailable(self):
        self.add_company()

        code, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(code, 0)
        self.assertEqual(document["unavailable_reason"], "no_price_series_stored")

    def test_a_symbol_is_matched_case_insensitively(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "100")
        self.add_price(company, date(2026, 9, 30), "110")

        code, document, _ = self.run_command(["--symbol", "nvda"])

        self.assertEqual(code, 0)
        self.assertIsNone(document["unavailable_reason"])

    def test_an_empty_symbol_exits_nonzero(self):
        code, _, err = self.run_command(["--symbol", "   "])

        self.assertEqual(code, 1)
        self.assertIn("symbol", err)


class ReadOnlyTests(AnalyzeCommandTestCase):
    def test_the_command_writes_nothing(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "100")
        self.add_price(company, date(2026, 9, 30), "110")
        filing = self.add_filing(company)
        self.add_owner(filing, "0000000001", "ALPHA")
        self.add_transaction(filing)
        self.add_run(company)

        before = {
            model.__tablename__: self.count(model)
            for model in (
                Company,
                DailyPrice,
                SecFiling,
                InsiderReportingOwner,
                InsiderTransaction,
                IngestionRun,
            )
        }

        for _ in range(3):
            self.run_command(["--symbol", TICKER])

        after = {
            model.__tablename__: self.count(model)
            for model in (
                Company,
                DailyPrice,
                SecFiling,
                InsiderReportingOwner,
                InsiderTransaction,
                IngestionRun,
            )
        }
        self.assertEqual(before, after)

    def test_the_command_makes_no_network_request(self):
        """It reads the database and nothing else -- there is no client to call."""
        import app.analyze_insiders as module

        source = open(module.__file__).read()
        for forbidden in ("httpx", "urllib", "requests"):
            self.assertNotIn(forbidden, source)


class OutputShapeTests(AnalyzeCommandTestCase):
    def test_the_result_explains_itself(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "100")
        self.add_price(company, date(2026, 9, 30), "110")
        filing = self.add_filing(company)
        self.add_transaction(filing)

        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertTrue(document["methodology"])
        self.assertTrue(document["limitations"])
        self.assertEqual(document["overall_conclusion"], "insufficient_coverage")
        self.assertIn("reported_value = reported_shares", " ".join(document["methodology"]))
        self.assertTrue(
            any("not a point-in-time backtest" in note for note in document["limitations"])
        )

    def test_every_included_row_carries_its_source(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "100")
        self.add_price(company, date(2026, 9, 30), "110")
        filing = self.add_filing(company)
        self.add_transaction(filing)

        _, document, _ = self.run_command(["--symbol", TICKER])
        row = document["transactions"]["included"][0]

        self.assertEqual(row["accession_number"], "0001199039-26-000014")
        self.assertTrue(row["source_document_url"].startswith("https://www.sec.gov/"))

    def test_no_purchases_is_reported_as_a_fact_about_the_filings(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "100")
        self.add_price(company, date(2026, 9, 30), "110")
        filing = self.add_filing(company)
        self.add_transaction(filing)

        _, document, _ = self.run_command(["--symbol", TICKER])

        self.assertEqual(document["transactions"]["purchase_count"], 0)
        self.assertTrue(
            any(
                "not the same statement as" in note
                for note in document["limitations"]
            ),
            document["limitations"],
        )


if __name__ == "__main__":
    unittest.main()