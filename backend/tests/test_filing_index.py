"""Indexing, against a real Qdrant (in-process) and a real PostgreSQL.

The collection is genuinely Qdrant — created, validated, written and queried — so the
dimension check, the determinism of the ids and the upsert semantics are all exercised rather
than simulated. Only the embedding model is replaced.

Every test runs inside a transaction that is rolled back, and each gets its own in-memory
collection, so nothing here can touch the development index.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone
from unittest import mock

from qdrant_client import QdrantClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.chunking import CHUNKING_VERSION
from app.filing_index import (
    OUTCOME_INDEXED,
    OUTCOME_SKIPPED,
    OUTCOME_UNCHANGED,
    ReindexRequiredError,
    index_company,
)
from app.models import Company, DocumentIndexManifest, FilingDocument, SecFiling
from app.vector_store import (
    VECTOR_SIZE,
    CollectionMismatchError,
    VectorStore,
)
from tests.search_doubles import StubEmbedder
from tests.testdb import test_engine

CIK = "0001045810"
ACCEPTED = datetime(2026, 8, 26, 20, 36, tzinfo=timezone.utc)
TEXT = (
    "Item 1A. Risk Factors\n\n"
    + " ".join(f"risk{index}" for index in range(200))
    + "\n\nExport controls restrict sales of our products in several markets. "
    + " ".join(f"more{index}" for index in range(200))
)


class CountingEmbedder(StubEmbedder):
    """Counts how many passages it was asked to embed, so a repeat run can be checked."""

    def __init__(self) -> None:
        super().__init__()
        self.passages_embedded = 0

    def embed_passages(self, texts) -> list[list[float]]:
        items = list(texts)
        self.passages_embedded += len(items)
        return [self._vector(text) for text in items]


class IndexingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def setUp(self) -> None:
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)
        self.client = QdrantClient(location=":memory:")
        self.store = VectorStore(
            url="memory://", collection_name="test_filings", client=self.client
        )
        self.embedder = CountingEmbedder()

    def tearDown(self) -> None:
        self.client.close()
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    # --- seeding ---------------------------------------------------------------------

    def add_company(self, ticker: str = "NVDA", cik: str = CIK) -> Company:
        company = Company(
            name="NVIDIA CORP", ticker=ticker, exchange="NASDAQ",
            currency="USD", sec_issuer_cik=cik,
        )
        self.session.add(company)
        self.session.flush()
        return company

    def add_document(
        self,
        company: Company,
        *,
        accession: str = "0001045810-26-000075",
        document_name: str = "nvda-20260726.htm",
        form_type: str = "10-Q",
        text: str | None = TEXT,
        sections: dict | None = None,
        acceptance: datetime | None = ACCEPTED,
    ) -> FilingDocument:
        filing = SecFiling(
            company_id=company.id,
            accession_number=accession,
            form_type=form_type,
            filing_date=date(2026, 8, 26),
            report_date=date(2026, 7, 26),
            acceptance_datetime=acceptance,
            source_document_url=f"https://www.sec.gov/Archives/edgar/data/1045810/{accession}.htm",
            is_amendment=False,
            document_type=form_type,
            footnotes={},
        )
        self.session.add(filing)
        self.session.flush()

        document = FilingDocument(
            filing_id=filing.id,
            document_name=document_name,
            document_type=form_type,
            role="primary",
            sequence=None,
            source_url=f"https://www.sec.gov/Archives/edgar/data/1045810/{document_name}",
            content_type="text/html",
            content="<html>raw</html>",
            content_sha256="a" * 64,
            retrieved_at=ACCEPTED,
            extracted_text=text,
            extraction_version="1",
            extraction_status="extracted" if text else "unsupported",
            extraction_limitations=None,
            sections=sections or {},
        )
        self.session.add(document)
        self.session.flush()
        return document

    def run_index(self, symbol: str = "NVDA", dry_run: bool = False):
        return index_company(
            self.session,
            symbol=symbol,
            store=self.store,
            embedder=self.embedder,
            dry_run=dry_run,
        )

    def point_count(self) -> int:
        """Points in the collection, or zero when a dry run never created one."""
        return self.store.count() if self.store.exists() else 0

    def manifest_count(self) -> int:
        return self.session.scalar(
            select(func.count()).select_from(DocumentIndexManifest)
        )


class FirstRunTests(IndexingTestCase):
    def test_indexing_writes_points_and_records_the_manifest(self):
        self.add_company()
        self.add_document(self.session.scalar(select(Company)))

        summary = self.run_index()

        self.assertTrue(summary.collection_created)
        self.assertEqual(summary.counts()[OUTCOME_INDEXED], 1)
        self.assertGreater(summary.chunk_count, 0)
        self.assertEqual(self.point_count(), summary.chunk_count)
        self.assertEqual(self.manifest_count(), 1)

    def test_the_collection_is_created_at_the_models_dimension(self):
        self.add_company()

        self.run_index()

        info = self.client.get_collection("test_filings")
        self.assertEqual(info.config.params.vectors.size, VECTOR_SIZE)

    def test_a_document_with_no_extracted_text_is_skipped_with_a_reason(self):
        company = self.add_company()
        self.add_document(company, text=None)

        summary = self.run_index()

        self.assertEqual(summary.counts()[OUTCOME_SKIPPED], 1)
        self.assertEqual(summary.chunk_count, 0)
        self.assertEqual(self.manifest_count(), 0)
        self.assertIn("no extracted text", summary.documents[0].reason)

    def test_an_unknown_symbol_indexes_nothing_and_says_so(self):
        summary = self.run_index(symbol="AAPL")

        self.assertIsNone(summary.company_id)
        self.assertEqual(summary.documents, ())
        self.assertEqual(self.manifest_count(), 0)

    def test_the_manifest_holds_the_required_identity(self):
        company = self.add_company()
        document = self.add_document(company)

        self.run_index()

        record = self.session.scalar(select(DocumentIndexManifest))
        self.assertEqual(record.document_id, document.id)
        self.assertEqual(record.collection_name, "test_filings")
        self.assertEqual(record.embedding_model, self.embedder.model_name)
        self.assertEqual(record.chunking_version, CHUNKING_VERSION)
        self.assertEqual(record.content_sha256, "a" * 64)
        self.assertEqual(len(record.extracted_text_sha256), 64)
        self.assertEqual(record.point_count, self.point_count())


class RepeatRunTests(IndexingTestCase):
    def test_a_repeat_run_embeds_nothing_and_adds_no_points(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()
        first_points = self.point_count()
        first_embedded = self.embedder.passages_embedded

        summary = self.run_index()

        self.assertEqual(summary.counts()[OUTCOME_UNCHANGED], 1)
        self.assertEqual(self.embedder.passages_embedded, first_embedded)
        self.assertEqual(self.point_count(), first_points)
        self.assertEqual(self.manifest_count(), 1)

    def test_the_manifest_timestamp_is_not_rewritten_by_a_repeat(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()
        first = self.session.scalar(select(DocumentIndexManifest.indexed_at))

        self.run_index()

        self.assertEqual(
            self.session.scalar(select(DocumentIndexManifest.indexed_at)), first
        )

    def test_point_ids_are_deterministic_across_runs(self):
        from app.filing_index import _point_id

        self.assertEqual(_point_id(7, 0), _point_id(7, 0))
        self.assertNotEqual(_point_id(7, 0), _point_id(7, 1))
        self.assertNotEqual(_point_id(7, 0), _point_id(8, 0))


class StaleIndexTests(IndexingTestCase):
    def test_changed_extracted_text_requires_a_reindex(self):
        company = self.add_company()
        document = self.add_document(company)
        self.run_index()

        document.extracted_text = TEXT + " and a newly added sentence."
        self.session.flush()

        with self.assertRaises(ReindexRequiredError) as caught:
            self.run_index()

        self.assertIn(document.document_name, str(caught.exception))
        self.assertIn("reindexing", str(caught.exception))

    def test_a_changed_chunking_version_requires_a_reindex(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        with mock.patch("app.filing_index.CHUNKING_VERSION", "999"):
            with self.assertRaises(ReindexRequiredError):
                self.run_index()

    def test_a_changed_embedding_model_requires_a_reindex(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        other = CountingEmbedder()
        object.__setattr__(other, "_model_name", "stub/another-model")

        with self.assertRaises(ReindexRequiredError):
            index_company(
                self.session, symbol="NVDA", store=self.store, embedder=other
            )

    def test_a_refused_reindex_writes_nothing(self):
        company = self.add_company()
        document = self.add_document(company)
        self.run_index()
        points = self.point_count()

        document.extracted_text = TEXT + " changed."
        self.session.flush()
        with self.assertRaises(ReindexRequiredError):
            self.run_index()

        self.assertEqual(self.point_count(), points)
        self.assertEqual(self.manifest_count(), 1)

    def test_a_new_document_alongside_a_stale_one_is_still_refused(self):
        """The whole run stops, rather than quietly indexing the half that is fine."""
        company = self.add_company()
        first = self.add_document(company, accession="0001045810-26-000075")
        self.run_index()
        first.extracted_text = TEXT + " changed."
        self.add_document(
            company, accession="0001045810-26-000021", document_name="nvda-20260125.htm"
        )
        self.session.flush()

        with self.assertRaises(ReindexRequiredError):
            self.run_index()


class CollectionTests(IndexingTestCase):
    def test_an_existing_collection_is_validated_not_recreated(self):
        company = self.add_company()
        self.add_document(company)
        first = self.run_index()

        second = self.run_index()

        self.assertTrue(first.collection_created)
        self.assertFalse(
            second.collection_created, "an existing collection must never be recreated"
        )

    def test_a_wrong_dimension_fails_clearly(self):
        from qdrant_client import models

        self.client.create_collection(
            collection_name="test_filings",
            vectors_config=models.VectorParams(size=768, distance=models.Distance.COSINE),
        )
        self.add_company()

        with self.assertRaises(CollectionMismatchError) as caught:
            self.run_index()

        message = str(caught.exception)
        self.assertIn("768", message)
        self.assertIn(str(VECTOR_SIZE), message)

    def test_a_wrong_distance_fails_clearly(self):
        from qdrant_client import models

        self.client.create_collection(
            collection_name="test_filings",
            vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.DOT),
        )
        self.add_company()

        with self.assertRaises(CollectionMismatchError):
            self.run_index()


class IsolationTests(IndexingTestCase):
    def test_two_companies_do_not_share_points(self):
        first = self.add_company("NVDA", CIK)
        second = self.add_company("AMD", "0000002488")
        self.add_document(first, accession="0001045810-26-000075")
        self.add_document(
            second,
            accession="0000002488-26-000001",
            document_name="amd-20260726.htm",
        )

        self.run_index("NVDA")

        self.assertEqual(self.manifest_count(), 1)
        payload_company_ids = {
            point.payload.get("company_id")
            for point in self.client.scroll("test_filings", limit=1000)[0]
        }
        self.assertEqual(payload_company_ids, {first.id})


class InterruptedRunTests(IndexingTestCase):
    def test_a_failure_before_the_manifest_leaves_the_document_unindexed(self):
        """Points may exist; the manifest row is what marks a document complete."""
        company = self.add_company()
        self.add_document(company)

        with mock.patch.object(
            self.store, "upsert", side_effect=RuntimeError("connection lost")
        ):
            with self.assertRaises(RuntimeError):
                self.run_index()

        self.assertEqual(self.manifest_count(), 0)

    def test_a_retry_after_a_partial_write_converges(self):
        company = self.add_company()
        self.add_document(company)

        with mock.patch.object(
            self.store, "upsert", side_effect=RuntimeError("connection lost")
        ):
            with self.assertRaises(RuntimeError):
                self.run_index()

        self.run_index()

        self.assertEqual(self.manifest_count(), 1)
        self.assertEqual(self.point_count(), self.session.scalar(
            select(DocumentIndexManifest.point_count)
        ))


class DryRunTests(IndexingTestCase):
    def test_a_dry_run_writes_nothing_anywhere(self):
        company = self.add_company()
        self.add_document(company)

        summary = self.run_index(dry_run=True)

        self.assertEqual(summary.counts()[OUTCOME_INDEXED], 1)
        self.assertGreater(summary.chunk_count, 0)
        self.assertEqual(self.point_count(), 0)
        self.assertEqual(self.manifest_count(), 0)
        self.assertEqual(self.embedder.passages_embedded, 0)
        self.assertFalse(self.store.exists())

    def test_a_dry_run_reports_the_same_chunk_count_a_real_run_would_write(self):
        company = self.add_company()
        self.add_document(company)

        planned = self.run_index(dry_run=True).chunk_count
        written = self.run_index().chunk_count

        self.assertEqual(planned, written)


if __name__ == "__main__":
    unittest.main()
