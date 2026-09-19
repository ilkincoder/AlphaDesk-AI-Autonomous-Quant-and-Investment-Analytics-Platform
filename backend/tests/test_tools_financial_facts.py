"""Capability B: one reported figure, one period, one filing, and the cutoff that dates it.

The parsing and storage of Company Facts is tested in `test_company_facts.py` and
`test_context_ingestion.py`. What is tested here is what the *tool* promises: that a period is
a start and an end rather than an end alone, that the two revenue concepts are never combined,
that an ambiguous selection returns labelled candidates rather than a pick or a sum, that
availability follows the acceptance timestamp, and that absence is reported as absence rather
than as zero.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone

from pydantic import ValidationError

from app.tools import financial_facts
from app.tools.financial_facts import FinancialFactsRequest
from app.tools.results import ToolStatus
from tests.test_tools_contracts import ToolTestCase

AS_OF = date(2026, 9, 17)
QUARTER_START = date(2026, 4, 27)
QUARTER_END = date(2026, 7, 26)
YEAR_START = date(2026, 1, 26)
TEN_Q = "0001045810-26-000075"
TEN_K = "0001045810-26-000021"


class FinancialFactsTestCase(ToolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.company = self.add_company()
        self.snapshot = self.add_snapshot(self.company)
        # The 10-Q accepted 2026-08-26, the 10-K accepted 2026-02-25 -- both before AS_OF.
        self.add_filing(
            self.company,
            TEN_Q,
            form_type="10-Q",
            acceptance=datetime(2026, 8, 26, 20, 36, tzinfo=timezone.utc),
            filing_date=date(2026, 8, 26),
            report_date=QUARTER_END,
        )
        self.add_filing(
            self.company,
            TEN_K,
            form_type="10-K",
            acceptance=datetime(2026, 2, 25, 21, 42, tzinfo=timezone.utc),
            filing_date=date(2026, 2, 25),
            report_date=date(2026, 1, 25),
        )

    def read(self, **overrides):
        arguments = {"symbol": "NVDA", "metric": "revenue", "as_of": AS_OF}
        arguments.update(overrides)
        return financial_facts.run(self.session, FinancialFactsRequest(**arguments))

    def add_quarter_and_year_to_date(self) -> None:
        """The pair that makes a bare period_end ambiguous: same end, different starts."""
        self.add_fact(
            self.company,
            self.snapshot,
            value="96221000000",
            period_start=QUARTER_START,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )
        self.add_fact(
            self.company,
            self.snapshot,
            value="177837000000",
            period_start=YEAR_START,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )


class SelectionTests(FinancialFactsTestCase):
    def test_a_bare_period_end_returns_candidates_rather_than_a_pick_or_a_sum(self):
        self.add_quarter_and_year_to_date()

        result = self.read(period_end=QUARTER_END)

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(result.reason, "multiple_candidates")
        self.assertEqual(result.data["selection"], "multiple_candidates")
        self.assertEqual(result.data["observations_total"], 2)

        periods = {
            (row["period_start"], row["value"]) for row in result.data["observations"]
        }
        self.assertEqual(
            periods,
            {
                (QUARTER_START.isoformat(), "96221000000"),
                (YEAR_START.isoformat(), "177837000000"),
            },
        )
        # Neither picked nor added: the two summed would be 274058000000, a figure the company
        # never reported for any period.
        self.assertNotIn("274058000000", str(result.data))
        self.assertIn("neither picked between nor added", " ".join(result.warnings))

    def test_an_exact_period_resolves_to_one_observation(self):
        self.add_quarter_and_year_to_date()

        result = self.read(period_start=QUARTER_START, period_end=QUARTER_END)

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.data["selection"], "single")
        self.assertEqual(len(result.data["observations"]), 1)
        self.assertEqual(result.data["observations"][0]["value"], "96221000000")

    def test_a_period_start_is_compared_exactly_and_not_as_a_range(self):
        """A year-to-date figure is not a match for the quarter inside it."""
        self.add_quarter_and_year_to_date()

        result = self.read(period_start=YEAR_START, period_end=QUARTER_END)

        self.assertEqual(result.data["observations"][0]["value"], "177837000000")

    def test_the_same_balance_from_two_filings_returns_both_comparatives(self):
        """A figure restated as a comparative in a later filing is a second fact."""
        instant = date(2026, 1, 25)
        for accession, value in ((TEN_K, "206803000000"), (TEN_Q, "206803000000")):
            self.add_fact(
                self.company,
                self.snapshot,
                concept="Assets",
                value=value,
                period_start=None,
                period_end=instant,
                accession=accession,
            )

        result = self.read(metric="total_assets", period_end=instant)

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(result.data["observations_total"], 2)
        self.assertEqual(
            {row["accession_number"] for row in result.data["observations"]},
            {TEN_K, TEN_Q},
        )

    def test_an_accession_narrows_to_the_filing_that_reported_it(self):
        self.add_quarter_and_year_to_date()

        result = self.read(
            period_end=QUARTER_END, accession_number=TEN_Q, period_start=QUARTER_START
        )

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.data["observations"][0]["accession_number"], TEN_Q)

    def test_a_unit_filter_that_matches_nothing_says_which_units_are_stored(self):
        self.add_quarter_and_year_to_date()

        result = self.read(unit="EUR")

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "no_observation_matches_selection")
        self.assertIn("Stored units: USD", " ".join(result.warnings))


class MetricSeparationTests(FinancialFactsTestCase):
    def test_the_two_revenue_concepts_are_never_returned_together(self):
        """They are different definitions, and a total of the two would be neither."""
        self.add_fact(
            self.company,
            self.snapshot,
            concept="Revenues",
            value="111",
            period_start=QUARTER_START,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )
        self.add_fact(
            self.company,
            self.snapshot,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value="222",
            period_start=QUARTER_START,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )

        first = self.read(metric="revenue")
        second = self.read(metric="revenue_contract_with_customer")

        self.assertEqual(first.data["concept"], "Revenues")
        self.assertEqual(first.data["observations"][0]["value"], "111")
        self.assertEqual(
            second.data["concept"], "RevenueFromContractWithCustomerExcludingAssessedTax"
        )
        self.assertEqual(second.data["observations"][0]["value"], "222")

    def test_each_metric_carries_the_note_explaining_why_it_exists(self):
        """The note is the reason a reader does not have to guess why this concept was read."""
        self.add_quarter_and_year_to_date()
        self.add_fact(
            self.company,
            self.snapshot,
            concept="RevenueFromContractWithCustomerExcludingAssessedTax",
            value="777",
            period_start=QUARTER_START,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )

        revenue = self.read(metric="revenue")
        contract = self.read(metric="revenue_contract_with_customer")

        self.assertIn("NOT a fallback for the other", revenue.data["metric_note"])
        self.assertIn("never added", contract.data["metric_note"])
        self.assertIn("Apple reports this one", contract.data["metric_note"])


class AvailabilityTests(FinancialFactsTestCase):
    def test_an_observation_accepted_after_the_cutoff_is_excluded_and_reported(self):
        self.add_fact(
            self.company,
            self.snapshot,
            value="96221000000",
            period_start=QUARTER_START,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )

        # The 10-Q was accepted on 2026-08-26, so before this date it was not yet public.
        result = self.read(as_of=date(2026, 8, 1))

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "no_observation_available_before_cutoff")
        self.assertIsNone(result.data)
        self.assertIn("none was accepted before the information cutoff", " ".join(result.warnings))

    def test_one_available_and_one_not_is_partial_with_the_exclusion_listed(self):
        self.add_fact(
            self.company,
            self.snapshot,
            value="111",
            period_start=YEAR_START,
            period_end=QUARTER_END,
            accession=TEN_K,
        )
        self.add_fact(
            self.company,
            self.snapshot,
            value="222",
            period_start=QUARTER_START,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )

        result = self.read(period_end=QUARTER_END, as_of=date(2026, 6, 1))

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(result.reason, "accepted_after_cutoff")
        self.assertEqual(result.data["observations_total"], 1)
        self.assertEqual(result.data["excluded_total"], 1)
        self.assertEqual(result.data["excluded"][0]["reason"], "accepted_after_cutoff")
        self.assertEqual(result.data["excluded"][0]["accession_number"], TEN_Q)

    def test_a_fact_whose_filing_is_not_stored_has_its_availability_reported_as_unknown(self):
        """The outer join is what makes this visible; an inner join would drop the row."""
        self.add_fact(
            self.company,
            self.snapshot,
            value="333",
            period_start=QUARTER_START,
            period_end=QUARTER_END,
            accession="0001045810-26-999999",
        )
        self.add_fact(
            self.company,
            self.snapshot,
            value="444",
            period_start=YEAR_START,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )

        result = self.read(period_end=QUARTER_END)

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        reasons = {row["reason"] for row in result.data["excluded"]}
        self.assertEqual(reasons, {"filing_not_stored"})
        self.assertEqual(result.data["observations_total"], 1)

    def test_a_filing_with_no_acceptance_time_is_not_treated_as_available(self):
        self.add_filing(
            self.company,
            "0001045810-26-000099",
            form_type="10-Q",
            acceptance=None,
            filing_date=date(2026, 8, 26),
        )
        self.add_fact(
            self.company,
            self.snapshot,
            value="555",
            period_start=QUARTER_START,
            period_end=QUARTER_END,
            accession="0001045810-26-000099",
        )
        self.add_fact(
            self.company,
            self.snapshot,
            value="666",
            period_start=YEAR_START,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )

        result = self.read(period_end=QUARTER_END)

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(
            {row["reason"] for row in result.data["excluded"]}, {"acceptance_time_unknown"}
        )

    def test_every_returned_observation_carries_the_acceptance_it_was_judged_on(self):
        self.add_quarter_and_year_to_date()

        result = self.read(period_start=QUARTER_START, period_end=QUARTER_END)

        observation = result.data["observations"][0]
        self.assertEqual(observation["availability"]["form_type"], "10-Q")
        self.assertEqual(
            observation["availability"]["acceptance_datetime"], "2026-08-26T20:36:00Z"
        )
        self.assertEqual(
            observation["source_reference"],
            f"https://www.sec.gov/Archives/edgar/data/1045810/{TEN_Q}.htm",
        )
        self.assertEqual(observation["accession_number"], TEN_Q)


class AbsenceTests(FinancialFactsTestCase):
    def test_a_company_with_no_facts_at_all_is_unavailable(self):
        result = self.read()

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "no_stored_financial_facts")
        self.assertIsNone(result.data)

    def test_a_concept_with_no_stored_observation_is_absent_not_zero(self):
        self.add_quarter_and_year_to_date()

        result = self.read(metric="net_income")

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "concept_not_in_stored_sample")
        self.assertIsNone(result.data)
        joined = " ".join(result.warnings)
        # The distinction that matters: not in our sample is not the same as not reported.
        self.assertIn("not in this stored sample", joined)
        self.assertIn("not a statement that the company never reported it", joined)
        self.assertIn("absent from this sample, not zero", joined)

    def test_a_selection_that_matches_nothing_says_what_is_stored_instead(self):
        self.add_quarter_and_year_to_date()

        result = self.read(period_end=date(2019, 1, 1))

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "no_observation_matches_selection")
        self.assertIn("Stored period ends", " ".join(result.warnings))

    def test_an_instant_metric_has_no_period_start_to_select_on(self):
        """Rejected rather than answered: such a request could only ever return nothing."""
        with self.assertRaises(ValidationError) as caught:
            FinancialFactsRequest(
                symbol="NVDA",
                metric="total_assets",
                as_of=AS_OF,
                period_start=QUARTER_START,
            )

        self.assertIn("no period start", str(caught.exception))

    def test_an_instant_metric_is_selected_by_its_instant_date(self):
        self.add_fact(
            self.company,
            self.snapshot,
            concept="Assets",
            value="320272000000",
            period_start=None,
            period_end=QUARTER_END,
            accession=TEN_Q,
        )

        result = self.read(metric="total_assets", period_end=QUARTER_END)

        self.assertEqual(result.status, ToolStatus.OK)
        observation = result.data["observations"][0]
        self.assertTrue(observation["instant"])
        self.assertIsNone(observation["period_start"])

    def test_a_backwards_period_is_refused(self):
        with self.assertRaises(ValidationError):
            FinancialFactsRequest(
                symbol="NVDA",
                metric="revenue",
                as_of=AS_OF,
                period_start=QUARTER_END,
                period_end=QUARTER_START,
            )


class BoundsAndIsolationTests(FinancialFactsTestCase):
    def test_the_candidate_limit_trims_the_list_without_hiding_the_total(self):
        for index in range(5):
            self.add_fact(
                self.company,
                self.snapshot,
                value=str(1000 + index),
                period_start=date(2026, 1, 1 + index),
                period_end=date(2026, 1, 2 + index),
                accession=TEN_Q,
            )

        result = self.read(candidate_limit=2)

        self.assertEqual(result.data["observations_returned"], 2)
        self.assertEqual(result.data["observations_total"], 5)
        self.assertEqual(result.data["stored_observation_count"], 5)

    def test_another_companys_figures_are_not_visible(self):
        other = self.add_company(ticker="AAPL", cik="0000320193", name="Apple Inc.")
        other_snapshot = self.add_snapshot(other)
        self.add_filing(other, "0000320193-26-000020", form_type="10-Q")
        self.add_fact(
            other,
            other_snapshot,
            concept="Revenues",
            value="999",
            accession="0000320193-26-000020",
        )
        self.add_quarter_and_year_to_date()

        result = self.read(period_start=QUARTER_START, period_end=QUARTER_END)

        self.assertEqual(len(result.data["observations"]), 1)
        self.assertEqual(result.data["observations"][0]["value"], "96221000000")

    def test_an_unknown_symbol_is_its_own_reason(self):
        result = self.read(symbol="ZZZZ")

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "unknown_company")

    def test_selection_criteria_record_what_was_actually_applied(self):
        self.add_quarter_and_year_to_date()

        result = self.read(period_start=QUARTER_START, period_end=QUARTER_END)

        self.assertEqual(
            result.data["selection_criteria"],
            {
                "metric": "revenue",
                "period_start": QUARTER_START.isoformat(),
                "period_end": QUARTER_END.isoformat(),
            },
        )


if __name__ == "__main__":
    unittest.main()
