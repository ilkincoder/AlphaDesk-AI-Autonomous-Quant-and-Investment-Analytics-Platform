"""Ingestion, against a real PostgreSQL and mocked provider payloads.

No network: the payloads here are built directly from the clients' own return types, which
is what `ingest` actually receives. The clients themselves are covered by their own tests.

Every test runs inside a transaction that is rolled back, so nothing is ever committed to
`alphadesk_test` and tests cannot see each other's rows.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.form4 import FootnoteRef, Form4Document, ReportingOwner, TransactionRow
from app.ingestion import (
    IngestionConflictError,
    IngestionError,
    IngestionIdentityError,
    ingest,
)
from app.models import (
    Company,
    DailyPrice,
    IngestionRun,
    InsiderReportingOwner,
    InsiderTransaction,
    SecFiling,
)
from app.sec_edgar import DiscoveryResult, FilingRecord, SecFilingRef
from app.twelvedata import DailyBar, DailyPriceSeries
from tests.testdb import test_engine

NVDA_CIK = "0001045810"
STARTED_AT = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 9, 18, 11, 59, tzinfo=timezone.utc)

# The value Step 2 refused to round and Step 4 exists to preserve.
SEVEN_DP_CLOSE = "225.0099945"


def make_bar(
    trading_date: date = date(2026, 9, 17),
    *,
    open_: str = "218.38000",
    high: str = "219.91000",
    low: str = "217.14999",
    close: str = "219.34000",
    volume: int = 93_960_500,
) -> DailyBar:
    return DailyBar(
        trading_date=trading_date,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


def make_series(bars=None, **overrides) -> DailyPriceSeries:
    chosen = tuple(bars) if bars is not None else (make_bar(),)
    values = dict(
        symbol="NVDA",
        exchange="NASDAQ",
        currency="USD",
        exchange_timezone="America/New_York",
        provider="twelve_data",
        interval="1day",
        adjustment_basis="adjusted",
        provider_adjust_mode="splits",
        retrieved_at=RETRIEVED_AT,
        requested_bars=len(chosen),
        bars=chosen,
    )
    values.update(overrides)
    return DailyPriceSeries(**values)


def make_owner(
    cik: str = "0002152188", name: str = "Parker Nicholas P.", **overrides
) -> ReportingOwner:
    values = dict(
        owner_cik=cik,
        owner_name=name,
        is_director=False,
        is_officer=True,
        is_ten_percent_owner=False,
        is_other=False,
        officer_title="EVP, Worldwide Field Ops",
        other_text=None,
    )
    values.update(overrides)
    return ReportingOwner(**values)


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


def make_document(**overrides) -> Form4Document:
    values = dict(
        document_type="4",
        schema_version="X0609",
        issuer_cik=NVDA_CIK,
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


def make_ref(accession: str = "0002152188-26-000005", **overrides) -> SecFilingRef:
    values = dict(
        accession_number=accession,
        form_type="4",
        filing_date=date(2026, 9, 11),
        acceptance_datetime=datetime(2026, 9, 11, 21, 4, 47, tzinfo=timezone.utc),
        report_date=date(2026, 9, 9),
        primary_document="xslF345X06/wk-form4_1789160684.xml",
    )
    values.update(overrides)
    return SecFilingRef(**values)


def make_record(accession: str = "0002152188-26-000005", **overrides) -> FilingRecord:
    values = dict(
        filing=make_ref(accession),
        document=make_document(),
        source_xml=(
            b'<?xml version="1.0"?><ownershipDocument>'
            b"<documentType>4</documentType></ownershipDocument>"
        ),
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


class IngestionTestCase(unittest.TestCase):
    """A session inside a transaction that is always rolled back."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def setUp(self) -> None:
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)

    def tearDown(self) -> None:
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def run_ingest(self, *, series=None, discovery=None, records=None, dry_run=False):
        return ingest(
            self.session,
            series=series if series is not None else make_series(),
            discovery=discovery if discovery is not None else make_discovery(),
            records=records if records is not None else (make_record(),),
            parameters={"bars": 1, "filings": 1},
            started_at=STARTED_AT,
            dry_run=dry_run,
        )

    def count(self, model: type) -> int:
        return self.session.scalar(select(func.count()).select_from(model))


class FirstRunTests(IngestionTestCase):
    def test_stores_a_company_a_price_and_a_filing(self):
        summary = self.run_ingest()

        self.assertEqual(self.count(Company), 1)
        self.assertEqual(self.count(DailyPrice), 1)
        self.assertEqual(self.count(SecFiling), 1)
        self.assertEqual(self.count(InsiderReportingOwner), 1)
        self.assertEqual(self.count(InsiderTransaction), 1)
        self.assertEqual(self.count(IngestionRun), 1)

        self.assertEqual(summary.counts["daily_prices"].inserted, 1)
        self.assertEqual(summary.counts["daily_prices"].unchanged, 0)

    def test_the_company_is_resolved_by_cik(self):
        self.run_ingest()
        company = self.session.scalar(select(Company))

        self.assertEqual(company.sec_issuer_cik, NVDA_CIK)
        self.assertEqual(company.ticker, "NVDA")
        self.assertEqual(company.exchange, "NASDAQ")

    def test_the_price_row_carries_the_series_metadata(self):
        self.run_ingest()
        price = self.session.scalar(select(DailyPrice))

        self.assertEqual(price.provider, "twelve_data")
        self.assertEqual(price.adjustment_basis, "adjusted")
        self.assertEqual(price.provider_adjust_mode, "splits")
        self.assertIsNone(
            price.volume_adjustment,
            "the provider states nothing about volume adjustment, so it stays unknown",
        )
        self.assertEqual(price.retrieved_at, RETRIEVED_AT)

    def test_the_summary_reports_scope_rather_than_claiming_completeness(self):
        summary = self.run_ingest()

        self.assertEqual(summary.company_ticker, "NVDA")
        self.assertEqual(summary.recent_filings_scanned, 1001)
        self.assertEqual(summary.older_filing_files_not_searched, 1478)
        self.assertIn("NOT searched", summary.discovery_scope)
        self.assertTrue(any("bounded sample" in w for w in summary.warnings))
        self.assertFalse(summary.dry_run)

    def test_the_run_record_is_written_inside_the_transaction(self):
        self.run_ingest()
        run = self.session.scalar(select(IngestionRun))

        self.assertEqual(run.started_at, STARTED_AT)
        self.assertEqual(run.parameters["bars"], 1)
        self.assertEqual(run.summary["company"]["ticker"], "NVDA")


class RepeatRunTests(IngestionTestCase):
    def test_a_repeat_run_inserts_nothing_and_changes_nothing(self):
        self.run_ingest()

        price_id = self.session.scalar(select(DailyPrice.id))
        filing_id = self.session.scalar(select(SecFiling.id))
        transaction_id = self.session.scalar(select(InsiderTransaction.id))
        original_retrieved_at = self.session.scalar(select(DailyPrice.retrieved_at))

        summary = self.run_ingest()

        self.assertEqual(self.count(DailyPrice), 1)
        self.assertEqual(self.count(SecFiling), 1)
        self.assertEqual(self.count(InsiderTransaction), 1)

        self.assertEqual(summary.counts["daily_prices"].inserted, 0)
        self.assertEqual(summary.counts["daily_prices"].unchanged, 1)
        self.assertEqual(summary.counts["sec_filings"].inserted, 0)
        self.assertEqual(summary.counts["sec_filings"].unchanged, 1)
        self.assertEqual(summary.counts["companies"].unchanged, 1)

        self.assertEqual(self.session.scalar(select(DailyPrice.id)), price_id)
        self.assertEqual(self.session.scalar(select(SecFiling.id)), filing_id)
        self.assertEqual(
            self.session.scalar(select(InsiderTransaction.id)), transaction_id
        )
        self.assertEqual(
            self.session.scalar(select(DailyPrice.retrieved_at)),
            original_retrieved_at,
            "the original retrieval timestamp must survive a re-run untouched",
        )

    def test_a_second_run_still_records_that_it_ran(self):
        self.run_ingest()
        self.run_ingest()

        self.assertEqual(self.count(IngestionRun), 2)

    def test_a_changed_price_under_the_same_identity_is_a_conflict(self):
        self.run_ingest()
        changed = make_series(bars=[make_bar(close="219.99")])

        with self.assertRaises(IngestionConflictError) as caught:
            self.run_ingest(series=changed)

        message = str(caught.exception)
        self.assertIn("daily bar", message)
        self.assertIn("close", message)
        self.assertIn("219.34000", message)
        self.assertIn("219.99", message)

    def test_a_conflict_does_not_overwrite_the_stored_row(self):
        self.run_ingest()
        changed = make_series(bars=[make_bar(close="219.99")])

        with self.assertRaises(IngestionConflictError):
            self.run_ingest(series=changed)

        self.session.rollback()
        self.assertEqual(self.count(DailyPrice), 0, "the whole transaction rolled back")

    def test_a_changed_filing_is_a_conflict_naming_the_accession(self):
        self.run_ingest()
        changed = (
            make_record(document=make_document(rule_10b5_1=True)),
        )

        with self.assertRaises(IngestionConflictError) as caught:
            self.run_ingest(records=changed)

        self.assertIn("0002152188-26-000005", str(caught.exception))
        self.assertIn("rule_10b5_1", str(caught.exception))

    def test_the_conflict_message_names_the_change_without_dumping_the_payload(self):
        """A source XML runs to kilobytes and has no business in an error string."""
        self.run_ingest()
        bulky = b"<ownershipDocument>" + b"x" * 5000 + b"</ownershipDocument>"

        with self.assertRaises(IngestionConflictError) as caught:
            self.run_ingest(records=(make_record(source_xml=bulky),))

        message = str(caught.exception)
        # It says which record and which column, which is what someone needs to act on...
        self.assertIn("0002152188-26-000005", message)
        self.assertIn("source_xml", message)
        # ...and not the five kilobytes that column happens to hold.
        self.assertNotIn("x" * 100, message)
        self.assertLess(len(message), 900)

    def test_a_changed_owner_flag_is_a_conflict(self):
        self.run_ingest()
        changed = (
            make_record(document=make_document(owners=(make_owner(is_officer=False),))),
        )

        with self.assertRaises(IngestionConflictError) as caught:
            self.run_ingest(records=changed)

        self.assertIn("is_officer", str(caught.exception))


class PrecisionTests(IngestionTestCase):
    def test_a_price_beyond_the_column_scale_survives_around_trip(self):
        """The Decimal Step 2 measured, written and read back through PostgreSQL."""
        series = make_series(
            bars=[make_bar(open_="200", high="226", low="190.0099945",
                           close=SEVEN_DP_CLOSE)]
        )

        self.run_ingest(series=series)
        self.session.expire_all()

        stored = self.session.scalar(select(DailyPrice.close))
        self.assertEqual(stored, Decimal(SEVEN_DP_CLOSE))
        self.assertEqual(
            str(stored), SEVEN_DP_CLOSE, "the seventh decimal place must still be there"
        )

    def test_a_filing_transaction_price_keeps_its_precision_too(self):
        row = make_transaction(price_per_share=Decimal("227.6954321"))
        records = (make_record(document=make_document(transactions=(row,))),)

        self.run_ingest(records=records)
        self.session.expire_all()

        stored = self.session.scalar(
            select(InsiderTransaction.price_per_share)
        )
        self.assertEqual(stored, Decimal("227.6954321"))

    def test_a_missing_price_and_a_reported_zero_stay_distinguishable(self):
        rows = (
            make_transaction(row_position=0, price_per_share=None),
            make_transaction(row_position=1, price_per_share=Decimal("0")),
        )
        records = (make_record(document=make_document(transactions=rows)),)

        self.run_ingest(records=records)
        self.session.expire_all()

        stored = self.session.scalars(
            select(InsiderTransaction.price_per_share).order_by(
                InsiderTransaction.row_position
            )
        ).all()
        self.assertEqual(stored, [None, Decimal("0")])
        self.assertIsNone(stored[0])
        self.assertIsNotNone(stored[1])


class OwnerAndTransactionShapeTests(IngestionTestCase):
    def test_two_owners_with_one_transaction_stay_two_and_one(self):
        """The fan-out Step 3's fixture is about, now proved against the database."""
        document = make_document(
            owners=(
                make_owner(cik="0000000001", name="ALPHA HOLDINGS LLC"),
                make_owner(cik="0000000002", name="BETA MANAGEMENT GP"),
            ),
            transactions=(make_transaction(),),
        )

        self.run_ingest(records=(make_record(document=document),))

        self.assertEqual(self.count(InsiderReportingOwner), 2)
        self.assertEqual(
            self.count(InsiderTransaction),
            1,
            "owners attach to the filing; they must not multiply transaction rows",
        )

    def test_two_identical_looking_source_rows_stay_two_transactions(self):
        """Identity is provenance, never date-and-amount."""
        rows = (
            make_transaction(row_position=0),
            make_transaction(row_position=1),
        )

        self.run_ingest(records=(make_record(document=make_document(transactions=rows)),))

        self.assertEqual(self.count(InsiderTransaction), 2)

    def test_the_same_position_in_the_other_table_is_a_separate_row(self):
        rows = (
            make_transaction(row_position=0),
            make_transaction(
                row_position=0,
                source_table="derivativeTable",
                security_title="Employee Stock Option (Right to Buy)",
            ),
        )

        self.run_ingest(records=(make_record(document=make_document(transactions=rows)),))

        self.assertEqual(self.count(InsiderTransaction), 2)

    def test_an_owner_flag_the_document_omits_is_stored_as_null_not_false(self):
        document = make_document(owners=(make_owner(is_director=None),))

        self.run_ingest(records=(make_record(document=document),))
        self.session.expire_all()

        self.assertIsNone(self.session.scalar(select(InsiderReportingOwner.is_director)))

    def test_a_repeat_payload_with_a_duplicate_identity_is_refused(self):
        """ON CONFLICT would have called the second copy "unchanged", which would be a lie."""
        series = make_series(bars=[make_bar(), make_bar()])

        with self.assertRaises(IngestionError) as caught:
            self.run_ingest(series=series)

        self.assertIn("more than one bar", str(caught.exception))

    def test_a_filing_payload_with_a_repeated_accession_is_refused(self):
        with self.assertRaises(IngestionError):
            self.run_ingest(records=(make_record(), make_record()))


class SecFieldStorageTests(IngestionTestCase):
    def test_footnotes_remarks_and_field_references_are_stored(self):
        document = make_document(remarks="Amended for clarity.")
        self.run_ingest(records=(make_record(document=document),))
        self.session.expire_all()

        filing = self.session.scalar(select(SecFiling))
        self.assertEqual(filing.footnotes, {
            "F1": "Vesting schedule.",
            "F2": "Received for no consideration.",
        })
        self.assertEqual(filing.remarks, "Amended for clarity.")

        row = self.session.scalar(select(InsiderTransaction))
        self.assertEqual(
            row.footnote_refs,
            [
                {"field": "transactionShares", "footnote_id": "F1"},
                {"field": "transactionPricePerShare", "footnote_id": "F2"},
            ],
        )
        self.assertIn("[F1] Vesting schedule.", row.footnotes)
        self.assertIn("[F2] Received for no consideration.", row.footnotes)

    def test_the_rule_10b5_1_indicator_distinguishes_unknown_from_false(self):
        """Each case needs its own transaction: the same accession with a different
        indicator is a conflict, which is exactly what the previous test asserts."""
        for value, expected in ((None, None), (False, False), (True, True)):
            with self.subTest(value=value):
                with self.engine.connect() as connection:
                    transaction = connection.begin()
                    session = Session(bind=connection)
                    try:
                        ingest(
                            session,
                            series=make_series(),
                            discovery=make_discovery(),
                            records=(
                                make_record(document=make_document(rule_10b5_1=value)),
                            ),
                            parameters={},
                            started_at=STARTED_AT,
                        )
                        self.assertIs(
                            session.scalar(select(SecFiling.rule_10b5_1)), expected
                        )
                    finally:
                        session.close()
                        transaction.rollback()

    def test_derivative_details_survive_storage(self):
        row = make_transaction(
            source_table="derivativeTable",
            security_title="Employee Stock Option (Right to Buy)",
            underlying_security_title="Common Stock",
            underlying_shares=Decimal("5000"),
            exercise_price=Decimal("50.00"),
            expiration_date=date(2030, 3, 1),
        )

        self.run_ingest(records=(make_record(document=make_document(transactions=(row,))),))
        self.session.expire_all()

        stored = self.session.scalar(select(InsiderTransaction))
        self.assertTrue(stored.is_derivative)
        self.assertEqual(stored.underlying_security_title, "Common Stock")
        self.assertEqual(stored.underlying_shares, Decimal("5000"))
        self.assertEqual(stored.exercise_price, Decimal("50.00"))
        self.assertEqual(stored.expiration_date, date(2030, 3, 1))

    def test_the_excluded_holding_row_count_is_stored(self):
        document = make_document(holding_rows_skipped=2)

        self.run_ingest(records=(make_record(document=document),))
        self.session.expire_all()

        self.assertEqual(self.session.scalar(select(SecFiling.holding_rows_skipped)), 2)

    def test_the_source_xml_and_its_hash_are_stored(self):
        import hashlib

        record = make_record()
        self.run_ingest(records=(record,))
        self.session.expire_all()

        filing = self.session.scalar(select(SecFiling))
        self.assertEqual(filing.source_xml, record.source_xml.decode("utf-8"))
        self.assertEqual(
            filing.source_xml_sha256, hashlib.sha256(record.source_xml).hexdigest()
        )
        self.assertEqual(filing.source_document_url, record.source_xml_url)

    def test_owner_relationships_are_stored(self):
        owner = make_owner(
            is_director=True, is_officer=False, officer_title=None,
            is_ten_percent_owner=True,
        )

        self.run_ingest(records=(make_record(document=make_document(owners=(owner,))),))
        self.session.expire_all()

        stored = self.session.scalar(select(InsiderReportingOwner))
        self.assertTrue(stored.is_director)
        self.assertFalse(stored.is_officer)
        self.assertTrue(stored.is_ten_percent_owner)
        self.assertEqual(stored.reporting_owner_cik, owner.owner_cik)


class AmendmentTests(IngestionTestCase):
    def test_an_amendment_is_stored_separately_without_an_invented_link(self):
        amendment = make_record(
            accession="0001045810-23-000242",
            filing=make_ref(
                accession="0001045810-23-000242", form_type="4/A"
            ),
            document=make_document(
                document_type="4/A",
                date_of_original_submission=date(2023, 11, 28),
            ),
        )
        original = make_record()

        self.run_ingest(records=(original, amendment))
        self.session.expire_all()

        self.assertEqual(self.count(SecFiling), 2)
        stored = self.session.scalars(select(SecFiling)).all()
        by_accession = {filing.accession_number: filing for filing in stored}

        self.assertFalse(by_accession["0002152188-26-000005"].is_amendment)
        amended = by_accession["0001045810-23-000242"]
        self.assertTrue(amended.is_amendment)
        self.assertEqual(amended.date_of_original_submission, date(2023, 11, 28))

        for filing in stored:
            self.assertIsNone(
                filing.amends_filing_id,
                "nothing may guess which accession an amendment replaces",
            )

    def test_the_summary_warns_that_amendments_are_not_safe_to_sum(self):
        amendment = make_record(
            accession="0001045810-23-000242",
            filing=make_ref(accession="0001045810-23-000242", form_type="4/A"),
            document=make_document(document_type="4/A"),
        )

        summary = self.run_ingest(records=(make_record(), amendment))

        self.assertEqual(summary.amendment_count, 1)
        self.assertTrue(
            any("double-count" in warning for warning in summary.warnings),
            summary.warnings,
        )


class AdjustmentModeTests(IngestionTestCase):
    def test_two_adjustment_modes_for_one_day_coexist(self):
        splits = make_series(provider_adjust_mode="splits")
        dividends = make_series(provider_adjust_mode="dividends")

        self.run_ingest(series=splits)
        summary = self.run_ingest(series=dividends)

        self.assertEqual(self.count(DailyPrice), 2)
        self.assertEqual(summary.counts["daily_prices"].inserted, 1)
        self.assertEqual(summary.counts["daily_prices"].unchanged, 0)

    def test_the_same_mode_twice_does_not_duplicate(self):
        self.run_ingest(series=make_series(provider_adjust_mode="splits"))
        summary = self.run_ingest(series=make_series(provider_adjust_mode="splits"))

        self.assertEqual(self.count(DailyPrice), 1)
        self.assertEqual(summary.counts["daily_prices"].unchanged, 1)


class RefusalTests(IngestionTestCase):
    def test_sources_that_disagree_about_the_company_write_nothing(self):
        series = make_series(symbol="AMD")
        discovery = make_discovery(tickers=("NVDA",))

        with self.assertRaises(IngestionIdentityError) as caught:
            self.run_ingest(series=series, discovery=discovery)

        self.assertIn("AMD", str(caught.exception))
        self.assertEqual(self.count(Company), 0)
        self.assertEqual(self.count(DailyPrice), 0)

    def test_a_discovery_with_no_tickers_writes_nothing(self):
        with self.assertRaises(IngestionIdentityError):
            self.run_ingest(discovery=make_discovery(tickers=()))

        self.assertEqual(self.count(Company), 0)

    def test_a_company_whose_ticker_changed_is_a_conflict_not_an_update(self):
        self.run_ingest()
        discovery = make_discovery(issuer_name="NVIDIA CORPORATION")

        with self.assertRaises(IngestionConflictError) as caught:
            self.run_ingest(discovery=discovery)

        self.assertIn("name", str(caught.exception))


class PersistenceFailureTests(unittest.TestCase):
    """A failure part-way through must leave nothing behind. Separate engine session."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def test_a_failure_part_way_through_leaves_no_rows(self):
        # `shares > 0` is a CHECK constraint, so this row cannot be written. It is not
        # something the parser would produce -- it is standing in for any persistence
        # failure that happens after earlier rows have already gone in.
        impossible = make_transaction(row_position=1, shares=Decimal("0"))
        good = make_record()
        broken = make_record(
            accession="0001199039-26-000014",
            filing=make_ref(accession="0001199039-26-000014"),
            document=make_document(transactions=(impossible,)),
        )

        with self.engine.connect() as connection:
            transaction = connection.begin()
            session = Session(bind=connection)
            try:
                with self.assertRaises(Exception):
                    ingest(
                        session,
                        series=make_series(),
                        discovery=make_discovery(),
                        records=(good, broken),
                        parameters={},
                        started_at=STARTED_AT,
                    )
            finally:
                session.close()
                transaction.rollback()

        # A fresh connection, so this reads committed state rather than the failed
        # transaction's view of it.
        with self.engine.connect() as connection:
            for model in (
                Company,
                DailyPrice,
                SecFiling,
                InsiderReportingOwner,
                InsiderTransaction,
                IngestionRun,
            ):
                self.assertEqual(
                    connection.scalar(select(func.count()).select_from(model)),
                    0,
                    f"{model.__tablename__} should have nothing in it",
                )


class ConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def test_a_second_run_cannot_take_the_lock_while_the_first_holds_it(self):
        """Proved with `pg_try_advisory_xact_lock` rather than by racing two threads."""
        lock_key = f"alphadesk:ingest:{NVDA_CIK}"

        with self.engine.connect() as holder_connection:
            holder_transaction = holder_connection.begin()
            holder_session = Session(bind=holder_connection)
            try:
                ingest(
                    holder_session,
                    series=make_series(),
                    discovery=make_discovery(),
                    records=(make_record(),),
                    parameters={},
                    started_at=STARTED_AT,
                )
                # The first ingestion is written but not committed, so it still holds
                # the lock -- exactly the window a concurrent run must not enter.
                with self.engine.connect() as other_connection:
                    acquired = other_connection.scalar(
                        text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                        {"key": lock_key},
                    )
                    self.assertFalse(
                        acquired,
                        "a second run took the lock while the first still held it",
                    )
            finally:
                holder_session.close()
                holder_transaction.rollback()

        # Once the first transaction ends, the lock is released.
        with self.engine.connect() as connection:
            acquired = connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {"key": lock_key},
            )
            self.assertTrue(acquired, "the lock should have been released")

    def test_different_companies_do_not_block_each_other(self):
        with self.engine.connect() as connection:
            transaction = connection.begin()
            session = Session(bind=connection)
            try:
                ingest(
                    session,
                    series=make_series(),
                    discovery=make_discovery(),
                    records=(make_record(),),
                    parameters={},
                    started_at=STARTED_AT,
                )
                with self.engine.connect() as other:
                    acquired = other.scalar(
                        text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                        {"key": "alphadesk:ingest:0000320193"},
                    )
                    self.assertTrue(acquired, "another company should not be blocked")
            finally:
                session.close()
                transaction.rollback()


class DryRunTests(IngestionTestCase):
    def test_a_dry_run_writes_nothing(self):
        summary = self.run_ingest(dry_run=True)

        for model in (
            Company,
            DailyPrice,
            SecFiling,
            InsiderReportingOwner,
            InsiderTransaction,
            IngestionRun,
        ):
            self.assertEqual(self.count(model), 0, f"{model.__tablename__} was written to")

        self.assertTrue(summary.dry_run)
        self.assertIsNone(summary.company_id)
        self.assertEqual(summary.counts["daily_prices"].inserted, 1)

    def test_a_dry_run_still_reports_the_scope_and_counts(self):
        summary = self.run_ingest(dry_run=True)

        self.assertEqual(summary.returned_bars, 1)
        self.assertEqual(summary.returned_filings, 1)
        self.assertEqual(summary.filing_accessions, ("0002152188-26-000005",))
        self.assertEqual(summary.counts["ingestion_runs"].inserted, 0)

    def test_a_dry_run_over_existing_data_reports_unchanged(self):
        self.run_ingest()
        summary = self.run_ingest(dry_run=True)

        self.assertEqual(summary.counts["daily_prices"].inserted, 0)
        self.assertEqual(summary.counts["daily_prices"].unchanged, 1)
        self.assertEqual(self.count(DailyPrice), 1)

    def test_a_dry_run_still_raises_a_conflict(self):
        self.run_ingest()
        changed = make_series(bars=[make_bar(close="219.99")])

        with self.assertRaises(IngestionConflictError):
            self.run_ingest(series=changed, dry_run=True)

        self.session.rollback()
        self.assertEqual(self.count(DailyPrice), 0)


class EmptyFilingTests(IngestionTestCase):
    def test_prices_are_stored_even_when_no_filings_come_back(self):
        """A successful SEC request with no results is not a failure."""
        summary = self.run_ingest(discovery=make_discovery(filings=()), records=())

        self.assertEqual(self.count(DailyPrice), 1)
        self.assertEqual(self.count(Company), 1)
        self.assertEqual(self.count(SecFiling), 0)
        self.assertEqual(summary.returned_filings, 0)
        self.assertTrue(
            any("not evidence of no insider activity" in w for w in summary.warnings),
            summary.warnings,
        )


if __name__ == "__main__":
    unittest.main()