"""Retrieval: the statuses, the cutoff, and the validation of what comes back.

Against a real Qdrant (in-process) and a real PostgreSQL, with the model replaced. What is
being checked is what the caller is told — which is where a search interface usually goes
wrong, by reporting "nothing found" for half a dozen different situations.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
import uuid
from datetime import date, datetime, timezone
from unittest import mock

from qdrant_client import models
from app.filing_index import (
    STATUS_INDEX_UNAVAILABLE,
    STATUS_MODEL_UNAVAILABLE,
    STATUS_NO_ELIGIBLE_DOCUMENTS,
    STATUS_NO_INDEXED_DOCUMENTS,
    STATUS_NO_MATCHING_RESULTS,
    STATUS_OK,
    STATUS_UNKNOWN_COMPANY,
    index_company,
    search_filings,
)
from app.vector_store import VectorStore, VectorStoreUnavailableError
from tests.search_doubles import FailingEmbedder
from tests.test_filing_index import CIK, TEXT, IndexingTestCase  # noqa: F401

AS_OF = date(2026, 9, 17)
BEFORE_THE_10Q = date(2026, 8, 1)


class RetrievalTestCase(IndexingTestCase):
    def search(self, *, symbol="NVDA", query="export controls", as_of=AS_OF, top_k=5, store=None, embedder=None):
        return search_filings(
            self.session,
            symbol=symbol,
            query=query,
            as_of=as_of,
            top_k=top_k,
            store=store or self.store,
            embedder=embedder or self.embedder,
        )

    def a_vector(self) -> list[float]:
        """A real 384-dimensional vector for hand-written points.

        `scroll` does not return stored vectors by default, so a test that inserts a point
        directly has to supply one of the right shape.
        """
        return self.embedder.embed_query("export controls")

    def insert_point(self, payload: dict) -> None:
        """Write one point directly, to stand in for a leftover or a stale write.

        A generated UUID, because Qdrant only accepts UUID or integer ids and the real
        indexing derives UUIDs. The value is irrelevant to what these tests assert.
        """
        self.client.upsert(
            collection_name="test_filings",
            points=[
                models.PointStruct(
                    id=str(uuid.uuid4()), vector=self.a_vector(), payload=payload
                )
            ],
            wait=True,
        )


class StatusTests(RetrievalTestCase):
    def test_an_unknown_company_is_its_own_status(self):
        result = self.search(symbol="AAPL")

        self.assertEqual(result.status, STATUS_UNKNOWN_COMPANY)
        self.assertEqual(result.passages, ())

    def test_a_missing_collection_means_nothing_is_indexed(self):
        self.add_company()

        result = self.search()

        self.assertEqual(result.status, STATUS_NO_INDEXED_DOCUMENTS)
        self.assertIn("does not exist", result.reason)

    def test_an_index_with_no_manifests_means_nothing_is_indexed(self):
        company = self.add_company()
        self.add_document(company)
        self.store.ensure_collection()

        result = self.search()

        self.assertEqual(result.status, STATUS_NO_INDEXED_DOCUMENTS)

    def test_nothing_public_before_the_cutoff_is_its_own_status(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        result = self.search(as_of=date(2026, 1, 1))

        self.assertEqual(result.status, STATUS_NO_ELIGIBLE_DOCUMENTS)
        self.assertEqual(result.passages, ())
        self.assertIn("accepted before", result.reason)

    def test_a_query_matching_nothing_still_returns_nearest_neighbours(self):
        """A property of vector search, recorded rather than wished away.

        Cosine distance has no notion of "not relevant". Ask a question no passage answers and
        the index still returns whatever is nearest, with scores that look no different from
        the scores on a good answer. Nothing here can detect an unanswerable question, and any
        caller that treats a returned passage as an answered one will be misled.
        """
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        result = self.search(query="zzzznonexistentzzzz")

        self.assertEqual(
            result.status,
            STATUS_OK,
            "vector search returns its nearest neighbours whatever the query says",
        )
        self.assertTrue(result.passages)
        self.assertTrue(
            any("does not mean the question is answered" in w for w in result.warnings),
            result.warnings,
        )

    def test_the_no_matching_results_status_fires_when_the_index_returns_nothing(self):
        """The status is real; it just cannot be provoked by phrasing a query oddly."""
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        with mock.patch.object(self.store, "query", return_value=[]):
            result = self.search()

        self.assertEqual(result.status, STATUS_NO_MATCHING_RESULTS)
        self.assertEqual(result.passages, ())

    def test_the_no_matching_results_status_fires_when_validation_removes_everything(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        with mock.patch(
            "app.filing_index._payload_matches_manifest", return_value=False
        ):
            result = self.search()

        self.assertEqual(result.status, STATUS_NO_MATCHING_RESULTS)
        self.assertIn("validation", result.reason)

    def test_a_qdrant_failure_is_its_own_status(self):
        company = self.add_company()
        self.add_document(company)

        with mock.patch.object(
            self.store,
            "exists",
            side_effect=VectorStoreUnavailableError("qdrant is down"),
        ):
            result = self.search()

        self.assertEqual(result.status, STATUS_INDEX_UNAVAILABLE)
        self.assertIn("down", result.reason)

    def test_an_unavailable_model_is_its_own_status(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        result = self.search(embedder=FailingEmbedder())

        self.assertEqual(result.status, STATUS_MODEL_UNAVAILABLE)

    def test_every_status_is_distinct(self):
        statuses = {
            STATUS_OK,
            STATUS_UNKNOWN_COMPANY,
            STATUS_NO_INDEXED_DOCUMENTS,
            STATUS_NO_ELIGIBLE_DOCUMENTS,
            STATUS_NO_MATCHING_RESULTS,
            STATUS_INDEX_UNAVAILABLE,
            STATUS_MODEL_UNAVAILABLE,
        }
        self.assertEqual(len(statuses), 7)


class SuccessfulSearchTests(RetrievalTestCase):
    def test_a_matching_query_returns_passages_with_citations(self):
        company = self.add_company()
        document = self.add_document(company)
        self.run_index()

        result = self.search(query="export controls restrict sales", top_k=3)

        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue(result.passages)
        passage = result.passages[0]
        self.assertEqual(passage.accession_number, "0001045810-26-000075")
        self.assertEqual(passage.form_type, "10-Q")
        self.assertEqual(passage.document_id, document.id)
        self.assertEqual(passage.symbol, "NVDA")
        self.assertEqual(passage.cik, CIK)
        self.assertTrue(passage.source_url.startswith("https://www.sec.gov/"))
        self.assertEqual(len(passage.content_sha256), 64)
        self.assertEqual(len(passage.extracted_text_sha256), 64)

    def test_the_citation_offsets_point_at_the_stored_text(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        result = self.search(query="export controls", top_k=1)
        passage = result.passages[0]

        self.assertEqual(passage.text, TEXT[passage.start_offset : passage.end_offset])

    def test_no_more_than_top_k_are_returned(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        result = self.search(query="export controls risk", top_k=2)

        self.assertLessEqual(len(result.passages), 2)

    def test_a_similarity_is_never_presented_as_a_percentage(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        result = self.search(query="export controls", top_k=1)
        document = result.as_dict()

        self.assertLessEqual(document["passages"][0]["similarity"], 1.0)
        self.assertNotIn("%", str(document["passages"][0]["similarity"]))
        self.assertNotIn("confidence", str(document).lower())

    def test_the_result_says_what_a_hit_does_not_mean(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        result = self.search(query="export controls")

        self.assertTrue(
            any("does not mean the question is answered" in w for w in result.warnings),
            result.warnings,
        )


class CutoffTests(RetrievalTestCase):
    def test_a_filing_accepted_after_the_cutoff_is_not_returned(self):
        company = self.add_company()
        self.add_document(company, acceptance=datetime(2026, 8, 26, 20, 36, tzinfo=timezone.utc))
        self.run_index()

        result = self.search(as_of=BEFORE_THE_10Q)

        self.assertEqual(result.status, STATUS_NO_ELIGIBLE_DOCUMENTS)

    def test_a_filing_accepted_before_the_cutoff_is_returned(self):
        company = self.add_company()
        self.add_document(company, acceptance=datetime(2026, 2, 25, 21, 42, tzinfo=timezone.utc))
        self.run_index()

        result = self.search(as_of=BEFORE_THE_10Q)

        self.assertEqual(result.status, STATUS_OK)

    def test_the_cutoff_is_the_end_of_the_day_in_the_market_timezone(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        result = self.search(as_of=date(2026, 9, 17))

        self.assertEqual(
            result.information_cutoff.isoformat(), "2026-09-18T04:00:00+00:00"
        )

    def test_a_reporting_period_date_is_not_used_as_availability(self):
        """The period a filing covers is not the day the public could read it."""
        company = self.add_company()
        # Report period is 2026-07-26, but it was not accepted until August.
        self.add_document(
            company,
            acceptance=datetime(2026, 8, 26, 20, 36, tzinfo=timezone.utc),
        )
        self.run_index()

        result = self.search(as_of=date(2026, 7, 27))

        self.assertEqual(
            result.status,
            STATUS_NO_ELIGIBLE_DOCUMENTS,
            "the reporting period must not stand in for when the filing became public",
        )

    def test_a_filing_with_no_acceptance_timestamp_is_excluded_and_reported(self):
        company = self.add_company()
        self.add_document(company, acceptance=None)
        self.run_index()

        result = self.search()

        self.assertEqual(result.status, STATUS_NO_ELIGIBLE_DOCUMENTS)
        self.assertTrue(
            any("no acceptance timestamp" in w for w in result.warnings),
            result.warnings,
        )

    def test_an_indexed_document_with_unknown_acceptance_is_reported_alongside_good_ones(self):
        company = self.add_company()
        self.add_document(company, accession="0001045810-26-000075")
        self.add_document(
            company,
            accession="0001045810-26-000021",
            document_name="nvda-20260125.htm",
            acceptance=None,
        )
        self.run_index()

        result = self.search()

        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue(any("no acceptance timestamp" in w for w in result.warnings))


class ValidationTests(RetrievalTestCase):
    def test_a_point_with_no_manifest_row_is_not_shown(self):
        """The leftovers of an interrupted run must not be served as findings."""
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        payload = dict(self.client.scroll("test_filings", limit=1)[0][0].payload)
        payload["document_id"] = 999_999
        self.insert_point(payload)

        result = self.search(query="export controls")

        self.assertEqual(result.status, STATUS_OK)
        for passage in result.passages:
            self.assertNotEqual(passage.document_id, 999_999)

    def test_a_point_whose_payload_disagrees_with_the_manifest_is_dropped(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        payload = dict(self.client.scroll("test_filings", limit=1)[0][0].payload)
        payload["extracted_text_sha256"] = "b" * 64
        self.insert_point(payload)

        result = self.search(query="export controls")

        self.assertTrue(all(p.extracted_text_sha256 != "b" * 64 for p in result.passages))

    def test_dropping_hits_is_explained(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        payload = dict(self.client.scroll("test_filings", limit=1)[0][0].payload)
        payload["document_id"] = 999_999
        self.insert_point(payload)

        result = self.search(query="export controls")

        self.assertTrue(
            any("dropped because" in w for w in result.warnings), result.warnings
        )

    def test_asking_for_more_than_exist_returns_what_there_is_and_says_so(self):
        company = self.add_company()
        self.add_document(company)
        self.run_index()

        result = self.search(query="export controls", top_k=20)

        self.assertEqual(result.status, STATUS_OK)
        if len(result.passages) < 20:
            self.assertTrue(
                any("were asked for" in w for w in result.warnings), result.warnings
            )


class IsolationTests(RetrievalTestCase):
    def test_a_search_for_one_company_never_returns_another(self):
        first = self.add_company("NVDA", CIK)
        second = self.add_company("AMD", "0000002488")
        self.add_document(first, accession="0001045810-26-000075")
        self.add_document(
            second, accession="0000002488-26-000001", document_name="amd.htm"
        )
        index_company(
            self.session, symbol="NVDA", store=self.store, embedder=self.embedder
        )
        index_company(
            self.session, symbol="AMD", store=self.store, embedder=self.embedder
        )

        result = search_filings(
            self.session, symbol="NVDA", query="export controls", as_of=AS_OF,
            top_k=5, store=self.store, embedder=self.embedder,
        )

        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue(result.passages)
        for passage in result.passages:
            self.assertEqual(passage.symbol, "NVDA")
            self.assertEqual(passage.company_id, first.id)


if __name__ == "__main__":
    unittest.main()
