"""Capability C: retrieved passages, and the difference between "nothing" and "broken".

Retrieval itself -- the statuses, the cutoff filter, the manifest validation -- is tested in
`test_filing_retrieval.py`. What is tested here is what the *tool* promises on top: that a
broken index is `failed` while an empty result is `unavailable`, that citations are never
trimmed even when the quoted text is, that the search never indexes or creates anything, and
that company isolation and the acceptance cutoff survive the wrapper.

The collection is a real, in-process Qdrant and the database is real PostgreSQL; only the
embedding model is replaced. Reuses `IndexingTestCase` from `test_filing_index.py`.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date
from unittest import mock

from app.tools import filing_search
from app.tools.filing_search import FilingSearchRequest
from app.tools.results import ToolStatus
from app.vector_store import VectorStoreUnavailableError
from tests.search_doubles import FailingEmbedder
from tests.test_filing_index import CIK, TEXT, IndexingTestCase  # noqa: F401
from tests.test_filing_retrieval import AS_OF, BEFORE_THE_10Q  # noqa: F401


class FilingSearchToolTestCase(IndexingTestCase):
    """An indexed company, plus the tool on top of it."""

    def setUp(self) -> None:
        super().setUp()
        self.company = self.add_company()
        self.document = self.add_document(self.company)
        from app.filing_index import index_company

        index_company(
            self.session,
            symbol="NVDA",
            store=self.store,
            embedder=self.embedder,
        )
        self.session.flush()

    def search(self, **overrides):
        arguments = {
            "symbol": "NVDA",
            "question": "export controls",
            "as_of": AS_OF,
            "top_k": 3,
        }
        arguments.update(overrides)
        return filing_search.run(
            self.session,
            FilingSearchRequest(**arguments),
            store=self.store,
            embedder=self.embedder,
        )


class SuccessTests(FilingSearchToolTestCase):
    def test_a_match_returns_passages_with_their_full_citations(self):
        result = self.search(top_k=1)

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertIsNone(result.reason)
        self.assertEqual(len(result.data["passages"]), 1)

        passage = result.data["passages"][0]
        self.assertEqual(passage["filing"]["accession_number"], "0001045810-26-000075")
        self.assertEqual(passage["filing"]["form_type"], "10-Q")
        self.assertEqual(passage["company"]["symbol"], "NVDA")
        self.assertEqual(passage["company"]["cik"], CIK)
        self.assertTrue(passage["filing"]["source_url"].startswith("https://www.sec.gov/"))
        # The fixture stores no section map, so the passage honestly says "unknown" rather
        # than guessing a section name.
        self.assertEqual(passage["section"], "unknown")
        # A similarity, never a probability or a percentage.
        self.assertIsInstance(passage["similarity"], float)
        self.assertLessEqual(passage["similarity"], 1.0)

    def test_the_offsets_still_point_at_the_stored_text(self):
        """What makes a passage checkable: it is a slice of the document it cites."""
        result = self.search(top_k=1)

        passage = result.data["passages"][0]
        start = passage["offsets"]["start"]
        end = passage["offsets"]["end"]
        self.assertEqual(passage["text"], TEXT[start:end])

    def test_the_evidence_caveat_is_on_the_result(self):
        result = self.search()

        self.assertIn(
            "retrieved by similarity", " ".join(result.warnings)
        )
        self.assertIn("not mean the question is answered", " ".join(result.warnings))

    def test_returning_fewer_than_asked_is_partial_rather_than_a_quiet_shortfall(self):
        result = self.search(top_k=3)

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertLess(len(result.data["passages"]), 3)
        self.assertIn("where 3 were asked for", " ".join(result.warnings))


class FailureVersusEmptyTests(FilingSearchToolTestCase):
    """The distinction this tool exists to make: an outage is not an absence of evidence."""

    def test_an_unreachable_index_is_failed_not_unavailable(self):
        with mock.patch.object(
            type(self.store),
            "exists",
            side_effect=VectorStoreUnavailableError("qdrant is unreachable"),
        ):
            with self.assertLogs("app.tools.filing_search", level="WARNING") as logged:
                result = self.search()

        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertEqual(result.reason, "index_unavailable")
        self.assertIsNone(result.data)
        self.assertIn("could not be reached", " ".join(result.warnings))
        # The answer is sanitised; the detail is in the log rather than lost.
        self.assertIn("qdrant is unreachable", "\n".join(logged.output))
        self.assertNotIn("qdrant is unreachable", " ".join(result.warnings))

    def test_an_unusable_model_is_failed_not_unavailable(self):
        with self.assertLogs("app.tools.filing_search", level="WARNING"):
            result = filing_search.run(
                self.session,
                FilingSearchRequest(
                    symbol="NVDA", question="export controls", as_of=AS_OF, top_k=1
                ),
                store=self.store,
                embedder=FailingEmbedder(),
            )

        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertEqual(result.reason, "model_unavailable")
        self.assertIsNone(result.data)
        self.assertIn("embedding model could not be used", " ".join(result.warnings))

    def test_a_healthy_index_with_no_match_is_unavailable(self):
        """Reached by dropping every hit in validation, which is how it really happens."""
        with mock.patch(
            "app.filing_index._payload_matches_manifest", return_value=False
        ):
            result = self.search()

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "no_matching_results")
        self.assertIsNone(result.data)
        # The retrieval result's own sentence is the useful part, and it is not lost.
        self.assertIn("no passage passed validation", " ".join(result.warnings))

    def test_an_unknown_company_is_its_own_reason(self):
        result = self.search(symbol="AAPL")

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "unknown_company")

    def test_nothing_decided_before_the_cutoff_is_its_own_reason(self):
        """The 10-Q was accepted on 2026-08-26; before then it was not public."""
        result = self.search(as_of=BEFORE_THE_10Q)

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "no_eligible_documents")
        self.assertIn("accepted before", " ".join(result.warnings))


class CitationIntegrityTests(FilingSearchToolTestCase):
    def test_shortening_a_passage_leaves_every_citation_field_intact(self):
        full = self.search(top_k=1).data["passages"][0]

        with mock.patch.object(filing_search, "MAX_PASSAGE_CHARS", 100):
            trimmed = self.search(top_k=1).data["passages"][0]

        self.assertTrue(trimmed["text_truncated"])
        self.assertEqual(len(trimmed["text"]), 100)
        self.assertEqual(trimmed["text_char_count"], full["text_char_count"])

        # Everything that makes the passage checkable is untouched.
        for field in ("accession_number", "source_url", "section", "similarity"):
            scope = "filing" if field in ("accession_number", "source_url") else None
            before = full[scope][field] if scope else full[field]
            after = trimmed[scope][field] if scope else trimmed[field]
            self.assertEqual(before, after, field)

        self.assertEqual(trimmed["offsets"], full["offsets"])
        self.assertEqual(trimmed["document"], full["document"])

    def test_shortening_is_reported_in_the_payload_and_the_warnings(self):
        with mock.patch.object(filing_search, "MAX_PASSAGE_CHARS", 50):
            result = self.search(top_k=1)

        self.assertEqual(result.data["truncation"]["passages_shortened"], 1)
        self.assertEqual(result.data["truncation"]["max_passage_chars"], 50)
        self.assertIn("shortened to 50 characters", " ".join(result.warnings))

    def test_a_passage_that_fits_is_not_marked_as_shortened(self):
        result = self.search(top_k=1)

        passage = result.data["passages"][0]
        self.assertFalse(passage["text_truncated"])
        self.assertEqual(result.data["truncation"]["passages_shortened"], 0)
        self.assertNotIn("shortened", " ".join(result.warnings))


class ReadOnlyTests(FilingSearchToolTestCase):
    def test_searching_writes_no_point_and_creates_no_collection(self):
        collections_before = {
            item.name for item in self.client.get_collections().collections
        }
        points_before = self.client.count(collection_name="test_filings", exact=True).count

        self.search()
        self.search(question="risk factors", top_k=5)

        self.assertEqual(
            {item.name for item in self.client.get_collections().collections},
            collections_before,
        )
        self.assertEqual(
            self.client.count(collection_name="test_filings", exact=True).count,
            points_before,
        )

    def test_searching_never_indexes_a_document(self):
        """Retrieval must not repair, reindex or ingest, however stale the index looks."""
        with mock.patch("app.filing_index.index_company") as reindex:
            self.search()

        reindex.assert_not_called()

    def test_the_tool_source_never_reaches_for_indexing(self):
        from pathlib import Path

        source = Path(filing_search.__file__).read_text()
        self.assertNotIn("index_company", source)
        self.assertNotIn("ensure_collection", source)

    def test_a_caller_supplied_store_is_not_closed(self):
        """The store belongs to whoever opened it; only a self-opened one is closed."""
        with mock.patch.object(self.store, "close") as closed:
            self.search()

        closed.assert_not_called()


class IsolationTests(FilingSearchToolTestCase):
    def test_another_companys_documents_are_not_searchable(self):
        other = self.add_company(ticker="AAPL", cik="0000320193")
        self.add_document(
            other,
            accession="0000320193-26-000020",
            document_name="aapl-20260627.htm",
            text="Apple discusses export controls and tariffs at length. " * 20,
        )
        from app.filing_index import index_company

        index_company(
            self.session, symbol="AAPL", store=self.store, embedder=self.embedder
        )
        self.session.flush()

        result = self.search(symbol="NVDA", top_k=5)
        accessions = {p["filing"]["accession_number"] for p in result.data["passages"]}

        self.assertNotIn("0000320193-26-000020", accessions)
        self.assertTrue(accessions)

    def test_the_cutoff_excludes_a_filing_accepted_after_it(self):
        later_accession = "0001045810-26-000076"
        self.add_document(
            self.company,
            accession=later_accession,
            document_name="nvda-later.htm",
            text="Export controls are discussed again in a later filing. " * 20,
            acceptance=_days_after(AS_OF, 30),
        )
        self.session.flush()
        from app.filing_index import index_company

        index_company(
            self.session, symbol="NVDA", store=self.store, embedder=self.embedder
        )
        self.session.flush()

        result = self.search(top_k=10)
        accessions = {p["filing"]["accession_number"] for p in result.data["passages"]}

        self.assertNotIn(later_accession, accessions)
        self.assertIn("0001045810-26-000075", accessions)


def _days_after(as_of: date, days: int):
    from datetime import datetime, timedelta, timezone

    return datetime.combine(as_of + timedelta(days=days), datetime.min.time(), tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main()
