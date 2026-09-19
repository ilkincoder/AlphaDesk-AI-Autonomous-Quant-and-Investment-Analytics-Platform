"""The evidence map: what the tools returned, and what may be cited.

This is the layer that makes a citation checkable. Everything here is about the difference
between "the model said so" and "the application recorded it" -- the map is built only from
tool results, identifiers are assigned here rather than chosen by the model, and citation
metadata is read back out of the map so an invented URL cannot survive into an answer.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest

from app.agent.evidence import (
    MAX_ENTRIES,
    MAX_PASSAGE_CHARS,
    MAX_SUMMARY_CHARS,
    TRUNCATION_MARKER,
    EvidenceMap,
)

PASSAGE = {
    "text": "Export controls restrict the sale of our products in several markets.",
    "similarity": 0.771,
    "section": "risk_factors",
    "offsets": {"start": 100, "end": 164},
    "filing": {
        "accession_number": "0001045810-26-000075",
        "form_type": "10-Q",
        "acceptance_datetime": "2026-08-26T20:36:00Z",
        "report_date": "2026-07-26",
        "source_url": "https://www.sec.gov/Archives/edgar/data/1045810/x.htm",
    },
    "document": {
        "id": 7,
        "name": "nvda-20260726.htm",
        "role": "primary",
        "content_sha256": "a" * 64,
    },
}


def retrieval_result(*passages):
    return {"passages": list(passages)}


class FilingPassageTests(unittest.TestCase):
    def test_each_passage_becomes_its_own_citable_entry(self):
        """A citation points at a passage, not at the search that found it."""
        evidence = EvidenceMap()

        refs = evidence.add_filing_passages(
            tool="filing_evidence_search",
            symbol="NVDA",
            status="ok",
            data=retrieval_result(PASSAGE, {**PASSAGE, "text": "A second passage."}),
        )

        self.assertEqual(refs, ["E1", "E2"])
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence.get("E1").kind, "filing_passage")

    def test_the_citation_comes_from_the_result_and_not_from_the_model(self):
        evidence = EvidenceMap()
        evidence.add_filing_passages(
            tool="filing_evidence_search", symbol="NVDA", status="ok",
            data=retrieval_result(PASSAGE),
        )

        citation = evidence.get("E1").citation
        self.assertEqual(citation["accession_number"], "0001045810-26-000075")
        self.assertEqual(citation["section"], "risk_factors")
        self.assertEqual(citation["similarity"], 0.771)
        self.assertEqual(
            citation["source_url"],
            "https://www.sec.gov/Archives/edgar/data/1045810/x.htm",
        )
        # The hash, so a reader can check the passage against the stored document.
        self.assertEqual(citation["content_sha256"], "a" * 64)

    def test_a_search_that_found_nothing_creates_no_citable_entry(self):
        """`unavailable` is an answer, but it is not evidence."""
        evidence = EvidenceMap()

        refs = evidence.add_filing_passages(
            tool="filing_evidence_search", symbol="NVDA", status="unavailable",
            data={"passages": []},
        )

        self.assertEqual(refs, [])
        self.assertEqual(len(evidence), 0)

    def test_a_passage_longer_than_the_allowance_is_marked_as_trimmed(self):
        evidence = EvidenceMap()
        evidence.add_filing_passages(
            tool="filing_evidence_search", symbol="NVDA", status="ok",
            data=retrieval_result({**PASSAGE, "text": "x" * (MAX_PASSAGE_CHARS + 500)}),
        )

        item = evidence.get("E1")
        self.assertTrue(item.trimmed)
        self.assertTrue(item.summary["quoted_text"].endswith(TRUNCATION_MARKER))
        self.assertIn("trimmed", item.render())


class ToolResultTests(unittest.TestCase):
    def test_a_structured_result_becomes_one_entry(self):
        evidence = EvidenceMap()

        reference = evidence.add_tool_result(
            tool="market_insider_analysis",
            symbol="NVDA",
            status="ok",
            label="market_insider_analysis result for NVDA",
            summary={"prices": {"change_percent": "0.1598"}, "overall_conclusion":
                     "insufficient_coverage"},
        )

        self.assertEqual(reference, "E1")
        self.assertEqual(evidence.get("E1").kind, "tool_result")

    def test_a_full_map_reports_what_it_dropped(self):
        """A short citation list must never be mistaken for a complete one."""
        evidence = EvidenceMap(max_entries=2)
        for index in range(4):
            evidence.add_tool_result(
                tool="portfolio_context", symbol="NVDA", status="ok",
                label=f"result {index}", summary={"n": index},
            )

        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence.overflowed, 2)

    def test_a_long_summary_value_is_trimmed_without_breaking_the_entry(self):
        evidence = EvidenceMap()
        evidence.add_tool_result(
            tool="market_insider_analysis", symbol="NVDA", status="ok", label="x",
            summary={"notes": "y" * (MAX_SUMMARY_CHARS + 100)},
        )

        item = evidence.get("E1")
        self.assertTrue(item.trimmed)
        # Trimmed by value, so the entry is still a readable mapping rather than half a
        # JSON document.
        self.assertIn("notes", item.summary)


class CitationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = EvidenceMap()
        self.evidence.add_tool_result(
            tool="market_insider_analysis", symbol="NVDA", status="ok", label="market", summary={}
        )
        self.evidence.add_filing_passages(
            tool="filing_evidence_search", symbol="NVDA", status="ok",
            data=retrieval_result(PASSAGE),
        )

    def test_known_references_validate_and_unknown_ones_are_named(self):
        valid, invalid = self.evidence.validate(["E1", "E9", "E2", "E9", "E1"])

        self.assertEqual(valid, ["E1", "E2"])
        self.assertEqual(invalid, ["E9"])

    def test_a_citation_block_is_resolved_from_the_map(self):
        citations = self.evidence.citations_for(["E2"])

        self.assertEqual(citations[0]["reference"], "E2")
        self.assertEqual(
            citations[0]["filing"]["accession_number"], "0001045810-26-000075"
        )

    def test_an_unknown_reference_resolves_to_nothing_rather_than_to_a_guess(self):
        """The answer is refused elsewhere; this must not substitute anything."""
        self.assertEqual(self.evidence.citations_for(["E99"]), [])

    def test_the_render_is_bounded_and_says_when_entries_are_hidden(self):
        rendered = self.evidence.render(limit=1)

        self.assertIn("[E1]", rendered)
        self.assertNotIn("[E2]", rendered)
        self.assertIn("1 further evidence entries are not shown", rendered)

    def test_the_render_says_a_similarity_is_not_a_probability(self):
        rendered = self.evidence.render()

        self.assertIn("not a probability", rendered)

    def test_an_empty_map_says_so(self):
        self.assertIn("no evidence", EvidenceMap().render().lower())

    def test_the_entry_limit_is_a_real_ceiling(self):
        self.assertGreater(MAX_ENTRIES, 0)
        evidence = EvidenceMap(max_entries=MAX_ENTRIES)
        for index in range(MAX_ENTRIES + 5):
            evidence.add_tool_result(
                tool="portfolio_context", symbol="NVDA", status="ok", label=str(index),
                summary={},
            )
        self.assertEqual(len(evidence), MAX_ENTRIES)


class RenderingTests(unittest.TestCase):
    def test_a_passage_renders_with_its_filing_metadata(self):
        evidence = EvidenceMap()
        evidence.add_filing_passages(
            tool="filing_evidence_search", symbol="NVDA", status="ok",
            data=retrieval_result(PASSAGE),
        )

        rendered = evidence.render()

        self.assertIn("0001045810-26-000075", rendered)
        self.assertIn("risk_factors", rendered)
        self.assertIn("https://www.sec.gov/", rendered)

    def test_the_render_never_leaks_a_non_string_as_a_python_repr(self):
        """The prompt is read by a model; `Decimal('1')` is not a number it can use."""
        evidence = EvidenceMap()
        evidence.add_tool_result(
            tool="company_financial_facts", symbol="NVDA", status="ok", label="facts",
            summary={"value": "96221000000", "period": ["2026-04-27", "2026-07-26"]},
        )

        rendered = evidence.render()
        self.assertIn("96221000000", rendered)
        self.assertNotIn("Decimal", rendered)


if __name__ == "__main__":
    unittest.main()
