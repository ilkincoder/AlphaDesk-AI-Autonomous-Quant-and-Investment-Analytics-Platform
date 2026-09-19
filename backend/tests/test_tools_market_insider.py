"""Capability A: the price and insider analysis, behind a typed contract.

The arithmetic and the classification rules are `app.analysis`'s and are tested there. What is
tested here is what the *tool* promises on top: that the window is the caller's and is never
widened, that a withheld metric is reported rather than hidden, that the amendment and
coverage warnings survive serialization, that the listing cap does not change the totals, and
that each "we hold nothing" case is its own answer.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone
from unittest import mock

from pydantic import ValidationError

from app.tools import market_insider
from app.tools.market_insider import MarketInsiderRequest
from app.tools.results import ToolStatus
from tests.test_tools_contracts import ACCEPTED, ToolTestCase

START = date(2026, 9, 1)
END = date(2026, 9, 30)


class MarketInsiderTestCase(ToolTestCase):
    def analyse(self, **overrides):
        arguments = {"symbol": "NVDA", "start_date": START, "end_date": END}
        arguments.update(overrides)
        return market_insider.run(
            self.session, MarketInsiderRequest(**arguments)
        )

    def a_priced_company(self, *, days: int = 10) -> "object":
        company = self.add_company()
        self.add_price_range(company, START, days, first="100", step="2")
        return company


class WindowTests(MarketInsiderTestCase):
    def test_both_dates_are_required(self):
        for missing in ("start_date", "end_date"):
            with self.subTest(missing=missing):
                arguments = {"symbol": "NVDA", "start_date": START, "end_date": END}
                del arguments[missing]
                with self.assertRaises(ValidationError):
                    MarketInsiderRequest(**arguments)

    def test_a_window_that_runs_backwards_is_refused(self):
        with self.assertRaises(ValidationError):
            self.analyse(start_date=END, end_date=START)

    def test_the_requested_window_is_what_was_asked_for_not_what_is_stored(self):
        """The stored range never widens the answer: it is reported beside it."""
        self.a_priced_company()

        result = self.analyse()

        self.assertEqual(result.data["period"]["requested_start"], START.isoformat())
        self.assertEqual(result.data["period"]["requested_end"], END.isoformat())
        self.assertEqual(result.data["prices"]["first_date"], START.isoformat())
        self.assertEqual(result.data["stored_price_availability"]["first_date"], "2026-09-01")

    def test_a_window_outside_the_stored_prices_withholds_the_return(self):
        """Nothing is fetched and nothing is widened to manufacture a comparison."""
        self.a_priced_company()

        result = self.analyse(start_date=date(2020, 1, 1), end_date=date(2020, 12, 31))

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(result.reason, "price_metrics_unavailable")
        self.assertIsNone(result.data["prices"]["change_percent"])
        self.assertEqual(
            result.data["prices"]["unavailable_reason"], "fewer_than_two_price_dates_in_range"
        )
        # And the payload says what is actually held, so the gap is explainable.
        self.assertEqual(result.data["stored_price_availability"]["first_date"], "2026-09-01")

    def test_the_symbol_is_normalised_rather_than_guessed_at(self):
        self.a_priced_company()

        result = self.analyse(symbol="  nvda ")

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.symbol, "NVDA")


class UnavailableTests(MarketInsiderTestCase):
    def test_an_unknown_symbol_is_its_own_reason(self):
        result = self.analyse(symbol="ZZZZ")

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "unknown_company")
        self.assertIsNone(result.data)
        self.assertIn("Stored tickers", " ".join(result.warnings))

    def test_a_company_with_no_prices_is_unavailable_not_empty(self):
        self.add_company()

        result = self.analyse()

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "no_price_series_stored")
        self.assertIsNone(result.data)
        self.assertIn("No daily prices are stored", " ".join(result.warnings))

    def test_two_price_series_are_refused_rather_than_chosen_between(self):
        """Raw and adjusted closes are different numbers for the same day."""
        company = self.add_company()
        self.add_price_range(company, START, 5)
        self.add_price(company, START, "99", basis="raw", mode="none")

        result = self.analyse()

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "more_than_one_price_series_stored")
        self.assertIn("Refusing to pick one", " ".join(result.warnings))


class StatusTests(MarketInsiderTestCase):
    def test_a_complete_analysis_is_ok_even_though_coverage_is_insufficient(self):
        """The call succeeded; the sample is thin. The two must not be collapsed.

        `insufficient_coverage` is this project's standing conclusion about a bounded sample,
        so treating it as a failure would make every honest answer look like an error.
        """
        self.a_priced_company()

        result = self.analyse()

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertIsNone(result.reason)
        self.assertEqual(result.data["overall_conclusion"], "insufficient_coverage")
        # No eligible transactions is a complete answer: we looked, and there were none.
        self.assertEqual(result.data["sample_comparison"], "no_eligible_transactions")

    def test_an_amendment_withholds_the_comparison_and_is_reported_as_partial(self):
        """The existing blocking rule is preserved, and the warning survives serialization."""
        company = self.a_priced_company()
        filing = self.add_filing(company, "0001199039-26-000014")
        self.add_transaction(filing)
        # A 4/A with no transaction rows of its own would never be seen: the amendment list is
        # built from the transactions that were read, not from the filings that exist.
        amendment = self.add_filing(company, "0001199039-26-000015", is_amendment=True)
        self.add_transaction(amendment, row_position=0)

        result = self.analyse()

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(result.reason, "amendment_uncertainty")
        self.assertTrue(result.data["amendments"]["uncertainty"])
        self.assertEqual(result.data["sample_comparison"], "unavailable")
        # The blocking rule survives JSON, which is where it would otherwise be lost.
        self.assertIn(
            "An amendment was accepted before the cutoff",
            " ".join(result.warnings),
        )

    def test_unusable_values_withhold_the_net_total(self):
        company = self.a_priced_company()
        filing = self.add_filing(company)
        self.add_transaction(filing, row_position=0, price=None)

        result = self.analyse()

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(result.reason, "some_eligible_values_unavailable")
        self.assertFalse(result.data["transactions"]["values_complete"])
        self.assertIsNone(result.data["transactions"]["net_reported_value"])


class CutoffTests(MarketInsiderTestCase):
    def test_a_filing_accepted_after_the_window_end_is_excluded(self):
        """The cutoff is the end of the requested day in the market's timezone."""
        company = self.a_priced_company()
        inside = self.add_filing(
            company, "0001199039-26-000014", acceptance=datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
        )
        self.add_transaction(inside, row_position=0)
        after = self.add_filing(
            company, "0001199039-26-000015", acceptance=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        )
        self.add_transaction(after, row_position=0)

        result = self.analyse()

        self.assertEqual(result.data["transactions"]["sale_count"], 1)
        reasons = {row["reason"]: row["count"] for row in result.data["exclusions"]}
        self.assertEqual(reasons.get("filed_after_cutoff"), 1)

    def test_a_filing_with_no_acceptance_time_cannot_be_shown_to_be_knowable(self):
        company = self.a_priced_company()
        filing = self.add_filing(company, acceptance=None)
        self.add_transaction(filing)

        result = self.analyse()

        self.assertEqual(result.data["transactions"]["sale_count"], 0)
        reasons = {row["reason"]: row["count"] for row in result.data["exclusions"]}
        self.assertEqual(reasons.get("acceptance_time_unknown"), 1)


class OwnerJoinTests(MarketInsiderTestCase):
    def test_two_reporting_owners_do_not_multiply_a_transaction(self):
        """Owners attach to the filing, so joining them would double every total."""
        from app.models import InsiderReportingOwner

        company = self.a_priced_company()
        filing = self.add_filing(company)
        self.add_transaction(filing, shares="100", price="200")
        for index in range(2):
            self.session.add(
                InsiderReportingOwner(
                    filing_id=filing.id,
                    reporting_owner_cik=f"000000000{index}",
                    owner_name=f"Owner {index}",
                )
            )
        self.session.flush()

        result = self.analyse()

        self.assertEqual(result.data["transactions"]["sale_count"], 1)
        self.assertEqual(result.data["transactions"]["sale_value_known"], "20000")


class BoundedListingTests(MarketInsiderTestCase):
    def test_a_trimmed_listing_leaves_the_totals_complete_and_says_so(self):
        company = self.a_priced_company()
        filing = self.add_filing(company)
        for position in range(5):
            self.add_transaction(filing, row_position=position, shares="10", price="100")

        with mock.patch.object(market_insider, "MAX_INCLUDED_ROWS", 2):
            result = self.analyse()

        listing = result.data["included_transactions"]
        self.assertEqual(listing["returned"], 2)
        self.assertEqual(listing["total"], 5)
        self.assertEqual(listing["omitted"], 3)
        # The counts and the money are the calculation's, and cover all five rows.
        self.assertEqual(result.data["transactions"]["sale_count"], 5)
        self.assertEqual(result.data["transactions"]["sale_value_known"], "5000")
        self.assertIn("not listed individually", listing["note"])
        self.assertIn("not listed individually", " ".join(result.warnings))

    def test_an_untrimmed_listing_says_nothing_about_truncation(self):
        company = self.a_priced_company()
        filing = self.add_filing(company)
        self.add_transaction(filing)

        result = self.analyse()

        listing = result.data["included_transactions"]
        self.assertEqual(listing["omitted"], 0)
        self.assertIsNone(listing["note"])
        self.assertNotIn("not listed individually", " ".join(result.warnings))


class ProvenanceTests(MarketInsiderTestCase):
    def test_the_series_and_its_adjustment_mode_are_reported(self):
        self.a_priced_company()

        result = self.analyse()

        self.assertEqual(
            result.data["prices"]["series"],
            {
                "provider": "twelve_data",
                "adjustment_basis": "adjusted",
                "provider_adjust_mode": "splits",
            },
        )
        self.assertEqual(result.data["prices"]["observation_count"], 10)

    def test_coverage_from_the_ingestion_receipt_reaches_the_payload(self):
        from decimal import Decimal

        from app.models import IngestionRun

        company = self.a_priced_company()
        self.session.add(
            IngestionRun(
                company_id=company.id,
                scope="form4",
                started_at=ACCEPTED,
                completed_at=ACCEPTED,
                parameters={"filings": 3},
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

        result = self.analyse()

        coverage = result.data["coverage"]
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["latest_requested_filings"], 3)
        self.assertIn("bounded sample", " ".join(result.warnings))

    def test_the_methodology_travels_with_the_result(self):
        self.a_priced_company()

        result = self.analyse()

        self.assertTrue(result.data["methodology"])
        self.assertIn("information cutoff", " ".join(result.data["methodology"]))


class IsolationTests(MarketInsiderTestCase):
    def test_another_companys_prices_and_transactions_are_not_used(self):
        nvda = self.a_priced_company()
        aapl = self.add_company(ticker="AAPL", cik="0000320193", name="Apple Inc.")
        self.add_price_range(aapl, START, 5, first="500", step="0")
        filing = self.add_filing(aapl, "0000320193-26-000020")
        self.add_transaction(filing, shares="1", price="1")

        result = self.analyse(symbol="NVDA")

        self.assertEqual(result.data["company"]["cik"], nvda.sec_issuer_cik)
        self.assertEqual(result.data["prices"]["first_close"], "100")
        self.assertEqual(result.data["transactions"]["sale_count"], 0)


if __name__ == "__main__":
    unittest.main()
