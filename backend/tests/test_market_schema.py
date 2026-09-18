"""Constraint behaviour for the Module 1 tables.

What is checked here is mostly that the *database* refuses bad data. That matters more
than it first looks: a constraint is the only thing standing between a buggy ingestion
script and a number that is wrong but entirely plausible. The arithmetic tests
elsewhere in this directory cannot catch a duplicated row, and by the time a divergence
figure is on screen nothing downstream can tell that it counted one transaction twice.

Run against an isolated test database -- see tests/testdb.py -- never the application
database. Every insert happens inside a transaction that is rolled back, so no test
leaves a row behind.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.models import (
    Company,
    DailyPrice,
    Holding,
    InsiderReportingOwner,
    InsiderTransaction,
    SecFiling,
)
from tests.testdb import rolled_back, test_engine

_COMPANY = {
    "name": "NVIDIA Corporation",
    "ticker": "NVDA",
    "exchange": "NASDAQ",
    "currency": "USD",
    # Leading zeros, as EDGAR reports it.
    "sec_issuer_cik": "0001045810",
}

_PRICE = {
    "trading_date": date(2024, 1, 15),
    "open": Decimal("100.00"),
    "high": Decimal("110.00"),
    "low": Decimal("95.00"),
    "close": Decimal("105.00"),
    "volume": 1_000_000,
    "provider": "twelve_data",
    "currency": "USD",
    "adjustment_basis": "raw",
    # The provider's own mode. Part of the unique key since 0003, so two modes for one
    # day cannot collide.
    "provider_adjust_mode": "splits",
    # Left NULL: the provider states nothing about volume adjustment, and "not stated" is
    # a different fact from "not adjusted".
    "volume_adjustment": None,
}

_FILING = {
    "accession_number": "0001045810-24-000001",
    "form_type": "4",
    "filing_date": date(2024, 1, 17),
    "source_document_url": (
        "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000001/"
        "0001045810-24-000001-index.htm"
    ),
    # Columns added by 0003, all supplied by the parser in real use.
    "document_type": "4",
    "holding_rows_skipped": 0,
    "footnotes": {},
    "source_xml": "<ownershipDocument/>",
    "source_xml_sha256": "0" * 64,
}

_TRANSACTION = {
    "source_table": "nonDerivativeTable",
    "row_position": 0,
    "is_derivative": False,
    "transaction_date": date(2024, 1, 15),
    "security_title": "Common Stock",
    "transaction_code": "P",
    "acquired_disposed": "A",
    "shares": Decimal("1000.00"),
    "price_per_share": Decimal("100.00"),
    "ownership_direct_indirect": "D",
    # Field-level footnote references. Empty is the common case.
    "footnote_refs": [],
}

_OWNER = {
    "reporting_owner_cik": "0001234567",
    "owner_name": "DOE JANE",
}


def insert_company(connection: Connection, **overrides) -> int:
    return connection.execute(
        insert(Company).values(**{**_COMPANY, **overrides}).returning(Company.id)
    ).scalar_one()


def insert_price(connection: Connection, company_id: int, **overrides) -> int:
    return connection.execute(
        insert(DailyPrice)
        .values(**{**_PRICE, "company_id": company_id, **overrides})
        .returning(DailyPrice.id)
    ).scalar_one()


def insert_filing(connection: Connection, company_id: int, **overrides) -> int:
    return connection.execute(
        insert(SecFiling)
        .values(**{**_FILING, "company_id": company_id, **overrides})
        .returning(SecFiling.id)
    ).scalar_one()


def insert_transaction(connection: Connection, filing_id: int, **overrides) -> int:
    return connection.execute(
        insert(InsiderTransaction)
        .values(**{**_TRANSACTION, "filing_id": filing_id, **overrides})
        .returning(InsiderTransaction.id)
    ).scalar_one()


def insert_owner(connection: Connection, filing_id: int, **overrides) -> int:
    return connection.execute(
        insert(InsiderReportingOwner)
        .values(**{**_OWNER, "filing_id": filing_id, **overrides})
        .returning(InsiderReportingOwner.id)
    ).scalar_one()


class SchemaTestCase(unittest.TestCase):
    """Shared setup. Holds no tests of its own."""

    engine: Engine

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def assertRejected(self, connection: Connection, statement, message: str = "") -> None:
        """Assert the database refuses `statement`.

        Wrapped in a SAVEPOINT because a failed statement aborts the entire
        transaction in PostgreSQL. Without one, any query after an expected rejection
        would fail with "current transaction is aborted" -- and the test would look
        like it was checking something it never reached.
        """
        savepoint = connection.begin_nested()
        try:
            with self.assertRaises(IntegrityError, msg=message or None):
                connection.execute(statement)
        finally:
            savepoint.rollback()


class CompaniesTest(SchemaTestCase):
    def test_a_company_needs_no_portfolio_or_holding(self):
        """Company records exist independently of what anyone holds."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)

            self.assertEqual(
                connection.scalar(select(func.count()).select_from(Company)), 1
            )
            self.assertEqual(
                connection.scalar(select(func.count()).select_from(Holding)), 0
            )
            self.assertIsNotNone(company_id)

    def test_holdings_are_not_linked_to_companies(self):
        """A holding is a position; a company is a company. No foreign key joins them."""
        self.assertEqual(
            {fk.target_fullname for fk in Holding.__table__.foreign_keys},
            {"portfolios.id"},
        )
        self.assertEqual(set(Company.__table__.foreign_keys), set())

    def test_duplicate_ticker_is_rejected(self):
        with rolled_back(self.engine) as connection:
            insert_company(connection)
            self.assertRejected(
                connection, insert(Company).values(**{**_COMPANY, "sec_issuer_cik": "0000320193"})
            )

    def test_duplicate_cik_is_rejected(self):
        with rolled_back(self.engine) as connection:
            insert_company(connection)
            self.assertRejected(
                connection, insert(Company).values(**{**_COMPANY, "ticker": "AAPL"})
            )

    def test_non_numeric_cik_is_rejected(self):
        with rolled_back(self.engine) as connection:
            self.assertRejected(
                connection, insert(Company).values(**{**_COMPANY, "sec_issuer_cik": "ABC123"})
            )

    def test_cik_keeps_its_leading_zeros(self):
        """The whole reason the CIK is TEXT. An integer column would store 1045810."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            self.assertEqual(
                connection.scalar(
                    select(Company.sec_issuer_cik).where(Company.id == company_id)
                ),
                "0001045810",
            )


class DailyPriceConstraintsTest(SchemaTestCase):
    def test_duplicate_bar_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            insert_price(connection, company_id)
            self.assertRejected(connection, insert(DailyPrice).values(**{**_PRICE, "company_id": company_id}))

    def test_the_same_day_at_a_different_basis_is_allowed(self):
        """Raw and adjusted are different numbers for the same day, not a duplicate."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            insert_price(connection, company_id, adjustment_basis="raw")
            insert_price(connection, company_id, adjustment_basis="adjusted")

            self.assertEqual(self._count(connection, company_id), 2)

    def test_the_same_day_from_a_different_provider_is_allowed(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            insert_price(connection, company_id, provider="twelve_data")
            insert_price(connection, company_id, provider="another_provider")

            self.assertEqual(self._count(connection, company_id), 2)

    def test_an_unknown_adjustment_basis_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            self.assertRejected(
                connection,
                insert(DailyPrice).values(
                    **{**_PRICE, "company_id": company_id, "adjustment_basis": "split_adjusted"}
                ),
            )

    def test_high_below_low_is_rejected(self):
        """Asserted on the row, not on which check fired.

        An inverted range violates several checks at once and PostgreSQL reports
        whichever it reaches first, so pinning the constraint name here would be
        asserting an implementation detail of the query planner.
        """
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            self.assertRejected(
                connection,
                insert(DailyPrice).values(
                    **{**_PRICE, "company_id": company_id, "high": Decimal("90.00")}
                ),
            )

    def test_a_bar_the_range_checks_alone_would_miss_is_still_rejected(self):
        """`high >= low` holds, but open and close sit outside the range."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            insert_price(connection, company_id)

            self.assertRejected(
                connection,
                insert(DailyPrice).values(
                    **{
                        **_PRICE,
                        "company_id": company_id,
                        "trading_date": date(2024, 1, 16),
                        "low": Decimal("100.00"),
                        "high": Decimal("100.00"),
                        "open": Decimal("99.00"),
                    }
                ),
            )

    def test_open_outside_the_low_high_range_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            self.assertRejected(
                connection,
                insert(DailyPrice).values(
                    **{**_PRICE, "company_id": company_id, "open": Decimal("94.99")}
                ),
            )

    def test_close_outside_the_low_high_range_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            self.assertRejected(
                connection,
                insert(DailyPrice).values(
                    **{**_PRICE, "company_id": company_id, "close": Decimal("110.01")}
                ),
            )

    def test_a_non_positive_price_is_rejected(self):
        """A zero low forces a zero price somewhere in the bar. Real equities are above zero."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            self.assertRejected(
                connection,
                insert(DailyPrice).values(
                    **{
                        **_PRICE,
                        "company_id": company_id,
                        "open": Decimal("0"),
                        "high": Decimal("0"),
                        "low": Decimal("0"),
                        "close": Decimal("0"),
                    }
                ),
            )

    def test_negative_volume_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            self.assertRejected(
                connection,
                insert(DailyPrice).values(
                    **{**_PRICE, "company_id": company_id, "volume": -1}
                ),
            )

    def test_zero_volume_is_accepted(self):
        """A halt, or a session with no trades. Zero is a reading, not an error."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            insert_price(connection, company_id, volume=0)
            self.assertEqual(self._count(connection, company_id), 1)

    def test_a_bar_for_an_unknown_company_is_rejected(self):
        with rolled_back(self.engine) as connection:
            self.assertRejected(
                connection, insert(DailyPrice).values(**{**_PRICE, "company_id": 999_999})
            )

    @staticmethod
    def _count(connection: Connection, company_id: int) -> int:
        return connection.scalar(
            select(func.count())
            .select_from(DailyPrice)
            .where(DailyPrice.company_id == company_id)
        )


class SecFilingConstraintsTest(SchemaTestCase):
    def test_duplicate_accession_number_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            insert_filing(connection, company_id)
            self.assertRejected(
                connection, insert(SecFiling).values(**{**_FILING, "company_id": company_id})
            )

    def test_a_filing_for_an_unknown_company_is_rejected(self):
        with rolled_back(self.engine) as connection:
            self.assertRejected(
                connection, insert(SecFiling).values(**{**_FILING, "company_id": 999_999})
            )

    def test_a_filing_cannot_amend_itself(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertRejected(
                connection,
                update(SecFiling)
                .where(SecFiling.id == filing_id)
                .values(amends_filing_id=SecFiling.id),
            )

    def test_an_amendment_may_point_at_an_original(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            original_id = insert_filing(connection, company_id)
            amendment_id = insert_filing(
                connection,
                company_id,
                accession_number="0001045810-24-000002",
                form_type="4/A",
                is_amendment=True,
                amends_filing_id=original_id,
            )

            self.assertEqual(
                connection.scalar(
                    select(SecFiling.amends_filing_id).where(
                        SecFiling.id == amendment_id
                    )
                ),
                original_id,
            )

    def test_the_three_times_are_stored_separately(self):
        """Filing date, EDGAR acceptance, and our retrieval are three different facts."""
        accepted = datetime(2024, 1, 17, 16, 30, 45, tzinfo=timezone.utc)

        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(
                connection, company_id, acceptance_datetime=accepted
            )

            row = connection.execute(
                select(
                    SecFiling.filing_date,
                    SecFiling.acceptance_datetime,
                    SecFiling.retrieved_at,
                ).where(SecFiling.id == filing_id)
            ).one()

            self.assertEqual(row.filing_date, date(2024, 1, 17))
            self.assertEqual(row.acceptance_datetime, accepted)
            # Server-defaulted, so ingestion never has to supply it.
            self.assertIsNotNone(row.retrieved_at)

    def test_acceptance_datetime_may_be_absent(self):
        """Not every source supplies it. Absent means absent, not filled in."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertIsNone(
                connection.scalar(
                    select(SecFiling.acceptance_datetime).where(SecFiling.id == filing_id)
                )
            )


class InsiderTransactionConstraintsTest(SchemaTestCase):
    def test_duplicate_position_within_a_source_table_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)
            insert_transaction(connection, filing_id)

            self.assertRejected(
                connection,
                insert(InsiderTransaction).values(
                    **{**_TRANSACTION, "filing_id": filing_id}
                ),
                "the same row of the same table of the same filing is the same row",
            )

    def test_the_same_position_in_the_other_table_is_allowed(self):
        """The non-derivative and derivative tables both start at position 0."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)
            insert_transaction(connection, filing_id)

            insert_transaction(
                connection,
                filing_id,
                source_table="derivativeTable",
                is_derivative=True,
                security_title="Employee Stock Option (Right to Buy)",
            )

            self.assertEqual(
                connection.scalar(
                    select(func.count())
                    .select_from(InsiderTransaction)
                    .where(InsiderTransaction.filing_id == filing_id)
                ),
                2,
            )

    def test_identical_amounts_on_the_same_date_are_two_separate_rows(self):
        """Identity is provenance. Two real transactions can share a date and an amount."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)
            insert_transaction(connection, filing_id, row_position=0)
            insert_transaction(connection, filing_id, row_position=1)

            self.assertEqual(
                connection.scalar(
                    select(func.count())
                    .select_from(InsiderTransaction)
                    .where(InsiderTransaction.filing_id == filing_id)
                ),
                2,
            )

    def test_the_derivative_flag_must_match_the_source_table(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertRejected(
                connection,
                insert(InsiderTransaction).values(
                    **{
                        **_TRANSACTION,
                        "filing_id": filing_id,
                        "source_table": "derivativeTable",
                        "is_derivative": False,
                    }
                ),
            )

    def test_an_unknown_source_table_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertRejected(
                connection,
                insert(InsiderTransaction).values(
                    **{**_TRANSACTION, "filing_id": filing_id, "source_table": "footnoteTable"}
                ),
            )

    def test_a_transaction_for_an_unknown_filing_is_rejected(self):
        with rolled_back(self.engine) as connection:
            self.assertRejected(
                connection,
                insert(InsiderTransaction).values(**{**_TRANSACTION, "filing_id": 999_999}),
            )

    def test_zero_shares_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertRejected(
                connection,
                insert(InsiderTransaction).values(
                    **{**_TRANSACTION, "filing_id": filing_id, "shares": Decimal("0")}
                ),
            )

    def test_a_missing_price_stays_missing(self):
        """Null and zero are different facts, and a default of 0 would erase the difference."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)
            transaction_id = insert_transaction(
                connection, filing_id, price_per_share=None
            )

            self.assertIsNone(
                connection.scalar(
                    select(InsiderTransaction.price_per_share).where(
                        InsiderTransaction.id == transaction_id
                    )
                )
            )

    def test_a_reported_zero_price_is_accepted(self):
        """A gift or a grant is reported at 0. Refusing it would discard real filings."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)
            transaction_id = insert_transaction(
                connection, filing_id, transaction_code="G", price_per_share=Decimal("0")
            )

            self.assertEqual(
                connection.scalar(
                    select(InsiderTransaction.price_per_share).where(
                        InsiderTransaction.id == transaction_id
                    )
                ),
                Decimal("0.000000"),
            )

    def test_a_negative_price_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertRejected(
                connection,
                insert(InsiderTransaction).values(
                    **{
                        **_TRANSACTION,
                        "filing_id": filing_id,
                        "price_per_share": Decimal("-1"),
                    }
                ),
            )

    def test_a_code_edgar_has_not_used_yet_is_still_storable(self):
        """The code column is deliberately open. A closed list would reject real filings."""
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)
            insert_transaction(connection, filing_id, transaction_code="ZZ")

            self.assertEqual(
                connection.scalar(
                    select(InsiderTransaction.transaction_code)
                    .where(InsiderTransaction.filing_id == filing_id)
                ),
                "ZZ",
            )

    def test_an_invalid_acquired_disposed_code_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertRejected(
                connection,
                insert(InsiderTransaction).values(
                    **{**_TRANSACTION, "filing_id": filing_id, "acquired_disposed": "X"}
                ),
            )

    def test_an_invalid_ownership_code_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertRejected(
                connection,
                insert(InsiderTransaction).values(
                    **{
                        **_TRANSACTION,
                        "filing_id": filing_id,
                        "ownership_direct_indirect": "X",
                    }
                ),
            )

    def test_a_negative_row_position_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertRejected(
                connection,
                insert(InsiderTransaction).values(
                    **{**_TRANSACTION, "filing_id": filing_id, "row_position": -1}
                ),
            )


class ReportingOwnerConstraintsTest(SchemaTestCase):
    def test_the_owner_cik_keeps_its_leading_zeros(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)
            owner_id = insert_owner(connection, filing_id)

            self.assertEqual(
                connection.scalar(
                    select(InsiderReportingOwner.reporting_owner_cik).where(
                        InsiderReportingOwner.id == owner_id
                    )
                ),
                "0001234567",
            )

    def test_a_non_numeric_owner_cik_is_rejected(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            self.assertRejected(
                connection,
                insert(InsiderReportingOwner).values(
                    **{**_OWNER, "filing_id": filing_id, "reporting_owner_cik": "DOE"}
                ),
            )

    def test_the_same_owner_cannot_appear_twice_on_one_filing(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)
            insert_owner(connection, filing_id)

            self.assertRejected(
                connection,
                insert(InsiderReportingOwner).values(
                    **{**_OWNER, "filing_id": filing_id}
                ),
            )

    def test_the_same_owner_may_appear_on_two_filings(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            first = insert_filing(connection, company_id)
            second = insert_filing(
                connection, company_id, accession_number="0001045810-24-000002"
            )
            insert_owner(connection, first)
            insert_owner(connection, second)

            self.assertEqual(
                connection.scalar(select(func.count()).select_from(InsiderReportingOwner)),
                2,
            )

    def test_an_owner_for_an_unknown_filing_is_rejected(self):
        with rolled_back(self.engine) as connection:
            self.assertRejected(
                connection,
                insert(InsiderReportingOwner).values(**{**_OWNER, "filing_id": 999_999}),
            )


class JointFilingTest(SchemaTestCase):
    """A filing with two reporting owners must not duplicate its transactions.

    This is the shape the owner table exists to support, and the fan-out it exposes.
    The last assertion is a deliberate demonstration of the hazard rather than a wish:
    once step 5 starts summing shares, the join below is the mistake it will make.
    """

    def test_two_owners_do_not_duplicate_the_transaction(self):
        with rolled_back(self.engine) as connection:
            company_id = insert_company(connection)
            filing_id = insert_filing(connection, company_id)

            insert_owner(
                connection,
                filing_id,
                reporting_owner_cik="0001234567",
                owner_name="DOE JANE",
                is_director=True,
            )
            insert_owner(
                connection,
                filing_id,
                reporting_owner_cik="0007654321",
                owner_name="SMITH JOHN",
                is_officer=True,
                officer_title="Chief Financial Officer",
            )
            insert_transaction(connection, filing_id, shares=Decimal("1000.00"))

            self.assertEqual(
                connection.scalar(
                    select(func.count())
                    .select_from(InsiderReportingOwner)
                    .where(InsiderReportingOwner.filing_id == filing_id)
                ),
                2,
            )
            self.assertEqual(
                connection.scalar(
                    select(func.count())
                    .select_from(InsiderTransaction)
                    .where(InsiderTransaction.filing_id == filing_id)
                ),
                1,
                "two owners must not mean two transaction rows",
            )
            self.assertEqual(
                connection.scalar(
                    select(func.sum(InsiderTransaction.shares)).where(
                        InsiderTransaction.filing_id == filing_id
                    )
                ),
                Decimal("1000.000000"),
            )

            # And here is the trap. Summing from the owner side counts the same
            # 1,000 shares twice, and nothing about the result would look wrong.
            fanned_out = connection.scalar(
                select(func.sum(InsiderTransaction.shares))
                .select_from(InsiderTransaction)
                .join(SecFiling, InsiderTransaction.filing_id == SecFiling.id)
                .join(
                    InsiderReportingOwner,
                    InsiderReportingOwner.filing_id == SecFiling.id,
                )
            )
            self.assertEqual(fanned_out, Decimal("2000.000000"))


if __name__ == "__main__":
    unittest.main()
