"""Company Facts parsing. Pure: a JSON payload in, typed observations out.

The fixtures reproduce NVDA's real shape, including the case the whole natural key exists
for: a quarter and a year-to-date figure sharing an end date *and* a fiscal period, which
only `period_start` tells apart.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import json
import unittest
from datetime import date
from decimal import Decimal

from app.company_facts import (
    CONCEPTS,
    MalformedCompanyFactsError,
    parse_company_facts,
)

ACCESSION_10Q = "0001045810-26-000075"
ACCESSION_10K = "0001045810-26-000021"


def payload(facts: dict, *, entity: str = "NVIDIA CORP") -> bytes:
    return json.dumps(
        {"cik": 1045810, "entityName": entity, "facts": {"us-gaap": facts}},
        # The real endpoint sends numbers, and the parser must see them as JSON numbers.
        default=str,
    ).encode()


def revenue_entries(entries: list[dict]) -> dict:
    return {"Revenues": {"label": "Revenues", "units": {"USD": entries}}}


class PrecisionTests(unittest.TestCase):
    def test_a_large_integer_is_kept_exactly(self):
        document = parse_company_facts(
            payload(revenue_entries([
                {"start": "2026-04-27", "end": "2026-07-26", "val": 96221000000,
                 "accn": ACCESSION_10Q, "form": "10-Q"},
            ])),
            accessions={ACCESSION_10Q},
        )

        self.assertEqual(document.observations[0].value, Decimal("96221000000"))

    def test_a_decimal_is_not_routed_through_a_float(self):
        """`parse_float=Decimal` is the only reason this survives intact."""
        raw = (
            b'{"entityName": "X", "facts": {"us-gaap": {"Revenues": {"units": {"USD": ['
            b'{"start": "2026-01-01", "end": "2026-02-01", "val": 0.1234567890123456789,'
            b' "accn": "' + ACCESSION_10Q.encode() + b'", "form": "10-Q"}]}}}}}'
        )
        document = parse_company_facts(raw, accessions={ACCESSION_10Q})

        self.assertEqual(
            document.observations[0].value, Decimal("0.1234567890123456789")
        )

    def test_a_negative_net_income_is_accepted(self):
        """A loss-making period reports a negative value, and nothing may reject it."""
        document = parse_company_facts(
            payload({"NetIncomeLoss": {"units": {"USD": [
                {"start": "2026-01-01", "end": "2026-03-31", "val": -1234567,
                 "accn": ACCESSION_10Q, "form": "10-Q"},
            ]}}}),
            accessions={ACCESSION_10Q},
        )

        self.assertEqual(document.observations[0].value, Decimal("-1234567"))

    def test_a_non_numeric_value_is_refused(self):
        with self.assertRaises(MalformedCompanyFactsError):
            parse_company_facts(
                payload(revenue_entries([
                    {"start": "2026-01-01", "end": "2026-02-01", "val": "not a number",
                     "accn": ACCESSION_10Q},
                ])),
                accessions={ACCESSION_10Q},
            )

    def test_a_boolean_is_not_a_number(self):
        """`True` is an `int` in Python, and must not slip through as 1."""
        with self.assertRaises(MalformedCompanyFactsError):
            parse_company_facts(
                payload(revenue_entries([
                    {"start": "2026-01-01", "end": "2026-02-01", "val": True,
                     "accn": ACCESSION_10Q},
                ])),
                accessions={ACCESSION_10Q},
            )


class PeriodTests(unittest.TestCase):
    # The real pair: same end date, same fiscal period, different durations.
    QUARTER_AND_YEAR_TO_DATE = {
        "Revenues": {
            "label": "Revenues",
            "units": {
                "USD": [
                    {
                        "start": "2026-04-27",
                        "end": "2026-07-26",
                        "val": 96221000000,
                        "fy": 2027,
                        "fp": "Q2",
                        "form": "10-Q",
                        "accn": ACCESSION_10Q,
                        "frame": "CY2026Q2",
                    },
                    {
                        "start": "2026-01-26",
                        "end": "2026-07-26",
                        "val": 177837000000,
                        "fy": 2027,
                        "fp": "Q2",
                        "form": "10-Q",
                        "accn": ACCESSION_10Q,
                    },
                ]
            },
        }
    }

    def test_a_quarter_and_a_year_to_date_fact_stay_separate(self):
        document = parse_company_facts(
            payload(self.QUARTER_AND_YEAR_TO_DATE), accessions={ACCESSION_10Q}
        )

        self.assertEqual(len(document.observations), 2)
        by_start = {o.period_start: o.value for o in document.observations}
        self.assertEqual(by_start[date(2026, 4, 27)], Decimal("96221000000"))
        self.assertEqual(by_start[date(2026, 1, 26)], Decimal("177837000000"))

    def test_the_fiscal_period_alone_does_not_distinguish_them(self):
        """Which is exactly why `fp` is stored as metadata and not used as identity."""
        document = parse_company_facts(
            payload(self.QUARTER_AND_YEAR_TO_DATE), accessions={ACCESSION_10Q}
        )

        self.assertEqual({o.fiscal_period for o in document.observations}, {"Q2"})

    def test_an_instant_fact_has_no_period_start(self):
        document = parse_company_facts(
            payload({"Assets": {"units": {"USD": [
                {"end": "2026-07-26", "val": 320272000000, "accn": ACCESSION_10Q,
                 "form": "10-Q", "frame": "CY2026Q2I"},
            ]}}}),
            accessions={ACCESSION_10Q},
        )
        observation = document.observations[0]

        self.assertIsNone(observation.period_start)
        self.assertEqual(observation.period_end, date(2026, 7, 26))

    def test_an_instant_concept_given_a_period_start_is_refused(self):
        """A balance sheet figure that claims a duration is not the concept it claims."""
        with self.assertRaises(MalformedCompanyFactsError) as caught:
            parse_company_facts(
                payload({"Assets": {"units": {"USD": [
                    {"start": "2026-01-01", "end": "2026-07-26", "val": 1,
                     "accn": ACCESSION_10Q},
                ]}}}),
                accessions={ACCESSION_10Q},
            )

        self.assertIn("instant", str(caught.exception))

    def test_a_missing_end_date_is_refused(self):
        with self.assertRaises(MalformedCompanyFactsError):
            parse_company_facts(
                payload(revenue_entries([{"start": "2026-01-01", "val": 1,
                                          "accn": ACCESSION_10Q}])),
                accessions={ACCESSION_10Q},
            )

    def test_the_same_period_under_two_accessions_stays_two_facts(self):
        """A revised comparative is a second document's statement of the same period."""
        document = parse_company_facts(
            payload(revenue_entries([
                {"start": "2020-01-27", "end": "2021-01-31", "val": 16675000000,
                 "form": "10-K", "accn": "0001045810-21-000010"},
                {"start": "2020-01-27", "end": "2021-01-31", "val": 16675000000,
                 "form": "10-K", "accn": "0001045810-22-000036"},
            ])),
            accessions={"0001045810-21-000010", "0001045810-22-000036"},
        )

        self.assertEqual(len(document.observations), 2)
        self.assertEqual(
            {o.accession_number for o in document.observations},
            {"0001045810-21-000010", "0001045810-22-000036"},
        )


class ConceptSelectionTests(unittest.TestCase):
    def test_only_the_selected_accessions_are_kept(self):
        document = parse_company_facts(
            payload(revenue_entries([
                {"start": "2026-04-27", "end": "2026-07-26", "val": 1, "accn": ACCESSION_10Q},
                {"start": "2025-04-28", "end": "2025-07-27", "val": 2, "accn": ACCESSION_10K},
            ])),
            accessions={ACCESSION_10Q},
        )

        self.assertEqual(len(document.observations), 1)
        self.assertEqual(document.observations[0].accession_number, ACCESSION_10Q)

    def test_a_concept_with_nothing_in_the_selected_filings_is_unmatched(self):
        """NVDA's case: the older revenue tag exists but the recent filings do not use it."""
        document = parse_company_facts(
            payload(revenue_entries([
                {"start": "2026-04-27", "end": "2026-07-26", "val": 1, "accn": ACCESSION_10Q},
            ])),
            accessions={ACCESSION_10Q},
        )

        self.assertIn("revenue", document.concepts_matched)
        self.assertIn("revenue_contract_with_customer", document.concepts_unmatched)
        self.assertIn("net_income", document.concepts_unmatched)
        self.assertTrue(
            any("unmatched" in note for note in document.limitations),
            document.limitations,
        )

    def test_both_revenue_concepts_are_read_as_separate_concepts(self):
        document = parse_company_facts(
            payload({
                "Revenues": {"units": {"USD": [
                    {"start": "2026-01-01", "end": "2026-03-31", "val": 1,
                     "accn": ACCESSION_10Q},
                ]}},
                "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                    {"start": "2026-01-01", "end": "2026-03-31", "val": 2,
                     "accn": ACCESSION_10Q},
                ]}},
            }),
            accessions={ACCESSION_10Q},
        )

        self.assertEqual(
            {o.concept for o in document.observations},
            {"Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"},
        )
        self.assertEqual(
            len(document.observations), 2, "two concepts are not one series"
        )

    def test_the_concept_table_documents_every_choice(self):
        for mapping in CONCEPTS:
            with self.subTest(concept=mapping.concept):
                self.assertIn(mapping.kind, ("duration", "instant"))
                self.assertTrue(mapping.note, "every concept says why it is kept")

    def test_both_revenue_mappings_exist_and_are_distinct(self):
        names = {mapping.name: mapping.concept for mapping in CONCEPTS}

        self.assertEqual(names["revenue"], "Revenues")
        self.assertEqual(
            names["revenue_contract_with_customer"],
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        )


class MalformedPayloadTests(unittest.TestCase):
    def test_a_payload_that_is_not_json_is_refused(self):
        with self.assertRaises(MalformedCompanyFactsError):
            parse_company_facts(b"<html>not json</html>", accessions=set())

    def test_a_payload_with_no_facts_object_is_refused(self):
        with self.assertRaises(MalformedCompanyFactsError):
            parse_company_facts(b'{"entityName": "X"}', accessions=set())

    def test_a_payload_with_no_us_gaap_taxonomy_is_refused(self):
        with self.assertRaises(MalformedCompanyFactsError):
            parse_company_facts(b'{"facts": {"dei": {}}}', accessions=set())

    def test_the_entity_name_is_carried_through(self):
        document = parse_company_facts(
            payload(revenue_entries([])), accessions=set()
        )

        self.assertEqual(document.entity_name, "NVIDIA CORP")


if __name__ == "__main__":
    unittest.main()
