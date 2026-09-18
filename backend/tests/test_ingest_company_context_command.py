"""Filing and exhibit selection for the company-context command.

Selection is a pure function of the submissions feed, so it is tested from fixtures rather
than from HTTP. The exhibit rule is tested the same way, from index entries.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from app.analysis import information_cutoff
from app.ingest_company_context import (
    MAX_EIGHT_K,
    MAX_EXHIBITS_PER_FILING,
    _select_exhibits,
    select_filings,
)
from app.sec_edgar import DiscoveryResult, FilingIndexEntry, SecFilingRef

AS_OF = date(2026, 9, 17)
CUTOFF = information_cutoff(AS_OF)


def acc(n: int) -> str:
    """A well-formed accession number.

    Real ones only: `archive_file_url` refuses anything that is not
    NNNNNNNNNN-NN-NNNNNN, which is the validation doing its job rather than an obstacle.
    """
    return f"0001045810-26-{n:06d}"


def ref(
    accession: str,
    form: str,
    filed: date,
    *,
    accepted: datetime | None = None,
    primary: str = "doc.htm",
) -> SecFilingRef:
    return SecFilingRef(
        accession_number=accession,
        form_type=form,
        filing_date=filed,
        acceptance_datetime=accepted,
        report_date=None,
        primary_document=primary,
    )


def accepted_on(day: date, hour: int = 21) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def discovery(*refs: SecFilingRef) -> DiscoveryResult:
    return DiscoveryResult(
        issuer_cik="0001045810",
        issuer_name="NVIDIA CORP",
        tickers=("NVDA",),
        exchanges=("Nasdaq",),
        recent_filings_scanned=1001,
        older_filing_files_not_searched=1478,
        filings=tuple(refs),
    )


class FilingSelectionTests(unittest.TestCase):
    def test_the_latest_10k_and_10q_before_the_cutoff_are_taken(self):
        selection = select_filings(
            discovery(
                ref(acc(1), "10-K", date(2025, 2, 26), accepted=accepted_on(date(2025, 2, 26))),
                ref(acc(2), "10-K", date(2026, 2, 25), accepted=accepted_on(date(2026, 2, 25))),
                ref(acc(3), "10-Q", date(2026, 5, 20), accepted=accepted_on(date(2026, 5, 20))),
                ref(acc(4), "10-Q", date(2026, 8, 26), accepted=accepted_on(date(2026, 8, 26))),
            ),
            cutoff=CUTOFF,
            as_of=AS_OF,
        )

        chosen = {filing.form_type: filing.accession_number for filing in selection.filings}
        self.assertEqual(chosen["10-K"], acc(2))
        self.assertEqual(chosen["10-Q"], acc(4))

    def test_a_filing_accepted_after_the_cutoff_is_not_eligible(self):
        selection = select_filings(
            discovery(
                ref(acc(5), "10-K", date(2026, 2, 25), accepted=accepted_on(date(2026, 2, 25))),
                ref(acc(6), "10-Q", date(2026, 9, 17),
                    accepted=datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)),
            ),
            cutoff=CUTOFF,
            as_of=AS_OF,
        )

        forms = {filing.form_type for filing in selection.filings}
        self.assertIn("10-K", forms)
        self.assertNotIn("10-Q", forms)
        self.assertTrue(any("No original 10-Q" in note for note in selection.limitations))

    def test_a_missing_10k_is_reported_as_a_limit_of_the_search(self):
        selection = select_filings(
            discovery(ref(acc(7), "10-Q", date(2026, 8, 26), accepted=accepted_on(date(2026, 8, 26)))),
            cutoff=CUTOFF,
            as_of=AS_OF,
        )

        note = next(n for n in selection.limitations if "10-K" in n)
        self.assertIn("not a statement that the company has never filed one", note)

    def test_an_unknown_acceptance_timestamp_makes_a_filing_ineligible(self):
        selection = select_filings(
            discovery(ref(acc(5), "10-K", date(2026, 2, 25), accepted=None)),
            cutoff=CUTOFF,
            as_of=AS_OF,
        )

        self.assertEqual(selection.filings, ())
        self.assertTrue(
            any("no acceptance timestamp" in note for note in selection.limitations)
        )

    def test_amendments_are_reported_and_never_selected(self):
        selection = select_filings(
            discovery(
                ref(acc(5), "10-K", date(2026, 2, 25), accepted=accepted_on(date(2026, 2, 25))),
                ref(acc(8), "10-K/A", date(2026, 3, 1), accepted=accepted_on(date(2026, 3, 1))),
            ),
            cutoff=CUTOFF,
            as_of=AS_OF,
        )

        self.assertEqual(selection.amendment_accessions, (acc(8),))
        self.assertEqual(
            [filing.accession_number for filing in selection.filings], [acc(5)]
        )
        note = next(n for n in selection.limitations if "amendment" in n.lower())
        self.assertIn("not applied", note)
        self.assertIn("amendment resolution is not implemented", note)

    def test_an_amendment_after_the_cutoff_is_not_reported(self):
        selection = select_filings(
            discovery(
                ref(acc(8), "10-K/A", date(2026, 9, 17),
                    accepted=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)),
            ),
            cutoff=CUTOFF,
            as_of=AS_OF,
        )

        self.assertEqual(selection.amendment_accessions, ())


class EightKSelectionTests(unittest.TestCase):
    def window_day(self, days_ago: int) -> date:
        return AS_OF - timedelta(days=days_ago)

    def test_only_8ks_inside_the_window_are_taken(self):
        selection = select_filings(
            discovery(
                ref(acc(9), "8-K", self.window_day(10), accepted=accepted_on(self.window_day(10))),
                ref(acc(10), "8-K", self.window_day(120), accepted=accepted_on(self.window_day(120))),
            ),
            cutoff=CUTOFF,
            as_of=AS_OF,
        )

        self.assertEqual([f.accession_number for f in selection.filings], [acc(9)])

    def test_the_most_recent_8ks_come_first_and_are_capped(self):
        days = [1, 5, 20, 40, 60]
        selection = select_filings(
            discovery(*[
                ref(acc(d), "8-K", self.window_day(d), accepted=accepted_on(self.window_day(d)))
                for d in days
            ]),
            cutoff=CUTOFF,
            as_of=AS_OF,
        )

        self.assertEqual(len(selection.filings), MAX_EIGHT_K)
        self.assertEqual(
            [f.accession_number for f in selection.filings],
            [acc(1), acc(5), acc(20)],
        )
        self.assertTrue(any("most recent" in note for note in selection.limitations))

    def test_an_empty_window_says_so_without_claiming_anything_about_the_company(self):
        selection = select_filings(discovery(), cutoff=CUTOFF, as_of=AS_OF)

        note = next(n for n in selection.limitations if "8-K" in n)
        self.assertIn("limit of the window and the search", note)

    def test_an_8k_amendment_is_not_selected(self):
        selection = select_filings(
            discovery(
                ref(acc(11), "8-K/A", self.window_day(5),
                    accepted=accepted_on(self.window_day(5))),
            ),
            cutoff=CUTOFF,
            as_of=AS_OF,
        )

        self.assertEqual(selection.filings, ())
        self.assertEqual(selection.amendment_accessions, (acc(11),))


class ExhibitSelectionTests(unittest.TestCase):
    def entry(self, name: str, doc_type: str, sequence: int) -> FilingIndexEntry:
        return FilingIndexEntry(
            sequence=sequence, description=doc_type, document_name=name,
            document_type=doc_type,
        )

    def test_only_ex_99_entries_are_considered(self):
        chosen, omitted = _select_exhibits([
            self.entry("nvda-20260826.htm", "8-K", 1),
            self.entry("q2fy27pr.htm", "EX-99.1", 2),
            self.entry("nvda-20260826.xsd", "EX-101.SCH", 4),
            self.entry("nvdalogoa19.jpg", "GRAPHIC", 7),
        ])

        self.assertEqual([e.document_name for e in chosen], ["q2fy27pr.htm"])
        self.assertEqual(omitted, [])

    def test_a_pdf_exhibit_is_reported_as_omitted(self):
        chosen, omitted = _select_exhibits([
            self.entry("results.pdf", "EX-99.1", 2),
            self.entry("q2fy27pr.htm", "EX-99.2", 3),
        ])

        self.assertEqual([e.document_name for e in chosen], ["q2fy27pr.htm"])
        self.assertEqual(len(omitted), 1)
        self.assertIn("results.pdf", omitted[0])
        self.assertIn("PDF", omitted[0])

    def test_exhibits_are_ordered_by_exhibit_number_not_index_order(self):
        chosen, _ = _select_exhibits([
            self.entry("second.htm", "EX-99.2", 3),
            self.entry("first.htm", "EX-99.1", 2),
        ])

        self.assertEqual([e.document_name for e in chosen], ["first.htm", "second.htm"])

    def test_no_more_than_the_cap_are_taken_and_the_rest_are_reported(self):
        chosen, omitted = _select_exhibits([
            self.entry("a.htm", "EX-99.1", 1),
            self.entry("b.htm", "EX-99.2", 2),
            self.entry("c.htm", "EX-99.3", 3),
        ])

        self.assertEqual(len(chosen), MAX_EXHIBITS_PER_FILING)
        self.assertEqual(len(omitted), 1)
        self.assertIn("c.htm", omitted[0])

    def test_a_filing_with_no_exhibits_yields_nothing_and_says_nothing(self):
        chosen, omitted = _select_exhibits([
            self.entry("nvda-20260902.htm", "8-K", 1),
            self.entry("nvda-20260902.xsd", "EX-101.SCH", 2),
        ])

        self.assertEqual(chosen, [])
        self.assertEqual(omitted, [])

    def test_a_text_exhibit_is_fetchable(self):
        chosen, _ = _select_exhibits([self.entry("release.txt", "EX-99.1", 1)])

        self.assertEqual([e.document_name for e in chosen], ["release.txt"])


if __name__ == "__main__":
    unittest.main()
