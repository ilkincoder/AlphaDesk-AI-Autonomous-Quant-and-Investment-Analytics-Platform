"""The envelope, the registry, and the promises every tool makes.

These are the properties that hold for all four tools rather than for any one of them: that an
unknown name is refused, that an undeclared argument is an error rather than something quietly
ignored, that validation happens before anything is read, that a result survives JSON with its
decimals intact, that a call changes nothing, and that the three SQL-only tools depend on
neither Qdrant nor the embedding model.

Against a real PostgreSQL (migrated, and rolled back per test) and with no Qdrant at all,
which is itself part of the point.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import hashlib
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import embeddings
from app.company_facts import CONCEPTS_BY_NAME
from app.models import (
    Company,
    CompanyFactSnapshot,
    Conversation,
    ConversationTurn,
    DailyPrice,
    DocumentIndexManifest,
    FilingDocument,
    FinancialFact,
    Holding,
    IngestionRun,
    InsiderReportingOwner,
    InsiderTransaction,
    Portfolio,
    SecFiling,
)
from app.tools import TOOLS, UnknownToolError, invoke, tool_names
from app.tools import financial_facts, market_insider, portfolio
from app.tools.financial_facts import MetricName
from app.tools.results import ToolStatus
from tests.testdb import test_engine

TICKER = "NVDA"
CIK = "0001045810"
OTHER_TICKER = "AAPL"
OTHER_CIK = "0000320193"

ACCEPTED = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)

# Every table a tool could plausibly write to. Counted before and after each call.
TRACKED_TABLES = (
    Company,
    DailyPrice,
    SecFiling,
    InsiderTransaction,
    InsiderReportingOwner,
    IngestionRun,
    FilingDocument,
    DocumentIndexManifest,
    CompanyFactSnapshot,
    FinancialFact,
    Portfolio,
    Holding,
    Conversation,
    ConversationTurn,
)


class ToolTestCase(unittest.TestCase):
    """A real database, a session inside a rolled-back transaction, and seed helpers.

    Nothing here opens a Qdrant client, which is what lets the three SQL-only tools be tested
    as the standalone things they are.
    """

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

    def add_company(
        self,
        ticker: str = TICKER,
        cik: str = CIK,
        name: str = "NVIDIA CORP",
    ) -> Company:
        company = Company(
            name=name, ticker=ticker, exchange="NASDAQ", currency="USD", sec_issuer_cik=cik
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

    def add_price_range(
        self,
        company: Company,
        start: date,
        days: int,
        *,
        first: str = "100",
        step: str = "1",
    ) -> None:
        close = Decimal(first)
        increment = Decimal(step)
        for offset in range(days):
            self.add_price(company, start + timedelta(days=offset), str(close))
            close += increment

    def add_filing(
        self,
        company: Company,
        accession: str = "0001199039-26-000014",
        *,
        form_type: str = "4",
        acceptance: datetime | None = ACCEPTED,
        is_amendment: bool = False,
        filing_date: date = date(2026, 9, 5),
        report_date: date | None = None,
    ) -> SecFiling:
        filing = SecFiling(
            company_id=company.id,
            accession_number=accession,
            form_type="4/A" if is_amendment else form_type,
            filing_date=filing_date,
            report_date=report_date,
            acceptance_datetime=acceptance,
            source_document_url=(
                f"https://www.sec.gov/Archives/edgar/data/1045810/{accession}.htm"
            ),
            is_amendment=is_amendment,
            document_type="4/A" if is_amendment else form_type,
            holding_rows_skipped=0,
            footnotes={},
            source_xml="<ownershipDocument/>",
            source_xml_sha256="0" * 64,
        )
        self.session.add(filing)
        self.session.flush()
        return filing

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

    def add_snapshot(self, company: Company) -> CompanyFactSnapshot:
        snapshot = CompanyFactSnapshot(
            company_id=company.id,
            source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{company.sec_issuer_cik}.json",
            retrieved_at=ACCEPTED,
            # Derived from the issuer, because `content_sha256` is unique across the table and
            # a shared constant would collide the moment a test seeded two companies.
            content_sha256=hashlib.sha256(company.sec_issuer_cik.encode()).hexdigest(),
            byte_size=1024,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def add_fact(
        self,
        company: Company,
        snapshot: CompanyFactSnapshot,
        *,
        concept: str = "Revenues",
        value: str = "1000",
        period_start: date | None = date(2026, 4, 27),
        period_end: date = date(2026, 7, 26),
        accession: str | None = "0001045810-26-000075",
        unit: str = "USD",
        form: str | None = "10-Q",
    ) -> FinancialFact:
        fact = FinancialFact(
            company_id=company.id,
            snapshot_id=snapshot.id,
            taxonomy="us-gaap",
            concept=concept,
            unit=unit,
            value=Decimal(value),
            period_start=period_start,
            period_end=period_end,
            accession_number=accession,
            form=form,
            filed_date=date(2026, 8, 26),
            fiscal_year=2027,
            fiscal_period="Q2",
            frame=None,
        )
        self.session.add(fact)
        self.session.flush()
        return fact

    # --- observability -----------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        return {
            model.__tablename__: self.session.scalar(
                select(func.count()).select_from(model)
            )
            for model in TRACKED_TABLES
        }

    def assert_wrote_nothing(self, before: dict[str, int]) -> None:
        """No row changed, and no ORM-level insert, update or delete was even staged."""
        self.assertEqual(before, self.counts())
        self.assertEqual(set(self.session.new), set())
        self.assertEqual(set(self.session.dirty), set())
        self.assertEqual(set(self.session.deleted), set())


class RegistryTests(ToolTestCase):
    def test_the_tools_are_the_four_that_were_asked_for(self):
        self.assertEqual(
            tool_names(),
            (
                "market_insider_analysis",
                "company_financial_facts",
                "filing_evidence_search",
                "portfolio_context",
            ),
        )

    def test_every_tool_declares_a_name_description_and_payload(self):
        for name, spec in TOOLS.items():
            with self.subTest(tool=name):
                self.assertEqual(spec.name, name)
                # A caller has to know when to reach for it, and what it returns.
                self.assertGreater(len(spec.description), 100)
                self.assertGreater(len(spec.payload), 20)
                self.assertTrue(callable(spec.run))
                self.assertTrue(issubclass(spec.request_model, object))

    def test_an_unknown_name_is_a_lookup_error_not_a_result(self):
        with self.assertRaises(UnknownToolError) as caught:
            invoke("market_insider", {}, self.session)

        self.assertIn("market_insider", str(caught.exception))
        self.assertIn("market_insider_analysis", str(caught.exception))

    def test_an_undeclared_argument_is_refused_by_every_tool(self):
        """Not ignored: a caller that misspells an argument must not get a defaulted answer."""
        for name, spec in TOOLS.items():
            with self.subTest(tool=name):
                with self.assertRaises(ValidationError):
                    spec.request_model.model_validate({"symbol": "NVDA", "typo": 1})


class ValidationHappensFirstTests(ToolTestCase):
    """A rejected request must cost nothing: no query, no index call, no partial work."""

    def setUp(self) -> None:
        super().setUp()
        self.company = self.add_company()

    def assert_no_read_on_rejection(self, name: str, arguments: dict) -> None:
        """A stand-in session that records everything asked of it. Nothing should be."""
        watched = mock.Mock(wraps=self.session)

        with self.assertRaises(ValidationError):
            invoke(name, arguments, watched)

        self.assertEqual(watched.mock_calls, [])

    def test_a_market_window_that_runs_backwards_is_refused_before_any_read(self):
        self.assert_no_read_on_rejection(
            "market_insider_analysis",
            {"symbol": "NVDA", "start_date": "2026-09-17", "end_date": "2026-08-06"},
        )

    def test_a_missing_date_is_refused_before_any_read(self):
        self.assert_no_read_on_rejection(
            "market_insider_analysis", {"symbol": "NVDA", "end_date": "2026-09-17"}
        )

    def test_an_unknown_metric_is_refused_before_any_read(self):
        self.assert_no_read_on_rejection(
            "company_financial_facts",
            {"symbol": "NVDA", "metric": "ebitda", "as_of": "2026-09-17"},
        )

    def test_a_balance_sheet_metric_with_a_period_start_is_refused_before_any_read(self):
        self.assert_no_read_on_rejection(
            "company_financial_facts",
            {
                "symbol": "NVDA",
                "metric": "total_assets",
                "as_of": "2026-09-17",
                "period_start": "2026-01-01",
            },
        )

    def test_a_top_k_outside_the_bound_is_refused_before_any_read(self):
        for top_k in (0, 21, -3):
            with self.subTest(top_k=top_k):
                self.assert_no_read_on_rejection(
                    "filing_evidence_search",
                    {
                        "symbol": "NVDA",
                        "question": "risk factors",
                        "as_of": "2026-09-17",
                        "top_k": top_k,
                    },
                )

    def test_a_blank_question_is_refused_before_any_read(self):
        self.assert_no_read_on_rejection(
            "filing_evidence_search",
            {"symbol": "NVDA", "question": "   ", "as_of": "2026-09-17"},
        )


class MetricVocabularyTests(unittest.TestCase):
    def test_the_metric_literal_is_exactly_the_stored_mappings(self):
        """The schema a model sees and the concepts this build reads cannot drift apart."""
        from typing import get_args

        self.assertEqual(set(get_args(MetricName)), set(CONCEPTS_BY_NAME))

    def test_the_two_revenue_concepts_are_separate_names(self):
        self.assertNotEqual(
            CONCEPTS_BY_NAME["revenue"].concept,
            CONCEPTS_BY_NAME["revenue_contract_with_customer"].concept,
        )


class JsonSafetyTests(ToolTestCase):
    """Decimals must reach the caller as exact strings, not as binary floats."""

    def test_a_market_result_is_json_and_keeps_its_decimals_exact(self):
        company = self.add_company()
        self.add_price(company, date(2026, 9, 1), "218.99001")
        self.add_price(company, date(2026, 9, 17), "219.34000")
        filing = self.add_filing(company)
        self.add_transaction(filing, shares="100", price="219.335")

        result = market_insider.run(
            self.session,
            market_insider.MarketInsiderRequest(
                symbol="NVDA", start_date=date(2026, 9, 1), end_date=date(2026, 9, 17)
            ),
        )
        document = json.loads(json.dumps(result.as_json()))

        self.assertEqual(document["status"], "ok")
        # The provider's seventh decimal place survives, unrounded.
        self.assertEqual(document["data"]["prices"]["first_close"], "218.99001")
        # 100 * 219.335 is exact, and multiplying by a float would not be.
        self.assertEqual(
            document["data"]["transactions"]["included"][0]["reported_value"], "21933.500"
        )

    def test_a_financial_fact_value_is_an_exact_string(self):
        company = self.add_company()
        snapshot = self.add_snapshot(company)
        self.add_filing(company, "0001045810-26-000075", form_type="10-Q")
        self.add_fact(company, snapshot, value="177837000000")

        result = financial_facts.run(
            self.session,
            financial_facts.FinancialFactsRequest(
                symbol="NVDA", metric="revenue", as_of=date(2026, 9, 17)
            ),
        )
        document = json.loads(json.dumps(result.as_json()))

        self.assertEqual(document["data"]["observations"][0]["value"], "177837000000")
        self.assertIsInstance(document["data"]["observations"][0]["value"], str)

    def test_a_portfolio_result_keeps_its_decimals_exact(self):
        self.session.add(
            Portfolio(name="AlphaDesk Demo", currency="USD", cash_balance=Decimal("10000.00"))
        )
        self.session.flush()

        result = portfolio.run(
            self.session, portfolio.PortfolioContextRequest(symbol="NVDA")
        )
        document = json.loads(json.dumps(result.as_json()))

        self.assertEqual(document["data"]["portfolio"]["name"], "AlphaDesk Demo")
        self.assertEqual(document["data"]["valuation"]["cash_balance"], "10000.00")


class ReadOnlyTests(ToolTestCase):
    """No tool may write, whatever it is asked."""

    def setUp(self) -> None:
        super().setUp()
        self.company = self.add_company()
        self.add_price_range(self.company, date(2026, 9, 1), 5)
        filing = self.add_filing(self.company)
        self.add_transaction(filing)
        snapshot = self.add_snapshot(self.company)
        self.add_fact(self.company, snapshot)
        self.session.add(
            Portfolio(name="AlphaDesk Demo", currency="USD", cash_balance=Decimal("100.00"))
        )
        self.session.flush()

    def test_no_tool_changes_a_row_or_stages_one(self):
        calls = (
            (
                "market_insider_analysis",
                {
                    "symbol": "NVDA",
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-05",
                },
            ),
            (
                "company_financial_facts",
                {"symbol": "NVDA", "metric": "revenue", "as_of": "2026-09-17"},
            ),
            ("portfolio_context", {"symbol": "NVDA"}),
            ("portfolio_context", {"symbol": "TSLA"}),
        )
        for name, arguments in calls:
            with self.subTest(tool=name, arguments=arguments):
                before = self.counts()
                invoke(name, arguments, self.session)
                self.assert_wrote_nothing(before)

    def test_every_statement_the_tools_issue_is_a_select(self):
        """Read from the SQL actually executed, rather than from the source's appearance.

        All three entry points are wrapped, not just `execute`: in SQLAlchemy 2.0 `scalar` and
        `scalars` do not route through `Session.execute`, so patching only that would leave
        most of a tool's reads unobserved and the assertion meaningless.
        """
        statements: list[str] = []

        def record(original):
            def wrapper(session_self, statement, *args, **kwargs):
                statements.append(str(statement))
                return original(session_self, statement, *args, **kwargs)

            return wrapper

        with mock.patch.object(Session, "execute", record(Session.execute)):
            with mock.patch.object(Session, "scalar", record(Session.scalar)):
                with mock.patch.object(Session, "scalars", record(Session.scalars)):
                    invoke(
                        "market_insider_analysis",
                        {
                            "symbol": "NVDA",
                            "start_date": "2026-09-01",
                            "end_date": "2026-09-05",
                        },
                        self.session,
                    )
                    invoke(
                        "company_financial_facts",
                        {"symbol": "NVDA", "metric": "revenue", "as_of": "2026-09-17"},
                        self.session,
                    )
                    invoke("portfolio_context", {"symbol": "NVDA"}, self.session)

        self.assertGreater(len(statements), 5)
        for statement in statements:
            with self.subTest(statement=statement[:80]):
                self.assertTrue(
                    statement.lstrip().upper().startswith("SELECT"),
                    f"a tool issued something other than a SELECT: {statement[:120]}",
                )


class LazinessTests(ToolTestCase):
    """The SQL-only tools must not depend on Qdrant or on the embedding model."""

    def test_their_source_names_neither_qdrant_nor_the_model(self):
        for module in (market_insider, financial_facts, portfolio):
            source = Path(module.__file__).read_text()
            for forbidden in ("qdrant", "VectorStore", "embeddings", "fastembed"):
                with self.subTest(module=module.__name__, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_calling_them_builds_no_vector_store_and_loads_no_model(self):
        company = self.add_company()
        self.add_price_range(company, date(2026, 9, 1), 3)

        with mock.patch("app.vector_store.QdrantClient") as qdrant:
            with mock.patch.object(embeddings, "get_embedder") as embedder:
                invoke(
                    "market_insider_analysis",
                    {
                        "symbol": "NVDA",
                        "start_date": "2026-09-01",
                        "end_date": "2026-09-03",
                    },
                    self.session,
                )
                invoke("portfolio_context", {"symbol": "NVDA"}, self.session)

        qdrant.assert_not_called()
        embedder.assert_not_called()


class OperationalFailureTests(ToolTestCase):
    """A database that cannot be read is `failed`, never `unavailable`."""

    def test_a_broken_database_is_failed_rather_than_no_data(self):
        self.add_company()

        with mock.patch.object(
            Session,
            "execute",
            side_effect=OperationalError("SELECT 1", {}, Exception("connection lost")),
        ):
            with self.assertLogs("app.tools.market_insider", level="ERROR") as logged:
                result = market_insider.run(
                    self.session,
                    market_insider.MarketInsiderRequest(
                        symbol="NVDA",
                        start_date=date(2026, 9, 1),
                        end_date=date(2026, 9, 5),
                    ),
                )

        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertEqual(result.reason, "database_unavailable")
        self.assertIsNone(result.data)
        self.assertFalse(result.carried_out)
        # Logged as well as reported: the full exception belongs in the log, and a failure
        # that only appears in the answer is one nobody can diagnose afterwards.
        self.assertIn("could not read the stored data", "\n".join(logged.output))

    def test_a_failure_message_carries_no_connection_detail(self):
        self.add_company()

        with mock.patch.object(
            Session,
            "execute",
            side_effect=OperationalError(
                "SELECT 1", {}, Exception("password authentication failed for user 'alpha'")
            ),
        ):
            with self.assertLogs("app.tools.financial_facts", level="ERROR"):
                result = financial_facts.run(
                    self.session,
                    financial_facts.FinancialFactsRequest(
                        symbol="NVDA", metric="revenue", as_of=date(2026, 9, 17)
                    ),
                )

        joined = " ".join(result.warnings)
        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertNotIn("password", joined)
        self.assertNotIn("alpha", joined)
        # The class name is enough to act on and carries no connection detail.
        self.assertIn("OperationalError", joined)

    def test_unavailable_and_failed_carry_no_payload(self):
        """`data` is None on both, so an error can never be read as a result."""
        company = self.add_company()

        missing = market_insider.run(
            self.session,
            market_insider.MarketInsiderRequest(
                symbol="ZZZZ", start_date=date(2026, 9, 1), end_date=date(2026, 9, 5)
            ),
        )

        self.assertEqual(missing.status, ToolStatus.UNAVAILABLE)
        self.assertIsNotNone(missing.reason)
        self.assertIsNone(missing.data)


if __name__ == "__main__":
    unittest.main()
