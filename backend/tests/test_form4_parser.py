"""The Form 4 parser, offline: fixture bytes in, typed values out.

No network and no database. Every case here is either a real SEC filing or a synthetic one
written to cover a shape the real filings did not happen to contain; see
`tests/fixtures/sec/README.md` for which is which.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import pathlib
import unittest
from datetime import date
from decimal import Decimal

from app.form4 import (
    IssuerMismatchError,
    MalformedDocumentError,
    NumberParseError,
    parse_ownership_document,
)

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "sec"

REAL_NVDA = "real_nvda_form4_0002152188-26-000005.xml"
MULTIPLE_OWNERS = "synthetic_multiple_owners_one_transaction.xml"
DERIVATIVE = "synthetic_derivative_and_nonderivative.xml"
NAMESPACED = "synthetic_namespaced.xml"
AMENDMENT = "synthetic_amendment.xml"
ZERO_TRANSACTIONS = "synthetic_zero_transactions.xml"
MALFORMED_NUMBER = "synthetic_malformed_number.xml"
EXTERNAL_ENTITY = "synthetic_external_entity.xml"

NVDA_CIK = "0001045810"
SYNTHETIC_CIK = "0000320193"


def load(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def parse(name: str, **kwargs):
    return parse_ownership_document(load(name), **kwargs)


def document_with(transaction_body: str, *, table: str = "nonDerivativeTable") -> bytes:
    """A minimal valid document wrapping one transaction snippet, for inline cases."""
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0609</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-01-05</periodOfReport>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>INLINE EXAMPLE CORP</issuerName>
    <issuerTradingSymbol>INL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000000009</rptOwnerCik>
      <rptOwnerName>INLINE PERSON</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <{table}>{transaction_body}</{table}>
</ownershipDocument>""".encode()


NON_DERIVATIVE_TEMPLATE = """
  <nonDerivativeTransaction>
    <securityTitle><value>Common Stock</value></securityTitle>
    <transactionDate><value>2026-01-05</value></transactionDate>
    <transactionCoding>
      <transactionFormType>4</transactionFormType>
      <transactionCode>S</transactionCode>
    </transactionCoding>
    <transactionAmounts>
      <transactionShares><value>100</value></transactionShares>
      {price}
      <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
    </transactionAmounts>
  </nonDerivativeTransaction>"""


def non_derivative(price_element: str) -> bytes:
    return document_with(NON_DERIVATIVE_TEMPLATE.format(price=price_element))


class RealFilingTests(unittest.TestCase):
    """One genuine SEC filing, downloaded verbatim."""

    def setUp(self):
        self.document = parse(REAL_NVDA, expected_issuer_cik=NVDA_CIK)

    def test_the_issuer_is_read_and_matches(self):
        self.assertEqual(self.document.issuer_cik, NVDA_CIK)
        self.assertEqual(self.document.issuer_name, "NVIDIA CORP")
        self.assertEqual(self.document.issuer_trading_symbol, "NVDA")

    def test_the_document_type_is_not_an_amendment(self):
        self.assertEqual(self.document.document_type, "4")
        self.assertFalse(self.document.is_amendment)
        self.assertIsNone(self.document.date_of_original_submission)

    def test_the_owner_is_read(self):
        owner = self.document.owners[0]

        self.assertEqual(owner.owner_cik, "0002152188")
        self.assertEqual(owner.owner_name, "Parker Nicholas P.")
        self.assertTrue(owner.is_officer)
        self.assertFalse(owner.is_director)
        self.assertFalse(owner.is_ten_percent_owner)
        self.assertEqual(owner.officer_title, "EVP, Worldwide Field Ops")

    def test_a_reported_zero_price_stays_zero_and_does_not_become_missing(self):
        """This filing really does report 0, with a footnote explaining the RSU award."""
        row = self.document.transactions[0]

        self.assertEqual(row.price_per_share, Decimal("0"))
        self.assertIsNotNone(row.price_per_share)

    def test_the_transaction_is_read(self):
        row = self.document.transactions[0]

        self.assertEqual(row.source_table, "nonDerivativeTable")
        self.assertEqual(row.row_position, 0)
        self.assertEqual(row.transaction_date, date(2026, 9, 9))
        self.assertEqual(row.transaction_code, "A")
        self.assertEqual(row.acquired_disposed, "A")
        self.assertEqual(row.shares, Decimal("172507"))
        self.assertEqual(row.ownership_direct_indirect, "D")
        self.assertEqual(row.shares_owned_following, Decimal("172507"))
        self.assertEqual(row.security_title, "Common Stock")

    def test_footnotes_are_resolved_to_their_text(self):
        self.assertEqual(
            set(self.document.footnotes), {"F1", "F2"}
        )
        self.assertIn("restricted stock units", self.document.footnotes["F1"])
        self.assertIn("no consideration", self.document.footnotes["F2"])

    def test_footnote_references_name_the_field_they_sat_under(self):
        """F1 annotates the share count and F2 the price, and that must not be flattened."""
        refs = {
            (ref.field, ref.footnote_id) for ref in self.document.transactions[0].footnote_refs
        }

        self.assertEqual(
            refs,
            {("transactionShares", "F1"), ("transactionPricePerShare", "F2")},
        )

    def test_there_are_no_holding_rows_in_this_filing(self):
        self.assertEqual(self.document.holding_rows_skipped, 0)

    def test_the_rule_10b5_1_indicator_is_false_not_missing(self):
        self.assertIs(self.document.rule_10b5_1, False)


class MissingVersusPresentTests(unittest.TestCase):
    def test_an_absent_price_is_none(self):
        document = parse_ownership_document(non_derivative(""))

        self.assertIsNone(document.transactions[0].price_per_share)

    def test_an_empty_price_element_is_none(self):
        document = parse_ownership_document(
            non_derivative(
                "<transactionPricePerShare><value></value></transactionPricePerShare>"
            )
        )

        self.assertIsNone(document.transactions[0].price_per_share)

    def test_a_reported_zero_price_is_decimal_zero(self):
        document = parse_ownership_document(
            non_derivative(
                "<transactionPricePerShare><value>0</value></transactionPricePerShare>"
            )
        )

        self.assertEqual(document.transactions[0].price_per_share, Decimal("0"))

    def test_an_absent_flag_is_none_while_a_present_zero_is_false(self):
        """`rule_10b5_1` distinguishes "not stated" from "stated as no"."""
        absent = parse(ZERO_TRANSACTIONS)
        present = parse(REAL_NVDA)

        self.assertIsNone(absent.rule_10b5_1)
        self.assertIs(present.rule_10b5_1, False)
        self.assertIs(parse(DERIVATIVE).rule_10b5_1, True)


class DecimalPrecisionTests(unittest.TestCase):
    def test_a_price_beyond_the_column_scale_survives_exactly(self):
        """numeric(18,6) cannot hold this. Rounding it here would hide that fact."""
        document = parse(NAMESPACED)
        price = document.transactions[0].price_per_share

        self.assertEqual(price, Decimal("123.4567890"))
        self.assertEqual(str(price), "123.4567890")

    def test_a_whole_share_count_stays_exact(self):
        document = parse(MULTIPLE_OWNERS)

        self.assertEqual(document.transactions[0].shares, Decimal("1000"))
        self.assertEqual(str(document.transactions[0].shares), "1000")

    def test_a_negative_price_is_not_silently_corrected(self):
        """The parser reports what the document says; it does not sanitise it."""
        document = parse_ownership_document(
            non_derivative(
                "<transactionPricePerShare><value>-1.00</value></transactionPricePerShare>"
            )
        )

        self.assertEqual(document.transactions[0].price_per_share, Decimal("-1.00"))


class MultipleOwnerTests(unittest.TestCase):
    def test_two_owners_with_one_transaction_stay_two_and_one(self):
        document = parse(MULTIPLE_OWNERS)

        self.assertEqual(len(document.owners), 2)
        self.assertEqual(
            len(document.transactions),
            1,
            "owners attach to the filing; they must not multiply transaction rows",
        )

    def test_both_owners_are_read_with_their_own_relationships(self):
        document = parse(MULTIPLE_OWNERS)
        first, second = document.owners

        self.assertEqual(first.owner_name, "ALPHA HOLDINGS LLC")
        self.assertTrue(first.is_ten_percent_owner)
        self.assertIsNone(first.other_text)

        self.assertEqual(second.owner_name, "BETA MANAGEMENT GP")
        self.assertTrue(second.is_other)
        self.assertEqual(second.other_text, "General partner of the other reporting owner")

    def test_indirect_ownership_keeps_its_description(self):
        row = parse(MULTIPLE_OWNERS).transactions[0]

        self.assertEqual(row.ownership_direct_indirect, "I")
        self.assertEqual(row.nature_of_ownership, "By Alpha Holdings LLC")


class DerivativeTests(unittest.TestCase):
    def setUp(self):
        self.document = parse(DERIVATIVE)

    def test_both_source_tables_are_read(self):
        tables = [row.source_table for row in self.document.transactions]

        self.assertEqual(tables, ["nonDerivativeTable", "derivativeTable"])

    def test_row_positions_are_zero_based_within_each_table(self):
        positions = [row.row_position for row in self.document.transactions]

        self.assertEqual(positions, [0, 0])

    def test_derivative_fields_are_read(self):
        derivative = self.document.transactions[1]

        self.assertEqual(derivative.underlying_security_title, "Common Stock")
        self.assertEqual(derivative.underlying_shares, Decimal("5000"))
        self.assertEqual(derivative.exercise_price, Decimal("50.00"))
        self.assertEqual(derivative.expiration_date, date(2030, 3, 1))

    def test_a_non_derivative_row_has_no_derivative_fields(self):
        non_derivative_row = self.document.transactions[0]

        self.assertIsNone(non_derivative_row.underlying_security_title)
        self.assertIsNone(non_derivative_row.underlying_shares)
        self.assertIsNone(non_derivative_row.exercise_price)
        self.assertIsNone(non_derivative_row.expiration_date)

    def test_holding_rows_are_not_transactions_but_are_counted(self):
        """One holding row in each table. Two rows seen, none of them traded."""
        self.assertEqual(len(self.document.transactions), 2)
        self.assertEqual(self.document.holding_rows_skipped, 2)

    def test_the_exercise_price_element_is_also_accepted_under_its_other_name(self):
        """Schema versions disagree on the name; both are read."""
        body = NON_DERIVATIVE_TEMPLATE.replace(
            "<transactionAmounts>",
            "<conversionOrExercisePrice><value>12.34</value></conversionOrExercisePrice>"
            "<transactionAmounts>",
        )
        document = parse_ownership_document(
            document_with(body, table="nonDerivativeTable")
        )

        self.assertEqual(document.transactions[0].exercise_price, Decimal("12.34"))


class NamespaceTests(unittest.TestCase):
    def test_a_namespaced_document_parses_the_same_as_a_plain_one(self):
        document = parse(NAMESPACED)

        self.assertEqual(document.issuer_cik, SYNTHETIC_CIK)
        self.assertEqual(document.issuer_name, "SYNTHETIC EXAMPLE CORP")
        self.assertEqual(len(document.transactions), 1)
        self.assertEqual(document.owners[0].owner_name, "DELTA DIRECTOR")


class AmendmentTests(unittest.TestCase):
    def test_an_amendment_is_marked_and_carries_the_original_submission_date(self):
        document = parse(AMENDMENT)

        self.assertEqual(document.document_type, "4/A")
        self.assertTrue(document.is_amendment)
        self.assertEqual(document.date_of_original_submission, date(2026, 2, 18))

    def test_an_ordinary_form_4_has_no_original_submission_date(self):
        document = parse(REAL_NVDA)

        self.assertFalse(document.is_amendment)
        self.assertIsNone(document.date_of_original_submission)

    def test_nothing_links_the_amendment_to_an_accession(self):
        """The document does not say which filing it amends, so nothing here invents one."""
        document = parse(AMENDMENT)

        self.assertFalse(hasattr(document, "amends_accession_number"))
        self.assertFalse(hasattr(document, "amends_filing_id"))


class ZeroTransactionTests(unittest.TestCase):
    def test_a_filing_with_no_trades_parses_with_an_empty_transaction_list(self):
        document = parse(ZERO_TRANSACTIONS)

        self.assertEqual(document.transactions, ())
        self.assertEqual(len(document.owners), 1)

    def test_remarks_are_kept(self):
        self.assertEqual(parse(ZERO_TRANSACTIONS).remarks, "No transactions to report.")


class ParseFailureTests(unittest.TestCase):
    def test_a_malformed_share_count_refuses_the_whole_document(self):
        """Not a skipped row: a partial document would look complete and be wrong."""
        with self.assertRaises(NumberParseError) as caught:
            parse(MALFORMED_NUMBER)

        self.assertIn("not-a-number", str(caught.exception))

    def test_a_number_parse_error_is_also_a_malformed_document(self):
        with self.assertRaises(MalformedDocumentError):
            parse(MALFORMED_NUMBER)

    def test_a_non_finite_price_is_refused(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaises(NumberParseError):
                    parse_ownership_document(
                        non_derivative(
                            f"<transactionPricePerShare><value>{value}</value>"
                            "</transactionPricePerShare>"
                        )
                    )

    def test_an_external_entity_is_refused_and_nothing_is_read(self):
        """The document points an entity at file:///etc/passwd. It must not be followed."""
        with self.assertRaises(MalformedDocumentError) as caught:
            parse(EXTERNAL_ENTITY)

        message = str(caught.exception)
        self.assertIn("undefined entity", message)
        self.assertNotIn("root:", message)

    def test_something_that_is_not_xml_at_all_is_refused(self):
        with self.assertRaises(MalformedDocumentError):
            parse_ownership_document(b"<html><body>SEC error page</body></html>")

    def test_an_empty_body_is_refused(self):
        with self.assertRaises(MalformedDocumentError):
            parse_ownership_document(b"")

    def test_an_issuer_mismatch_is_refused(self):
        with self.assertRaises(IssuerMismatchError) as caught:
            parse(REAL_NVDA, expected_issuer_cik="0000320193")

        self.assertIn("0001045810", str(caught.exception))

    def test_an_issuer_match_is_not_confused_by_padding(self):
        """EDGAR pads CIKs to ten digits; a caller may not have."""
        document = parse(REAL_NVDA, expected_issuer_cik="1045810")

        self.assertEqual(document.issuer_cik, NVDA_CIK)

    def test_a_document_without_an_issuer_is_refused(self):
        with self.assertRaises(MalformedDocumentError):
            parse_ownership_document(
                b"<ownershipDocument><documentType>4</documentType></ownershipDocument>"
            )

    def test_an_unexpected_element_inside_a_table_is_refused(self):
        """Guessing whether an unknown row is a transaction is worse than stopping."""
        body = NON_DERIVATIVE_TEMPLATE + "\n  <mysteryRow><value>1</value></mysteryRow>"

        with self.assertRaises(MalformedDocumentError) as caught:
            parse_ownership_document(document_with(body))

        self.assertIn("mysteryRow", str(caught.exception))

    def test_a_missing_transaction_date_is_refused(self):
        body = NON_DERIVATIVE_TEMPLATE.replace(
            "<transactionDate><value>2026-01-05</value></transactionDate>", ""
        )

        with self.assertRaises(MalformedDocumentError):
            parse_ownership_document(document_with(body))

    def test_a_table_may_be_absent_entirely(self):
        """A document that never mentions a table is not malformed."""
        minimal = b"""<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>NO TABLES CORP</issuerName>
  </issuer>
</ownershipDocument>"""

        document = parse_ownership_document(minimal)

        self.assertEqual(document.transactions, ())
        self.assertEqual(document.holding_rows_skipped, 0)
        self.assertEqual(document.owners, ())


if __name__ == "__main__":
    unittest.main()