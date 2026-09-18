"""The fetch_sec command's contract.

`collect` is replaced, so nothing here reaches the network or the SEC. What is checked is
the command's own behaviour: the JSON it prints, the exit codes it returns, and that it
does not open a database session.

The client and parser are covered in test_sec_edgar.py and test_form4_parser.py.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import contextlib
import io
import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

from app.fetch_sec import EXIT_FAILED, EXIT_OK, NVDA_CIK, main
from app.form4 import FootnoteRef, Form4Document, ReportingOwner, TransactionRow
from app.sec_edgar import (
    AccessDeniedError,
    DEFAULT_LIMIT,
    DiscoveryResult,
    FilingRecord,
    MissingUserAgentError,
    SecFilingRef,
)

RETRIEVED_AT = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def make_ref(**overrides) -> SecFilingRef:
    values = dict(
        accession_number="0002152188-26-000005",
        form_type="4",
        filing_date=date(2026, 9, 11),
        acceptance_datetime=datetime(2026, 9, 11, 21, 4, 47, tzinfo=timezone.utc),
        report_date=date(2026, 9, 9),
        primary_document="xslF345X06/wk-form4_1789160684.xml",
    )
    values.update(overrides)
    return SecFilingRef(**values)


def make_transaction(**overrides) -> TransactionRow:
    values = dict(
        source_table="nonDerivativeTable",
        row_position=0,
        security_title="Common Stock",
        transaction_date=date(2026, 9, 9),
        transaction_code="A",
        acquired_disposed="A",
        shares=Decimal("172507"),
        price_per_share=Decimal("0"),
        ownership_direct_indirect="D",
        nature_of_ownership=None,
        shares_owned_following=Decimal("172507"),
        footnote_refs=(
            FootnoteRef(field="transactionShares", footnote_id="F1"),
            FootnoteRef(field="transactionPricePerShare", footnote_id="F2"),
        ),
        underlying_security_title=None,
        underlying_shares=None,
        exercise_price=None,
        expiration_date=None,
    )
    values.update(overrides)
    return TransactionRow(**values)


def make_owner(**overrides) -> ReportingOwner:
    values = dict(
        owner_cik="0002152188",
        owner_name="Parker Nicholas P.",
        is_director=False,
        is_officer=True,
        is_ten_percent_owner=False,
        is_other=False,
        officer_title="EVP, Worldwide Field Ops",
        other_text=None,
    )
    values.update(overrides)
    return ReportingOwner(**values)


def make_document(**overrides) -> Form4Document:
    values = dict(
        document_type="4",
        schema_version="X0609",
        issuer_cik="0001045810",
        issuer_name="NVIDIA CORP",
        issuer_trading_symbol="NVDA",
        period_of_report=date(2026, 9, 9),
        date_of_original_submission=None,
        rule_10b5_1=False,
        footnotes={"F1": "Vesting schedule.", "F2": "Received for no consideration."},
        remarks=None,
        owners=(make_owner(),),
        transactions=(make_transaction(),),
        holding_rows_skipped=0,
    )
    values.update(overrides)
    return Form4Document(**values)


def make_record(**overrides) -> FilingRecord:
    values = dict(
        filing=make_ref(),
        document=make_document(),
        # The command does not print the document itself, so any bytes will do here; the
        # parser and client tests are where the real XML matters.
        source_xml=b"<ownershipDocument><documentType>4</documentType></ownershipDocument>",
        source_xml_url=(
            "https://www.sec.gov/Archives/edgar/data/1045810/000215218826000005/"
            "wk-form4_1789160684.xml"
        ),
        retrieved_at=RETRIEVED_AT,
    )
    values.update(overrides)
    return FilingRecord(**values)


def make_discovery(**overrides) -> DiscoveryResult:
    values = dict(
        issuer_cik=NVDA_CIK,
        issuer_name="NVIDIA CORP",
        tickers=("NVDA",),
        exchanges=("Nasdaq",),
        recent_filings_scanned=1001,
        older_filing_files_not_searched=1478,
        filings=(make_ref(),),
    )
    values.update(overrides)
    return DiscoveryResult(**values)


def invoke(argv, *, discovery=None, records=None, error=None):
    """Run the command with `collect` replaced, capturing both output streams."""
    stdout, stderr = io.StringIO(), io.StringIO()

    with mock.patch("app.fetch_sec.collect") as patched:
        if error is not None:
            patched.side_effect = error
        else:
            patched.return_value = (
                discovery if discovery is not None else make_discovery(),
                (make_record(),) if records is None else records,
            )
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)

    return code, stdout.getvalue(), stderr.getvalue(), patched


class SuccessTests(unittest.TestCase):
    def test_prints_json_and_exits_zero(self):
        code, out, err, _ = invoke([])

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        self.assertIn("filings", json.loads(out))

    def test_defaults_to_nvidia_and_verifies_the_ticker(self):
        """The default CIK is checked against SEC metadata, not taken on trust."""
        _, _, _, patched = invoke([])

        patched.assert_called_once_with(NVDA_CIK, DEFAULT_LIMIT, verify_nvda=True)

    def test_an_explicit_cik_is_not_ticker_checked(self):
        _, _, _, patched = invoke(["--cik", "0000320193"])

        patched.assert_called_once_with("0000320193", DEFAULT_LIMIT, verify_nvda=False)

    def test_the_limit_is_passed_through(self):
        _, _, _, patched = invoke(["--limit", "7"])

        patched.assert_called_once_with(NVDA_CIK, 7, verify_nvda=True)

    def test_the_discovery_scope_is_reported(self):
        _, out, _, _ = invoke([])
        discovery = json.loads(out)["discovery"]

        self.assertEqual(discovery["issuer_cik"], NVDA_CIK)
        self.assertEqual(discovery["issuer_name"], "NVIDIA CORP")
        self.assertEqual(discovery["tickers"], ["NVDA"])
        self.assertEqual(discovery["recent_filings_scanned"], 1001)
        self.assertEqual(discovery["older_filing_files_not_searched"], 1478)
        self.assertEqual(discovery["filing_count"], 1)

    def test_the_scope_sentence_says_the_history_is_incomplete(self):
        _, out, _, _ = invoke([])
        scope = json.loads(out)["discovery"]["scope"]

        self.assertIn("recent", scope)
        self.assertIn("NOT searched", scope)

    def test_an_empty_result_is_still_a_success(self):
        code, out, err, _ = invoke([], discovery=make_discovery(filings=()), records=())

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["filings"], [])


class FilingShapeTests(unittest.TestCase):
    def setUp(self):
        _, out, _, _ = invoke([])
        self.filing = json.loads(out)["filings"][0]

    def test_identifiers_dates_and_url(self):
        self.assertEqual(self.filing["accession_number"], "0002152188-26-000005")
        self.assertEqual(self.filing["form_type"], "4")
        self.assertFalse(self.filing["is_amendment"])
        self.assertEqual(self.filing["filing_date"], "2026-09-11")
        self.assertEqual(self.filing["period_of_report"], "2026-09-09")
        self.assertIn("wk-form4_1789160684.xml", self.filing["source_xml_url"])

    def test_timestamps_are_iso_with_an_offset(self):
        self.assertEqual(
            self.filing["acceptance_datetime"], "2026-09-11T21:04:47+00:00"
        )
        self.assertEqual(self.filing["retrieved_at"], "2026-09-18T12:00:00+00:00")

    def test_the_issuer_is_nested(self):
        self.assertEqual(
            self.filing["issuer"],
            {"cik": "0001045810", "name": "NVIDIA CORP", "trading_symbol": "NVDA"},
        )

    def test_owners_are_a_list_beside_the_transactions(self):
        self.assertEqual(len(self.filing["owners"]), 1)
        self.assertEqual(len(self.filing["transactions"]), 1)

    def test_owner_fields(self):
        owner = self.filing["owners"][0]

        self.assertEqual(owner["owner_cik"], "0002152188")
        self.assertEqual(owner["owner_name"], "Parker Nicholas P.")
        self.assertIs(owner["is_officer"], True)
        self.assertIs(owner["is_director"], False)
        self.assertEqual(owner["officer_title"], "EVP, Worldwide Field Ops")
        self.assertIsNone(owner["other_text"])

    def test_footnotes_are_printed_with_the_filing(self):
        self.assertEqual(set(self.filing["footnotes"]), {"F1", "F2"})

    def test_holding_rows_skipped_is_reported(self):
        self.assertEqual(self.filing["holding_rows_skipped"], 0)

    def test_a_missing_rule_10b5_1_is_json_null_not_false(self):
        _, out, _, _ = invoke(
            [], records=(make_record(document=make_document(rule_10b5_1=None)),)
        )
        self.assertIsNone(json.loads(out)["filings"][0]["rule_10b5_1"])

    def test_an_amendment_reports_its_original_submission_date(self):
        _, out, _, _ = invoke(
            [],
            records=(
                make_record(
                    filing=make_ref(form_type="4/A"),
                    document=make_document(
                        document_type="4/A",
                        date_of_original_submission=date(2026, 2, 18),
                    ),
                ),
            ),
        )
        filing = json.loads(out)["filings"][0]

        self.assertTrue(filing["is_amendment"])
        self.assertEqual(filing["date_of_original_submission"], "2026-02-18")


class DecimalSerialisationTests(unittest.TestCase):
    def test_decimals_are_json_strings_not_numbers(self):
        _, out, _, _ = invoke([])
        transaction = json.loads(out)["filings"][0]["transactions"][0]

        for field in ("shares", "price_per_share", "shares_owned_following"):
            self.assertIsInstance(transaction[field], str, f"{field} must be a string")

    def test_a_reported_zero_price_prints_as_zero(self):
        _, out, _, _ = invoke([])
        transaction = json.loads(out)["filings"][0]["transactions"][0]

        self.assertEqual(transaction["price_per_share"], "0")
        self.assertIsNotNone(transaction["price_per_share"])

    def test_a_missing_price_prints_as_null(self):
        _, out, _, _ = invoke(
            [],
            records=(make_record(document=make_document(
                transactions=(make_transaction(price_per_share=None),)
            )),),
        )
        self.assertIsNone(json.loads(out)["filings"][0]["transactions"][0]["price_per_share"])

    def test_precision_beyond_the_column_scale_survives(self):
        _, out, _, _ = invoke(
            [],
            records=(make_record(document=make_document(
                transactions=(make_transaction(price_per_share=Decimal("123.4567890")),)
            )),),
        )
        transaction = json.loads(out)["filings"][0]["transactions"][0]

        self.assertEqual(transaction["price_per_share"], "123.4567890")

    def test_footnote_references_carry_their_field(self):
        _, out, _, _ = invoke([])
        refs = json.loads(out)["filings"][0]["transactions"][0]["footnote_refs"]

        self.assertEqual(
            refs,
            [
                {"field": "transactionShares", "footnote_id": "F1"},
                {"field": "transactionPricePerShare", "footnote_id": "F2"},
            ],
        )

    def test_derivative_fields_are_present_as_null_on_a_plain_row(self):
        _, out, _, _ = invoke([])
        transaction = json.loads(out)["filings"][0]["transactions"][0]

        for field in ("underlying_security_title", "underlying_shares",
                      "exercise_price", "expiration_date"):
            self.assertIsNone(transaction[field], f"{field} should be null")


class FailureTests(unittest.TestCase):
    def test_a_client_failure_exits_nonzero(self):
        code, out, err, _ = invoke([], error=AccessDeniedError("SEC refused the request"))

        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(out, "")
        self.assertIn("SEC refused the request", err)

    def test_a_missing_user_agent_is_reported_clearly(self):
        code, _, err, _ = invoke(
            [], error=MissingUserAgentError("SEC_USER_AGENT is not set")
        )

        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("SEC_USER_AGENT", err)

    def test_the_failure_message_is_labelled_as_an_error(self):
        _, _, err, _ = invoke([], error=AccessDeniedError("nope"))

        self.assertTrue(err.startswith("error: "), err)

    def test_a_failure_prints_no_traceback(self):
        _, _, err, _ = invoke([], error=AccessDeniedError("nope"))

        self.assertNotIn("Traceback", err)
        self.assertNotIn('File "', err)

    def test_a_failure_prints_only_the_error_line(self):
        """No traceback and no environment dump -- just the message.

        Nothing on the SEC path is a credential. Its only configuration is
        `SEC_USER_AGENT`, which SEC requires to be a real contact address precisely
        because it is *not* a secret: it is sent on every request so SEC can reach you.
        So the guarantee worth testing is not that it is hidden, but that nothing else
        comes out with the error.
        """
        code, out, err, _ = invoke([], error=AccessDeniedError("SEC refused the request"))

        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(out, "")
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(err.startswith("error: "))


if __name__ == "__main__":
    unittest.main()