"""HTML to text, and finding sections in it. Pure: strings in, values out.

The fixtures reproduce the structures the real NVDA filings actually have -- hidden XBRL
context blocks, elements hidden with `display:none`, financial tables, and a contents page
that states every Item heading before the section does. Where a shape came from a real
document, the test says so.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest

from app.htmltext import EXTRACTION_VERSION, extract


def page(body: str) -> str:
    return f"<html><head><title>Ignore me</title></head><body>{body}</body></html>"


class HiddenContentTests(unittest.TestCase):
    def test_script_and_style_content_is_dropped(self):
        document = extract(
            page(
                "<style>.x{color:red}</style>"
                "<p>Visible.</p>"
                "<script>var secret = 'should not appear';</script>"
            ),
            form_type="10-K",
        )

        self.assertIn("Visible.", document.text)
        self.assertNotIn("color:red", document.text)
        self.assertNotIn("should not appear", document.text)

    def test_display_none_content_is_dropped(self):
        """The stored 10-K has 398 of these, so this is not a hypothetical."""
        document = extract(
            page(
                '<p>Shown.</p><div style="display:none">Hidden text.</div>'
                '<div style="DISPLAY : none">Also hidden.</div>'
            ),
            form_type="10-K",
        )

        self.assertIn("Shown.", document.text)
        self.assertNotIn("Hidden text.", document.text)
        self.assertNotIn("Also hidden.", document.text)

    def test_hidden_xbrl_context_blocks_are_dropped(self):
        document = extract(
            page(
                "<ix:header><ix:hidden><xbrli:context id='c1'>"
                "<xbrli:entity><xbrli:identifier>0001045810</xbrli:identifier></xbrli:entity>"
                "</xbrli:context></ix:hidden></ix:header>"
                "<p>Revenue was $96,221 million.</p>"
            ),
            form_type="10-K",
        )

        self.assertIn("Revenue was $96,221 million.", document.text)
        self.assertNotIn("xbrli:context", document.text)
        self.assertNotIn("0001045810", document.text)

    def test_visible_inline_xbrl_text_is_kept(self):
        """Inline XBRL tags a number where it is already displayed, so dropping it would
        delete the figure from the document."""
        document = extract(
            page(
                "<p>Revenue"
                "<ix:nonFraction name='us-gaap:Revenues' contextRef='c1' unitRef='usd'>"
                "96,221</ix:nonFraction> million.</p>"
            ),
            form_type="10-K",
        )

        self.assertIn("96,221", document.text)

    def test_a_visible_fact_is_not_duplicated_from_a_hidden_one(self):
        document = extract(
            page(
                "<p>Revenue 96,221</p>"
                "<ix:header><ix:hidden>Revenue 96,221</ix:hidden></ix:header>"
            ),
            form_type="10-K",
        )

        self.assertEqual(document.text.count("96,221"), 1)


class StructureTests(unittest.TestCase):
    def test_table_rows_and_cells_keep_their_boundaries(self):
        document = extract(
            page(
                "<table>"
                "<tr><th>Revenue</th><th>$ 96,221</th><th>$ 46,743</th></tr>"
                "<tr><td>Cost of revenue</td><td>24,079</td><td>12,890</td></tr>"
                "</table>"
            ),
            form_type="10-Q",
        )
        lines = [line for line in document.text.split("\n") if line.strip()]

        self.assertIn("Revenue\t$ 96,221\t$ 46,743", lines)
        self.assertIn("Cost of revenue\t24,079\t12,890", lines)

    def test_paragraphs_are_separated(self):
        document = extract(page("<p>First.</p><p>Second.</p>"), form_type="10-K")

        self.assertIn("First.\nSecond.", document.text)

    def test_entities_and_signs_survive(self):
        document = extract(
            page("<p>AT&amp;T reported &#8217;adjusted&#8217; revenue of $(1,234) and -5%.</p>"),
            form_type="10-K",
        )

        self.assertIn("AT&T", document.text)
        self.assertIn("’adjusted’", document.text)
        self.assertIn("$(1,234)", document.text)
        self.assertIn("-5%", document.text)

    def test_headings_are_preserved_as_text(self):
        document = extract(
            page("<h2>Item 1A. Risk Factors</h2><p>Risks follow.</p>"), form_type="10-K"
        )

        self.assertIn("Item 1A. Risk Factors", document.text)

    def test_no_tags_survive(self):
        document = extract(
            page("<div><span>text</span><br/><b>bold</b></div>"), form_type="10-K"
        )

        self.assertNotIn("<", document.text)
        self.assertIn("text", document.text)
        self.assertIn("bold", document.text)


class SectionTests(unittest.TestCase):
    # The shape of the real 10-K: every heading stated once in a tightly packed contents
    # page, then again where the section actually begins, thousands of characters later.
    CONTENTS_PAGE = (
        "<p>Item 1. Business</p><p>Item 1A. Risk Factors</p>"
        "<p>Item 7. Management's Discussion and Analysis</p>"
        "<p>Item 8. Financial Statements</p>"
    )
    FILLER = "<p>" + ("filler text. " * 120) + "</p>"

    def ten_k(self) -> str:
        return page(
            self.CONTENTS_PAGE
            + self.FILLER
            + "<p>Item 1. Business</p><p>We design GPUs.</p>"
            + self.FILLER
            + "<p>Item 1A. Risk Factors</p><p>Risks.</p>"
            + self.FILLER
            + "<p>Item 7. Management's Discussion and Analysis</p><p>Discussion.</p>"
            + self.FILLER
            + "<p>Item 8. Financial Statements</p><p>Statements.</p>"
        )

    def test_the_contents_page_is_not_mistaken_for_the_sections(self):
        """The rule that matters: a first-match rule would return the contents page."""
        document = extract(self.ten_k(), form_type="10-K")
        contents_end = document.text.find("filler text.")

        for name, span in document.sections.items():
            self.assertGreater(
                span.start,
                contents_end,
                f"{name} was located inside the contents page",
            )

    def test_all_four_10k_sections_are_found(self):
        document = extract(self.ten_k(), form_type="10-K")

        self.assertEqual(
            set(document.sections),
            {"business", "risk_factors", "management_discussion", "financial_statements"},
        )

    def test_sections_appear_in_document_order(self):
        document = extract(self.ten_k(), form_type="10-K")
        starts = [span.start for span in document.sections.values()]

        self.assertEqual(starts, sorted(starts))

    def test_a_section_that_is_absent_is_reported_not_guessed(self):
        document = extract(
            page("<p>Item 1. Business</p><p>Item 1A. Risk Factors</p>"), form_type="10-K"
        )

        self.assertNotIn("financial_statements", document.sections)
        self.assertTrue(
            any("could not be located" in note for note in document.limitations),
            document.limitations,
        )
        # The text is kept regardless, so nothing was discarded for want of a section.
        self.assertIn("Item 1. Business", document.text)

    def test_a_document_with_no_contents_page_is_read_normally(self):
        document = extract(
            page(
                self.FILLER + "<p>Item 1A. Risk Factors</p><p>Risks.</p>" + self.FILLER
            ),
            form_type="10-K",
        )

        self.assertIn("risk_factors", document.sections)

    def test_an_unknown_form_type_yields_no_sections_and_says_so(self):
        document = extract(page("<p>Item 1. Business</p>"), form_type="8-K")

        self.assertEqual(document.sections, {})
        self.assertTrue(
            any("No section patterns" in note for note in document.limitations),
            document.limitations,
        )

    def test_the_10q_uses_its_own_item_numbers(self):
        document = extract(
            page(
                "<p>Item 1. Financial Statements</p>"
                + self.FILLER
                + "<p>Item 2. Management’s Discussion and Analysis</p>"
                + self.FILLER
                + "<p>Item 1A. Risk Factors</p>"
            ),
            form_type="10-Q",
        )

        self.assertEqual(
            set(document.sections),
            {"financial_statements", "management_discussion", "risk_factors"},
        )

    def test_the_curly_apostrophe_in_managements_is_matched(self):
        """The real 10-Q writes "Management’s" with a right single quote."""
        document = extract(
            page(self.FILLER + "<p>Item 2. Management’s Discussion and Analysis</p>"),
            form_type="10-Q",
        )

        self.assertIn("management_discussion", document.sections)


class EmptyDocumentTests(unittest.TestCase):
    def test_an_empty_document_is_reported_not_crashed(self):
        document = extract("", form_type="10-K")

        self.assertEqual(document.text, "")
        self.assertTrue(document.limitations)

    def test_a_document_with_no_readable_text_says_so(self):
        document = extract(
            "<html><body><script>only code here</script></body></html>", form_type="10-K"
        )

        self.assertEqual(document.text, "")
        self.assertTrue(
            any("No readable text" in note for note in document.limitations),
            document.limitations,
        )

    def test_every_extraction_is_stamped_with_a_version(self):
        document = extract(page("<p>Text.</p>"), form_type="10-K")

        self.assertEqual(document.extraction_version, EXTRACTION_VERSION)


if __name__ == "__main__":
    unittest.main()
