"""Disclosure and fact persistence, against a real PostgreSQL.

Every test runs inside a transaction that is rolled back, so `alphadesk_test` is left
exactly as it was found and tests cannot see each other's rows.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.company_facts import FactObservation
from app.context_ingestion import (
    CONTEXT_SCOPE,
    FetchedDocument,
    FetchedSnapshot,
    SelectedFiling,
    persist_context,
    record_context_run,
)
from app.htmltext import EXTRACTION_VERSION, ExtractedDocument, SectionSpan
from app.ingestion import IngestionConflictError
from app.models import (
    Company,
    CompanyFactSnapshot,
    FilingDocument,
    FinancialFact,
    IngestionRun,
    SecFiling,
)
from tests.testdb import test_engine

CIK = "0001045810"
ACCESSION_10K = "0001045810-26-000021"
ACCESSION_10Q = "0001045810-26-000075"
ACCEPTED = datetime(2026, 8, 26, 20, 36, tzinfo=timezone.utc)


def make_company(session: Session) -> Company:
    company = Company(
        name="NVIDIA CORP",
        ticker="NVDA",
        exchange="NASDAQ",
        currency="USD",
        sec_issuer_cik=CIK,
    )
    session.add(company)
    session.flush()
    return company


def make_filing(accession: str = ACCESSION_10Q, form: str = "10-Q") -> SelectedFiling:
    return SelectedFiling(
        accession_number=accession,
        form_type=form,
        filing_date=date(2026, 8, 26),
        report_date=date(2026, 7, 26),
        acceptance_datetime=ACCEPTED,
        primary_document="nvda-20260726.htm",
        source_document_url=(
            f"https://www.sec.gov/Archives/edgar/data/1045810/"
            f"{accession.replace('-', '')}/nvda-20260726.htm"
        ),
    )


def make_document(
    *,
    accession: str = ACCESSION_10Q,
    name: str = "nvda-20260726.htm",
    role: str = "primary",
    content: str = "<p>Revenue was 96,221.</p>",
    with_extraction: bool = True,
) -> FetchedDocument:
    return FetchedDocument(
        filing_accession=accession,
        document_name=name,
        document_type="10-Q",
        role=role,
        sequence=None,
        source_url=f"https://www.sec.gov/Archives/edgar/data/1045810/{name}",
        content_type="text/html",
        content=content,
        retrieved_at=ACCEPTED,
        extraction=(
            ExtractedDocument(
                text="Revenue was 96,221.",
                sections={},
                limitations=("no sections looked for",),
                extraction_version=EXTRACTION_VERSION,
            )
            if with_extraction
            else None
        ),
        extraction_status="extracted" if with_extraction else "unsupported",
    )


def make_snapshot(payload: bytes = b'{"facts": {}}') -> FetchedSnapshot:
    return FetchedSnapshot(
        payload=payload,
        source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json",
        retrieved_at=ACCEPTED,
        byte_size=len(payload),
    )


def make_observation(**overrides) -> FactObservation:
    values = dict(
        taxonomy="us-gaap",
        concept="Revenues",
        unit="USD",
        value=Decimal("96221000000"),
        period_start=date(2026, 4, 27),
        period_end=date(2026, 7, 26),
        accession_number=ACCESSION_10Q,
        form="10-Q",
        filed_date=date(2026, 8, 26),
        fiscal_year=2027,
        fiscal_period="Q2",
        frame="CY2026Q2",
    )
    values.update(overrides)
    return FactObservation(**values)


class ContextIngestionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def setUp(self) -> None:
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)
        self.company = make_company(self.session)

    def tearDown(self) -> None:
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def persist(self, *, filings=None, documents=None, snapshot=None,
                observations=None, dry_run=False):
        return persist_context(
            self.session,
            company_id=self.company.id,
            filings=(make_filing(),) if filings is None else filings,
            documents=(make_document(),) if documents is None else documents,
            snapshot=make_snapshot() if snapshot is None else snapshot,
            observations=(make_observation(),) if observations is None else observations,
            dry_run=dry_run,
        )

    def count(self, model: type) -> int:
        return self.session.scalar(select(func.count()).select_from(model))


class FirstRunTests(ContextIngestionTestCase):
    def test_a_first_run_stores_everything(self):
        counts = self.persist()

        self.assertEqual(self.count(SecFiling), 1)
        self.assertEqual(self.count(FilingDocument), 1)
        self.assertEqual(self.count(CompanyFactSnapshot), 1)
        self.assertEqual(self.count(FinancialFact), 1)
        self.assertEqual(counts["filing_documents"].inserted, 1)
        self.assertEqual(counts["financial_facts"].inserted, 1)

    def test_a_disclosure_filing_leaves_the_form4_columns_null(self):
        """A 10-K is not an empty Form 4: it has no ownership XML and no holding rows."""
        self.persist()
        filing = self.session.scalar(select(SecFiling))

        self.assertIsNone(filing.source_xml)
        self.assertIsNone(filing.source_xml_sha256)
        self.assertIsNone(filing.holding_rows_skipped)
        self.assertIsNone(filing.rule_10b5_1)
        self.assertEqual(filing.footnotes, {})
        self.assertEqual(filing.document_type, "10-Q")
        self.assertEqual(filing.report_date, date(2026, 7, 26))
        self.assertFalse(filing.is_amendment)

    def test_the_document_stores_its_content_and_its_text_separately(self):
        self.persist()
        document = self.session.scalar(select(FilingDocument))

        self.assertIn("<p>", document.content)
        self.assertNotIn("<p>", document.extracted_text)
        self.assertEqual(document.extraction_status, "extracted")
        self.assertEqual(document.extraction_version, EXTRACTION_VERSION)
        self.assertEqual(len(document.content_sha256), 64)

    def test_an_unfetched_extraction_is_recorded_rather_than_left_blank(self):
        self.persist(documents=(make_document(with_extraction=False),))
        document = self.session.scalar(select(FilingDocument))

        self.assertIsNone(document.extracted_text)
        self.assertEqual(document.extraction_status, "unsupported")
        self.assertIsNotNone(document.content)


class RepeatRunTests(ContextIngestionTestCase):
    def test_a_repeat_run_inserts_nothing_and_preserves_ids(self):
        self.persist()
        ids = (
            self.session.scalar(select(FilingDocument.id)),
            self.session.scalar(select(FinancialFact.id)),
            self.session.scalar(select(SecFiling.id)),
        )
        retrieved = self.session.scalar(select(FilingDocument.retrieved_at))

        counts = self.persist()

        self.assertEqual(counts["filing_documents"].inserted, 0)
        self.assertEqual(counts["filing_documents"].unchanged, 1)
        self.assertEqual(counts["financial_facts"].inserted, 0)
        self.assertEqual(counts["company_fact_snapshots"].unchanged, 1)
        self.assertEqual(
            (
                self.session.scalar(select(FilingDocument.id)),
                self.session.scalar(select(FinancialFact.id)),
                self.session.scalar(select(SecFiling.id)),
            ),
            ids,
        )
        self.assertEqual(
            self.session.scalar(select(FilingDocument.retrieved_at)),
            retrieved,
            "the original retrieval timestamp must survive a re-run",
        )

    def test_changed_document_content_is_a_conflict(self):
        self.persist()

        with self.assertRaises(IngestionConflictError) as caught:
            self.persist(documents=(make_document(content="<p>Different.</p>"),))

        message = str(caught.exception)
        self.assertIn("nvda-20260726.htm", message)
        self.assertIn("content", message)

    def test_re_extraction_alone_is_not_a_conflict(self):
        """The document is its content; the text is derived from it and may improve."""
        self.persist()
        re_extracted = make_document()
        re_extracted = FetchedDocument(
            **{
                **re_extracted.__dict__,
                "extraction": ExtractedDocument(
                    text="Revenue was 96,221 (extracted better).",
                    sections={
                        "financial_statements": SectionSpan(
                            name="financial_statements",
                            start=0,
                            heading="Item 1. Financial Statements",
                        )
                    },
                    limitations=(),
                    extraction_version="99",
                ),
            }
        )

        counts = self.persist(documents=(re_extracted,))

        self.assertEqual(counts["filing_documents"].inserted, 0)
        self.assertEqual(counts["filing_documents"].unchanged, 1)

    def test_a_changed_fact_value_under_the_same_identity_is_a_conflict(self):
        self.persist()

        with self.assertRaises(IngestionConflictError) as caught:
            self.persist(observations=(make_observation(value=Decimal("1")),))

        self.assertIn("Revenues", str(caught.exception))


class SnapshotTests(ContextIngestionTestCase):
    def test_a_changed_snapshot_is_a_new_snapshot_not_a_conflict(self):
        """That endpoint grows as filings are added, and growth is not a disagreement."""
        self.persist()
        counts = self.persist(snapshot=make_snapshot(b'{"facts": {"more": true}}'))

        self.assertEqual(self.count(CompanyFactSnapshot), 2)
        self.assertEqual(counts["company_fact_snapshots"].inserted, 1)
        # The fact was already stored and is unchanged, even though it came with a new
        # snapshot -- `snapshot_id` is provenance and is never compared.
        self.assertEqual(counts["financial_facts"].unchanged, 1)
        self.assertEqual(self.count(FinancialFact), 1)

    def test_an_identical_snapshot_is_deduplicated_by_hash(self):
        self.persist()
        counts = self.persist()

        self.assertEqual(self.count(CompanyFactSnapshot), 1)
        self.assertEqual(counts["company_fact_snapshots"].unchanged, 1)

    def test_a_new_snapshot_can_bring_a_new_observation(self):
        self.persist()
        self.persist(
            snapshot=make_snapshot(b'{"facts": {"more": true}}'),
            observations=(
                make_observation(),
                make_observation(
                    period_start=date(2026, 1, 26),
                    period_end=date(2026, 7, 26),
                    value=Decimal("177837000000"),
                    frame=None,
                ),
            ),
        )

        self.assertEqual(self.count(FinancialFact), 2)


class NullablePeriodTests(ContextIngestionTestCase):
    def test_an_instant_fact_does_not_duplicate_on_a_repeat(self):
        """The whole reason the key is NULLS NOT DISTINCT in PostgreSQL."""
        instant = make_observation(
            concept="Assets",
            value=Decimal("320272000000"),
            period_start=None,
            frame="CY2026Q2I",
        )

        self.persist(observations=(instant,))
        counts = self.persist(observations=(instant,))

        self.assertEqual(self.count(FinancialFact), 1)
        self.assertEqual(counts["financial_facts"].inserted, 0)
        self.assertEqual(counts["financial_facts"].unchanged, 1)

    def test_two_instant_facts_with_different_end_dates_stay_separate(self):
        self.persist(observations=(
            make_observation(concept="Assets", period_start=None,
                             period_end=date(2026, 4, 26), value=Decimal("259474000000")),
            make_observation(concept="Assets", period_start=None,
                             period_end=date(2026, 7, 26), value=Decimal("320272000000")),
        ))

        self.assertEqual(self.count(FinancialFact), 2)

    def test_a_quarter_and_a_year_to_date_fact_both_persist(self):
        self.persist(observations=(
            make_observation(),
            make_observation(period_start=date(2026, 1, 26),
                             value=Decimal("177837000000"), frame=None),
        ))
        self.session.expire_all()

        values = self.session.scalars(
            select(FinancialFact.value).order_by(FinancialFact.period_start)
        ).all()
        self.assertEqual(values, [Decimal("177837000000"), Decimal("96221000000")])


class RecordTests(ContextIngestionTestCase):
    def test_the_receipt_is_written_under_its_own_scope(self):
        self.persist()
        record_context_run(
            self.session,
            company_id=self.company.id,
            parameters={"as_of": "2026-09-17"},
            summary={"documents": {"fetched": 1}},
            started_at=ACCEPTED,
            completed_at=ACCEPTED,
        )

        run = self.session.scalar(select(IngestionRun))

        self.assertEqual(run.scope, CONTEXT_SCOPE)
        self.assertEqual(run.summary["documents"]["fetched"], 1)


class DryRunTests(ContextIngestionTestCase):
    def test_a_dry_run_writes_nothing(self):
        counts = self.persist(dry_run=True)

        for model in (SecFiling, FilingDocument, CompanyFactSnapshot, FinancialFact):
            self.assertEqual(self.count(model), 0, f"{model.__tablename__} was written to")
        self.assertEqual(counts["filing_documents"].inserted, 1)

    def test_a_dry_run_over_existing_data_reports_unchanged(self):
        self.persist()
        counts = self.persist(dry_run=True)

        self.assertEqual(counts["filing_documents"].inserted, 0)
        self.assertEqual(counts["filing_documents"].unchanged, 1)
        self.assertEqual(self.count(FilingDocument), 1)

    def test_a_dry_run_still_raises_a_conflict(self):
        self.persist()

        with self.assertRaises(IngestionConflictError):
            self.persist(documents=(make_document(content="<p>Different.</p>"),), dry_run=True)

        self.session.rollback()
        self.assertEqual(self.count(FilingDocument), 0)


class FailureTests(unittest.TestCase):
    """A failure part-way through must leave nothing behind."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def test_a_document_with_no_matching_filing_is_refused(self):
        with self.engine.connect() as connection:
            transaction = connection.begin()
            session = Session(bind=connection)
            try:
                company = make_company(session)
                with self.assertRaises(ValueError):
                    persist_context(
                        session,
                        company_id=company.id,
                        filings=(make_filing(),),
                        documents=(make_document(accession="0000000000-00-000000"),),
                        snapshot=None,
                        observations=(),
                    )
            finally:
                session.close()
                transaction.rollback()

        with self.engine.connect() as connection:
            for model in (SecFiling, FilingDocument, FinancialFact):
                self.assertEqual(
                    connection.scalar(select(func.count()).select_from(model)),
                    0,
                    f"{model.__tablename__} should have nothing in it",
                )


if __name__ == "__main__":
    unittest.main()
