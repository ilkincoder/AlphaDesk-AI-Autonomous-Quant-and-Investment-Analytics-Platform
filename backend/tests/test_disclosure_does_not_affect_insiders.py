"""Adding disclosure filings must not move the insider analysis.

This is the protection the company-context ingestion needs and could easily do without. It
writes rows to `sec_filings` and to `ingestion_runs`, both of which the Form 4 analysis
reads. Two things could go wrong, and both are real rather than theoretical:

* A 10-K row in `sec_filings` could be joined into the transaction query. It carries no
  transactions today, so nothing would change -- until the day something did.
* A company-context run could become "the latest run" for the company, at which point the
  analysis would report that run's three disclosure filings as insider-history coverage.

The whole point of the milestone is that the numbers an analysis reports must mean what they
say, so this is checked against the real reads rather than argued.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import contextlib
import io
import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

from sqlalchemy.orm import Session

from sqlalchemy import select, text

from app.analyze_insiders import main as analyze_main
from app.context_ingestion import CONTEXT_SCOPE, SelectedFiling, persist_context, record_context_run
from app.ingestion import FORM4_SCOPE
from app.models import (
    Company,
    DailyPrice,
    IngestionRun,
    InsiderTransaction,
    SecFiling,
)
from tests.testdb import test_engine

CIK = "0001045810"
FORM4_ACCEPTED = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)
CONTEXT_ACCEPTED = datetime(2026, 8, 26, 20, 36, tzinfo=timezone.utc)


class SessionProxy:
    """Hands the command the test's own session without closing it."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *exc_info) -> bool:
        return False


class DisclosureIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def setUp(self) -> None:
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)
        self.company = self._seed_form4_world()

    def tearDown(self) -> None:
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    # --- seeding ---------------------------------------------------------------------

    def _seed_form4_world(self) -> Company:
        company = Company(
            name="NVIDIA CORP", ticker="NVDA", exchange="NASDAQ",
            currency="USD", sec_issuer_cik=CIK,
        )
        self.session.add(company)
        self.session.flush()

        for offset, close in enumerate(("218.99001", "219.34000")):
            self.session.add(
                DailyPrice(
                    company_id=company.id,
                    trading_date=date(2026, 9, 1 + offset * 16),
                    open=Decimal(close), high=Decimal(close),
                    low=Decimal(close), close=Decimal(close),
                    volume=1000, provider="twelve_data", currency="USD",
                    adjustment_basis="adjusted", provider_adjust_mode="splits",
                )
            )

        filing = SecFiling(
            company_id=company.id,
            accession_number="0001199039-26-000014",
            form_type="4",
            filing_date=date(2026, 9, 8),
            report_date=date(2026, 9, 3),
            acceptance_datetime=FORM4_ACCEPTED,
            source_document_url="https://www.sec.gov/Archives/edgar/data/1045810/x.xml",
            is_amendment=False,
            document_type="4",
            holding_rows_skipped=0,
            rule_10b5_1=False,
            footnotes={},
            source_xml="<ownershipDocument/>",
            source_xml_sha256="0" * 64,
        )
        self.session.add(filing)
        self.session.flush()

        self.session.add(
            InsiderTransaction(
                filing_id=filing.id,
                source_table="nonDerivativeTable",
                row_position=0,
                is_derivative=False,
                transaction_date=date(2026, 9, 3),
                security_title="Common Stock",
                transaction_code="S",
                acquired_disposed="D",
                shares=Decimal("198707"),
                price_per_share=Decimal("227.6954"),
                ownership_direct_indirect="D",
                footnote_refs=[],
            )
        )

        self.session.add(
            IngestionRun(
                company_id=company.id,
                scope=FORM4_SCOPE,
                started_at=FORM4_ACCEPTED,
                completed_at=FORM4_ACCEPTED,
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
        return company

    def _ingest_disclosures(self) -> None:
        """Write disclosure filings and a company-context run, as the command would."""
        filings = tuple(
            SelectedFiling(
                accession_number=f"0001045810-26-{n:06d}",
                form_type=form,
                filing_date=date(2026, 8, 26),
                report_date=date(2026, 7, 26),
                acceptance_datetime=CONTEXT_ACCEPTED,
                primary_document=f"nvda-{form}.htm",
                source_document_url=(
                    f"https://www.sec.gov/Archives/edgar/data/1045810/"
                    f"00010458102600{n:04d}/nvda-{form}.htm"
                ),
            )
            for n, form in ((21, "10-K"), (75, "10-Q"), (73, "8-K"))
        )

        persist_context(
            self.session,
            company_id=self.company.id,
            filings=filings,
            documents=(),
            snapshot=None,
            observations=(),
        )
        record_context_run(
            self.session,
            company_id=self.company.id,
            parameters={"as_of": "2026-09-17", "scope": CONTEXT_SCOPE},
            summary={"filings": {"returned_filings": 3}},
            started_at=CONTEXT_ACCEPTED,
            completed_at=CONTEXT_ACCEPTED,
        )

    # --- running ---------------------------------------------------------------------

    def analyze(self) -> dict:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with _patched_session(self.session):
                code = analyze_main(["--symbol", "NVDA"])
        self.assertEqual(code, 0)
        return json.loads(stdout.getvalue())

    def test_the_insider_analysis_is_identical_after_a_disclosure_ingestion(self):
        before = self.analyze()

        self._ingest_disclosures()

        after = self.analyze()

        self.assertEqual(
            json.dumps(before, sort_keys=True),
            json.dumps(after, sort_keys=True),
            "disclosure filings moved the insider analysis",
        )

    def test_the_disclosure_filings_really_are_in_the_database(self):
        """Otherwise the comparison above would be proving nothing."""
        self._ingest_disclosures()
        forms = set(
            self.session.scalars(
                select(SecFiling.form_type).where(SecFiling.company_id == self.company.id)
            )
        )

        self.assertIn("10-K", forms)
        self.assertIn("10-Q", forms)
        self.assertIn("8-K", forms)
        self.assertIn("4", forms)

    def test_coverage_still_reports_the_form4_run_not_the_disclosure_one(self):
        self._ingest_disclosures()
        result = self.analyze()

        coverage = result["coverage"]
        self.assertEqual(coverage["ingestion_run_count"], 1)
        self.assertEqual(coverage["latest_stored_filings"], 3)
        self.assertEqual(
            coverage["discovery_scope"], "recent submissions list only"
        )

    def test_the_run_count_does_not_include_disclosure_runs(self):
        """The failure mode this guards: a generic count reading as insider coverage."""
        self._ingest_disclosures()
        self._ingest_disclosures()

        self.assertEqual(self.analyze()["coverage"]["ingestion_run_count"], 1)
        self.assertEqual(
            self.session.scalar(
                text("SELECT count(*) FROM ingestion_runs WHERE scope = :s"),
                {"s": CONTEXT_SCOPE},
            ),
            2,
            "the disclosure runs are stored, they are just not counted as insider coverage",
        )

    def test_the_transaction_query_reads_ownership_filings_only(self):
        self._ingest_disclosures()
        result = self.analyze()

        self.assertEqual(result["transactions"]["sale_count"], 1)
        included = result["transactions"]["included"]
        self.assertEqual(
            [row["accession_number"] for row in included], ["0001199039-26-000014"]
        )
        for row in included:
            self.assertNotIn(row["accession_number"], ("0001045810-26-000021",
                                                       "0001045810-26-000075",
                                                       "0001045810-26-000073"))

    def test_amendment_blocking_ignores_disclosure_amendments(self):
        """A 10-K/A is an amendment, but not one that bears on insider transactions."""
        self._ingest_disclosures()
        self.session.add(
            SecFiling(
                company_id=self.company.id,
                accession_number="0001045810-26-000099",
                form_type="10-K/A",
                filing_date=date(2026, 9, 1),
                report_date=None,
                acceptance_datetime=CONTEXT_ACCEPTED,
                source_document_url="https://www.sec.gov/Archives/edgar/data/1045810/y.htm",
                is_amendment=True,
                document_type="10-K/A",
                footnotes={},
            )
        )
        self.session.flush()

        result = self.analyze()

        self.assertFalse(result["amendments"]["uncertainty"])
        self.assertEqual(result["sample_comparison"], "price_up_net_selling")


def _patched_session(session: Session):
    """Point the command's session factory at the test's own session."""
    return mock.patch(
        "app.analyze_insiders.SessionLocal", return_value=SessionProxy(session)
    )


if __name__ == "__main__":
    unittest.main()
