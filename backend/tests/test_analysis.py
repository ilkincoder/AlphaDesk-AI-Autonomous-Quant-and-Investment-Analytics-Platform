"""The analysis rules, from fixtures. No database, no network.

Every branch of the comparison is reachable here, including both divergence directions,
which the stored NVDA sample cannot demonstrate on its own -- it has sales and no purchases,
so only one of the two is real. The other is covered by labelled fixtures, never by
manufacturing data.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from app.analysis import (
    COMPARISON_REASON_AMENDMENTS,
    COMPARISON_REASON_INCOMPLETE_VALUES,
    COMPARISON_REASON_NO_ELIGIBLE,
    COMPARISON_REASON_PRICE,
    COVERAGE_PARTIAL,
    COVERAGE_UNKNOWN,
    EXCLUDED_ACCEPTANCE_UNKNOWN,
    EXCLUDED_AFTER_CUTOFF,
    EXCLUDED_CODE,
    EXCLUDED_DERIVATIVE,
    EXCLUDED_INCONSISTENT,
    EXCLUDED_OUTSIDE_RANGE,
    EXCLUDED_SECURITY_CLASS,
    INSUFFICIENT_COVERAGE,
    NO_DIRECTIONAL_DIFFERENCE,
    NO_ELIGIBLE_TRANSACTIONS,
    PRICE_DOWN_NET_BUYING,
    PRICE_REASON_TOO_FEW_DATES,
    PRICE_UP_NET_SELLING,
    SAME_DIRECTION,
    UNAVAILABLE,
    VALUE_PRICE_UNUSABLE,
    VALUE_SHARES_UNUSABLE,
    CoverageInput,
    PriceObservation,
    PriceSeriesSelection,
    TransactionRecord,
    analyze,
    information_cutoff,
    unavailable_result,
)

SERIES = PriceSeriesSelection(
    provider="twelve_data", adjustment_basis="adjusted", provider_adjust_mode="splits"
)

# Comfortably before any cutoff these tests use, and comfortably after the transaction
# dates, so it never decides a test by accident.
ACCEPTED = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)
COVERAGE = CoverageInput(2, ACCEPTED, 3, 3, "recent submissions list only")


def obs(trading_date: date, close: str) -> PriceObservation:
    return PriceObservation(trading_date=trading_date, close=Decimal(close))


def rising() -> list[PriceObservation]:
    return [obs(date(2026, 9, 1), "100"), obs(date(2026, 9, 30), "110")]


def falling() -> list[PriceObservation]:
    return [obs(date(2026, 9, 1), "110"), obs(date(2026, 9, 30), "100")]


def flat() -> list[PriceObservation]:
    return [obs(date(2026, 9, 1), "100"), obs(date(2026, 9, 30), "100")]


def txn(**overrides) -> TransactionRecord:
    values = dict(
        accession_number="0000000000-26-000001",
        source_table="nonDerivativeTable",
        row_position=0,
        security_title="Common Stock",
        transaction_date=date(2026, 9, 3),
        transaction_code="S",
        acquired_disposed="D",
        shares=Decimal("100"),
        price_per_share=Decimal("200"),
        is_derivative=False,
        acceptance_datetime=ACCEPTED,
        rule_10b5_1=None,
        is_amendment=False,
        source_document_url="https://www.sec.gov/Archives/example.xml",
    )
    values.update(overrides)
    return TransactionRecord(**values)


def sale(**overrides) -> TransactionRecord:
    return txn(transaction_code="S", acquired_disposed="D", **overrides)


def purchase(**overrides) -> TransactionRecord:
    return txn(transaction_code="P", acquired_disposed="A", **overrides)


def analyse(**overrides):
    values = dict(
        symbol="TEST",
        company_cik="0000000001",
        company_name="TEST CORP",
        requested_start=date(2026, 9, 1),
        requested_end=date(2026, 9, 30),
        observations=rising(),
        transactions=(),
        coverage=COVERAGE,
        price_series=SERIES,
    )
    values.update(overrides)
    return analyze(**values)


def reasons(result) -> dict[str, int]:
    return {exclusion.reason: exclusion.count for exclusion in result.exclusions}


class CutoffTests(unittest.TestCase):
    def test_the_cutoff_is_the_start_of_the_next_local_day_in_utc(self):
        """End of 2026-09-17 in New York, which is EDT, so 04:00 UTC the next day."""
        self.assertEqual(
            information_cutoff(date(2026, 9, 17)),
            datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc),
        )

    def test_the_cutoff_follows_daylight_saving(self):
        """January is EST, so the same local midnight is 05:00 UTC, not 04:00."""
        self.assertEqual(
            information_cutoff(date(2026, 1, 15)),
            datetime(2026, 1, 16, 5, 0, tzinfo=timezone.utc),
        )

    def test_an_evening_filing_on_the_end_date_is_still_included(self):
        """A 21:00 UTC acceptance is 17:00 in New York -- the same day, not the next."""
        result = analyse(
            requested_end=date(2026, 9, 3),
            observations=[obs(date(2026, 9, 1), "100"), obs(date(2026, 9, 3), "110")],
            transactions=(
                sale(acceptance_datetime=datetime(2026, 9, 3, 21, 0, tzinfo=timezone.utc)),
            ),
        )

        self.assertEqual(result.sale_count, 1)


class PriceMetricTests(unittest.TestCase):
    def test_price_change_uses_first_and_last_close_in_range(self):
        result = analyse(observations=rising())

        self.assertEqual(result.price_change_percent, Decimal("10.0"))
        self.assertEqual(result.price_first_date, date(2026, 9, 1))
        self.assertEqual(result.price_last_date, date(2026, 9, 30))
        self.assertEqual(result.price_observation_count, 2)

    def test_only_observations_inside_the_requested_range_are_used(self):
        result = analyse(
            requested_start=date(2026, 9, 10),
            observations=[
                obs(date(2026, 9, 1), "100"),
                obs(date(2026, 9, 10), "120"),
                obs(date(2026, 9, 20), "150"),
                obs(date(2026, 9, 30), "180"),
            ],
        )

        self.assertEqual(result.first_close, Decimal("120"))
        self.assertEqual(result.last_close, Decimal("180"))
        self.assertEqual(result.price_observation_count, 3)
        # The 09-01 bar is outside the range and must not set the opening price.
        self.assertEqual(result.price_first_date, date(2026, 9, 10))

    def test_one_price_date_is_unavailable_with_a_reason(self):
        result = analyse(
            observations=[obs(date(2026, 9, 15), "100")],
            transactions=(sale(),),
        )

        self.assertIsNone(result.price_change_percent)
        self.assertEqual(result.price_unavailable_reason, PRICE_REASON_TOO_FEW_DATES)
        self.assertEqual(result.sample_comparison, UNAVAILABLE)

    def test_no_observations_is_unavailable(self):
        result = analyse(observations=[], transactions=(sale(),))

        self.assertIsNone(result.price_change_percent)
        self.assertEqual(result.sample_comparison, UNAVAILABLE)

    def test_an_ambiguous_price_series_is_reported_not_picked_between(self):
        result = analyse(
            price_series=None,
            price_unavailable_reason="more_than_one_price_series_stored",
        )

        self.assertIsNone(result.price_change_percent)
        self.assertIn("more_than_one", result.price_unavailable_reason)

    def test_precision_is_preserved_and_only_the_display_block_is_rounded(self):
        result = analyse(
            observations=[obs(date(2026, 9, 1), "218.99001"),
                          obs(date(2026, 9, 30), "225.0099945")]
        )
        document = result.as_dict()

        # Exact, to the last digit the provider sent.
        self.assertEqual(
            document["prices"]["change_percent"], str(result.price_change_percent)
        )
        self.assertIn("2.7", document["prices"]["change_percent"])

        # Rounded, and only here.
        self.assertEqual(
            document["display"]["price_change_percent"],
            str(result.price_change_percent.quantize(Decimal("0.01"))),
        )


class DivergenceTests(unittest.TestCase):
    def test_price_up_with_net_selling(self):
        result = analyse(
            observations=rising(),
            transactions=(sale(shares=Decimal("100"), price_per_share=Decimal("200")),),
        )

        self.assertEqual(result.net_reported_value, Decimal("-20000"))
        self.assertEqual(result.sample_comparison, PRICE_UP_NET_SELLING)

    def test_price_down_with_net_buying(self):
        """The other half of the divergence rule, which the stored sample cannot show."""
        result = analyse(
            observations=falling(),
            transactions=(
                purchase(shares=Decimal("100"), price_per_share=Decimal("200")),
            ),
        )

        self.assertEqual(result.net_reported_value, Decimal("20000"))
        self.assertEqual(result.sample_comparison, PRICE_DOWN_NET_BUYING)

    def test_price_up_with_net_buying_agrees(self):
        result = analyse(
            observations=rising(),
            transactions=(
                purchase(shares=Decimal("100"), price_per_share=Decimal("200")),
            ),
        )

        self.assertEqual(result.sample_comparison, SAME_DIRECTION)

    def test_price_down_with_net_selling_agrees(self):
        result = analyse(observations=falling(), transactions=(sale(),))

        self.assertEqual(result.sample_comparison, SAME_DIRECTION)

    def test_both_directions_are_read_from_unrounded_values(self):
        """A change that rounds to zero is still a change, and must not read as flat."""
        result = analyse(
            observations=[obs(date(2026, 9, 1), "100"), obs(date(2026, 9, 30), "100.0001")],
            transactions=(sale(),),
        )

        self.assertNotEqual(result.price_change_percent, 0)
        self.assertEqual(result.sample_comparison, PRICE_UP_NET_SELLING)
        self.assertEqual(result.as_dict()["display"]["price_change_percent"], "0.00")


class NoDirectionalDifferenceTests(unittest.TestCase):
    def test_an_unchanged_price_with_sales(self):
        result = analyse(observations=flat(), transactions=(sale(),))

        self.assertEqual(result.sample_comparison, NO_DIRECTIONAL_DIFFERENCE)

    def test_balanced_purchases_and_sales(self):
        result = analyse(
            observations=rising(),
            transactions=(
                purchase(shares=Decimal("100"), price_per_share=Decimal("200")),
                sale(shares=Decimal("100"), price_per_share=Decimal("200"), row_position=1),
            ),
        )

        self.assertEqual(result.net_reported_value, Decimal("0"))
        self.assertEqual(result.sample_comparison, NO_DIRECTIONAL_DIFFERENCE)


class NoEligibleTransactionTests(unittest.TestCase):
    def test_a_filing_with_only_a_grant_has_nothing_to_compare(self):
        result = analyse(
            transactions=(
                txn(transaction_code="A", acquired_disposed="A", price_per_share=Decimal("0")),
            )
        )

        self.assertEqual(result.purchase_count, 0)
        self.assertEqual(result.sale_count, 0)
        self.assertEqual(result.sample_comparison, NO_ELIGIBLE_TRANSACTIONS)
        self.assertEqual(result.sample_comparison_reason, COMPARISON_REASON_NO_ELIGIBLE)

    def test_no_transactions_at_all_is_not_an_error(self):
        result = analyse(transactions=())

        self.assertEqual(result.sample_comparison, NO_ELIGIBLE_TRANSACTIONS)
        self.assertEqual(result.included, ())


class ValueTests(unittest.TestCase):
    def test_a_reported_value_is_shares_times_price(self):
        result = analyse(
            transactions=(sale(shares=Decimal("198707"), price_per_share=Decimal("227.6954")),)
        )

        self.assertEqual(result.sale_value_known, Decimal("45244669.8478"))

    def test_a_missing_price_withholds_the_value_and_the_net(self):
        result = analyse(
            observations=falling(),
            transactions=(
                purchase(shares=Decimal("100"), price_per_share=Decimal("200")),
                sale(price_per_share=None, row_position=1),
            ),
        )

        self.assertFalse(result.values_complete)
        self.assertIsNone(result.net_reported_value)
        self.assertEqual(result.sale_rows_without_value, 1)
        self.assertEqual(result.sale_value_known, Decimal("0"))
        self.assertEqual(result.purchase_value_known, Decimal("20000"))
        self.assertEqual(result.sample_comparison, UNAVAILABLE)
        self.assertEqual(
            result.sample_comparison_reason, COMPARISON_REASON_INCOMPLETE_VALUES
        )

    def test_a_zero_price_withholds_the_value_rather_than_counting_it_as_zero(self):
        """A zero price is usually an award for no consideration, not a free purchase."""
        result = analyse(
            transactions=(purchase(price_per_share=Decimal("0"), shares=Decimal("5000")),)
        )
        row = result.included[0]

        self.assertIsNone(row.reported_value)
        self.assertEqual(row.value_unavailable_reason, VALUE_PRICE_UNUSABLE)
        self.assertEqual(result.purchase_rows_without_value, 1)
        self.assertIsNone(result.net_reported_value)

    def test_a_nonpositive_share_count_withholds_the_value(self):
        result = analyse(transactions=(sale(shares=Decimal("0")),))
        row = result.included[0]

        self.assertIsNone(row.reported_value)
        self.assertEqual(row.value_unavailable_reason, VALUE_SHARES_UNUSABLE)

    def test_the_underlying_record_is_preserved_when_a_value_is_withheld(self):
        result = analyse(transactions=(sale(price_per_share=None),))
        row = result.included[0]

        self.assertEqual(row.shares, Decimal("100"))
        self.assertIsNone(row.price_per_share)
        self.assertEqual(row.accession_number, "0000000000-26-000001")

    def test_an_empty_purchase_side_still_produces_a_net(self):
        """Zero purchases is a complete answer, not a missing one."""
        result = analyse(transactions=(sale(shares=Decimal("10"), price_per_share=Decimal("5")),))

        self.assertTrue(result.values_complete)
        self.assertEqual(result.net_reported_value, Decimal("-50"))


class ExclusionTests(unittest.TestCase):
    def test_a_grant_and_a_gift_are_excluded_by_code(self):
        result = analyse(
            transactions=(
                txn(transaction_code="A", acquired_disposed="A"),
                txn(transaction_code="G", acquired_disposed="D", row_position=1),
            )
        )

        self.assertEqual(reasons(result), {EXCLUDED_CODE: 2})

    def test_option_exercises_and_tax_withholding_are_excluded_by_code(self):
        result = analyse(
            transactions=(
                txn(transaction_code="M", acquired_disposed="A"),
                txn(transaction_code="F", acquired_disposed="D", row_position=1),
            )
        )

        self.assertEqual(reasons(result), {EXCLUDED_CODE: 2})

    def test_a_derivative_row_is_excluded(self):
        result = analyse(
            transactions=(
                txn(
                    source_table="derivativeTable",
                    is_derivative=True,
                    security_title="Employee Stock Option (Right to Buy)",
                ),
            )
        )

        self.assertEqual(reasons(result), {EXCLUDED_DERIVATIVE: 1})

    def test_an_unsupported_security_class_is_excluded_and_named(self):
        result = analyse(transactions=(sale(security_title="Series A Preferred Stock"),))

        self.assertEqual(reasons(result), {EXCLUDED_SECURITY_CLASS: 1})
        self.assertEqual(
            result.exclusions[0].identities, ("0000000000-26-000001:nonDerivativeTable[0]",)
        )

    def test_the_common_title_variant_is_accepted(self):
        """One stored NVDA filing says "Common" where the others say "Common Stock"."""
        result = analyse(transactions=(sale(security_title="Common"),))

        self.assertEqual(result.sale_count, 1)
        self.assertEqual(result.included[0].security_title, "Common")

    def test_title_matching_ignores_case_and_extra_whitespace(self):
        result = analyse(transactions=(sale(security_title="  common   STOCK "),))

        self.assertEqual(result.sale_count, 1)

    def test_a_purchase_marked_disposed_is_a_data_quality_issue_not_a_sale(self):
        result = analyse(transactions=(txn(transaction_code="P", acquired_disposed="D"),))

        self.assertEqual(reasons(result), {EXCLUDED_INCONSISTENT: 1})
        self.assertEqual(result.purchase_count, 0)

    def test_a_sale_marked_acquired_is_a_data_quality_issue_not_a_purchase(self):
        result = analyse(transactions=(txn(transaction_code="S", acquired_disposed="A"),))

        self.assertEqual(reasons(result), {EXCLUDED_INCONSISTENT: 1})
        self.assertEqual(result.sale_count, 0)

    def test_a_transaction_outside_the_price_range_is_excluded(self):
        result = analyse(
            transactions=(sale(transaction_date=date(2026, 10, 15)),)
        )

        self.assertEqual(reasons(result), {EXCLUDED_OUTSIDE_RANGE: 1})

    def test_a_filing_accepted_after_the_cutoff_is_excluded(self):
        """A trade dated inside the period is still not knowable if it was filed later."""
        result = analyse(
            transactions=(
                sale(acceptance_datetime=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)),
            )
        )

        self.assertEqual(reasons(result), {EXCLUDED_AFTER_CUTOFF: 1})

    def test_a_missing_acceptance_timestamp_is_excluded_and_counted(self):
        result = analyse(transactions=(sale(acceptance_datetime=None),))

        self.assertEqual(reasons(result), {EXCLUDED_ACCEPTANCE_UNKNOWN: 1})

    def test_exclusions_keep_the_identity_so_a_row_can_be_found(self):
        result = analyse(
            transactions=(
                txn(
                    accession_number="0000000000-26-000042",
                    row_position=3,
                    transaction_code="A",
                    acquired_disposed="A",
                ),
            )
        )

        self.assertEqual(
            result.exclusions[0].identities,
            ("0000000000-26-000042:nonDerivativeTable[3]",),
        )

    def test_each_reason_is_counted_separately(self):
        result = analyse(
            transactions=(
                txn(transaction_code="A", acquired_disposed="A"),
                sale(security_title="Preferred Stock", row_position=1),
                sale(acceptance_datetime=None, row_position=2),
                sale(row_position=3),
            )
        )

        self.assertEqual(
            reasons(result),
            {EXCLUDED_CODE: 1, EXCLUDED_SECURITY_CLASS: 1, EXCLUDED_ACCEPTANCE_UNKNOWN: 1},
        )
        self.assertEqual(result.sale_count, 1)


class AmendmentTests(unittest.TestCase):
    def test_an_amendment_accepted_before_the_cutoff_withholds_the_comparison(self):
        result = analyse(
            observations=rising(),
            transactions=(
                sale(),
                txn(is_amendment=True, accession_number="0000000000-26-000002"),
            ),
        )

        self.assertTrue(result.amendment_uncertainty)
        self.assertEqual(result.amendment_accessions, ("0000000000-26-000002",))
        self.assertEqual(result.sample_comparison, UNAVAILABLE)
        self.assertEqual(result.sample_comparison_reason, COMPARISON_REASON_AMENDMENTS)

    def test_an_amendment_accepted_after_the_cutoff_does_not_affect_an_earlier_period(self):
        result = analyse(
            observations=rising(),
            transactions=(
                sale(),
                txn(
                    is_amendment=True,
                    accession_number="0000000000-26-000002",
                    acceptance_datetime=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
                ),
            ),
        )

        self.assertFalse(result.amendment_uncertainty)
        self.assertEqual(result.sample_comparison, PRICE_UP_NET_SELLING)

    def test_the_block_preserves_the_price_metrics_and_the_raw_detail(self):
        result = analyse(
            observations=rising(),
            transactions=(
                sale(),
                # A grant inside the amendment, so the only eligible row is the sale above
                # and the counts below say something unambiguous.
                txn(
                    is_amendment=True,
                    accession_number="0000000000-26-000002",
                    transaction_code="A",
                    acquired_disposed="A",
                ),
            ),
        )

        self.assertEqual(result.price_change_percent, Decimal("10.0"))
        self.assertEqual(result.sale_count, 1)
        self.assertEqual(reasons(result), {EXCLUDED_CODE: 1})
        self.assertTrue(
            any("amendment" in note.lower() for note in result.limitations),
            result.limitations,
        )

    def test_no_amendment_means_no_uncertainty(self):
        result = analyse(observations=rising(), transactions=(sale(),))

        self.assertFalse(result.amendment_uncertainty)
        self.assertEqual(result.amendment_accessions, ())


class CoverageTests(unittest.TestCase):
    def test_partial_coverage_withholds_the_overall_conclusion(self):
        result = analyse(
            observations=rising(), transactions=(sale(),), coverage=COVERAGE
        )

        self.assertEqual(result.coverage.status, COVERAGE_PARTIAL)
        # The sample comparison is still calculable...
        self.assertEqual(result.sample_comparison, PRICE_UP_NET_SELLING)
        # ...and the overall conclusion still refuses to draw one.
        self.assertEqual(result.overall_conclusion, INSUFFICIENT_COVERAGE)

    def test_no_ingestion_records_means_unknown_coverage_not_complete(self):
        result = analyse(
            observations=rising(),
            transactions=(sale(),),
            coverage=CoverageInput(0, None, None, None, None),
        )

        self.assertEqual(result.coverage.status, COVERAGE_UNKNOWN)
        self.assertEqual(result.overall_conclusion, INSUFFICIENT_COVERAGE)

    def test_the_discovery_scope_is_carried_through(self):
        result = analyse(observations=rising(), transactions=(sale(),))
        document = result.as_dict()

        self.assertEqual(
            document["coverage"]["discovery_scope"], "recent submissions list only"
        )
        self.assertEqual(document["coverage"]["ingestion_run_count"], 2)

    def test_an_empty_filing_sample_never_reads_as_complete_coverage(self):
        result = analyse(observations=rising(), transactions=())

        self.assertEqual(result.sample_comparison, NO_ELIGIBLE_TRANSACTIONS)
        self.assertEqual(result.overall_conclusion, INSUFFICIENT_COVERAGE)
        self.assertTrue(
            any("not a complete history" in note for note in result.limitations)
        )


class OwnerIndependenceTests(unittest.TestCase):
    def test_transactions_carry_no_owner_and_are_counted_once(self):
        """The result has no owner field at all, so no join can multiply a total."""
        result = analyse(
            observations=rising(),
            transactions=(sale(shares=Decimal("100"), price_per_share=Decimal("200")),),
        )
        row = result.included[0]

        self.assertFalse(hasattr(row, "owner_name"))
        self.assertFalse(hasattr(row, "owners"))
        self.assertEqual(result.sale_count, 1)
        self.assertEqual(result.sale_value_known, Decimal("20000"))

    def test_two_rows_with_identical_values_stay_two_rows(self):
        result = analyse(
            observations=rising(),
            transactions=(
                sale(shares=Decimal("100"), price_per_share=Decimal("200"), row_position=0),
                sale(shares=Decimal("100"), price_per_share=Decimal("200"), row_position=1),
            ),
        )

        self.assertEqual(result.sale_count, 2)
        self.assertEqual(result.sale_value_known, Decimal("40000"))
        self.assertEqual(
            [row.row_position for row in result.included], [0, 1]
        )


class Rule10b5OneTests(unittest.TestCase):
    def test_the_filing_indicator_is_carried_onto_the_row_with_unknown_preserved(self):
        result = analyse(
            observations=rising(),
            transactions=(
                sale(rule_10b5_1=True, row_position=0),
                sale(rule_10b5_1=None, row_position=1),
                sale(rule_10b5_1=False, row_position=2),
            ),
        )

        self.assertEqual(
            [row.rule_10b5_1 for row in result.included], [True, None, False]
        )

    def test_the_indicator_does_not_change_the_comparison(self):
        with_plan = analyse(observations=rising(), transactions=(sale(rule_10b5_1=True),))
        without = analyse(observations=rising(), transactions=(sale(rule_10b5_1=False),))

        self.assertEqual(with_plan.sample_comparison, without.sample_comparison)
        self.assertEqual(with_plan.net_reported_value, without.net_reported_value)


class UnavailableResultTests(unittest.TestCase):
    def test_a_missing_company_produces_a_typed_result_not_an_exception(self):
        result = unavailable_result(
            symbol="AAPL",
            reason="no_company_stored_under_that_symbol",
            requested_start=date(2026, 9, 1),
            requested_end=date(2026, 9, 30),
            note="Stored tickers: NVDA",
        )
        document = result.as_dict()

        self.assertEqual(document["unavailable_reason"], "no_company_stored_under_that_symbol")
        self.assertEqual(document["sample_comparison"], UNAVAILABLE)
        self.assertIsNone(document["prices"]["change_percent"])
        self.assertEqual(document["transactions"]["sale_count"], 0)
        self.assertIn("Stored tickers: NVDA", " ".join(document["limitations"]))

    def test_an_unavailable_result_still_explains_itself(self):
        result = unavailable_result(
            symbol="AAPL",
            reason="no_company_stored_under_that_symbol",
            requested_start=date(2026, 9, 1),
            requested_end=date(2026, 9, 30),
        )

        self.assertTrue(result.methodology)
        self.assertTrue(result.limitations)
        self.assertEqual(result.overall_conclusion, INSUFFICIENT_COVERAGE)


if __name__ == "__main__":
    unittest.main()