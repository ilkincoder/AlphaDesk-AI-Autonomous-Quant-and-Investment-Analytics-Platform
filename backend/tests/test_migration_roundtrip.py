"""Upgrade, downgrade, and upgrade again -- on a disposable database only.

A downgrade is the one migration operation that can destroy data, so it is tested
here against the isolated database that `tests/testdb.py` derives from `DATABASE_URL`
and refuses to use unless its name ends in `_test`. The development database holding
the demo portfolio is never a target of these commands.

What this proves is narrower than it looks, and worth stating plainly: it proves the
downgrade drops the five tables this milestone added and leaves `portfolios` and
`holdings` alone. It does not prove a downgrade would preserve *rows* in the new
tables, because dropping a table discards its contents by definition. There is nothing
in those tables yet to lose, and once there is, the answer is a backup, not a test.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest

from app.models import Base
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.testdb import alembic_revision, run_alembic, table_names, test_engine

HEAD_REVISION = "0011_news"
REVISION_0010 = "0010_alpaca_broker_link"
REVISION_0009 = "0009_conversations"
REVISION_0008 = "0008_form4_scope_rename"
REVISION_0007 = "0007_document_index_manifest"
REVISION_0004 = "0004_company_context"
REVISION_0003 = "0003_ingestion_storage"
REVISION_0002 = "0002_market_data_and_filings"
REVISION_0001 = "0001_portfolios_holdings"

# What each migration introduced, so a downgrade can be checked step by step rather than
# only all the way down. 0005 and 0006 add columns rather than tables, so they have no
# entry here.
TABLES_FROM_0002 = frozenset(
    {
        "companies",
        "daily_prices",
        "sec_filings",
        "insider_transactions",
        "insider_reporting_owners",
    }
)
TABLES_FROM_0003 = frozenset({"ingestion_runs"})
TABLES_FROM_0004 = frozenset(
    {"filing_documents", "company_fact_snapshots", "financial_facts"}
)
TABLES_FROM_0007 = frozenset({"document_index_manifest"})
TABLES_FROM_0009 = frozenset({"conversations", "conversation_turns"})
TABLES_FROM_0011 = frozenset({"news_articles", "news_ingestion_runs"})

# Every table any of our migrations creates, for the checks that only care that they are
# all there or all gone.
ALL_INTRODUCED = (
    TABLES_FROM_0002
    | TABLES_FROM_0003
    | TABLES_FROM_0004
    | TABLES_FROM_0007
    | TABLES_FROM_0009
    | TABLES_FROM_0011
)

# Untouched by any of them, and the reason every downgrade has to be selective.
EXISTING_TABLES = frozenset({"portfolios", "holdings"})

# What 0010 adds, and what its downgrade must therefore take away again.
BROKER_COLUMNS = frozenset(
    {"broker", "broker_account_id", "broker_equity", "last_synced_at"}
)
MARKET_COLUMNS = frozenset({"market_price", "market_value"})


class MigrationRoundtripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def setUp(self) -> None:
        # Every test here starts from head, whatever the previous one did.
        run_alembic("upgrade", "head")

    def test_head_is_the_news_revision(self):
        self.assertEqual(alembic_revision(self.engine), HEAD_REVISION)

    def test_downgrading_0011_removes_only_the_news_tables(self):
        """0011 adds two tables and alters nothing, so its downgrade should be exactly the
        two tables going and everything else staying."""
        run_alembic("downgrade", REVISION_0010)
        try:
            remaining = table_names(self.engine)

            self.assertEqual(remaining & TABLES_FROM_0011, set())
            self.assertLessEqual(EXISTING_TABLES, remaining)
            self.assertLessEqual(
                TABLES_FROM_0009,
                remaining,
                "0011's downgrade reached tables that belong to earlier migrations",
            )
            # 0010 is still applied, so its columns are still there.
            with self.engine.connect() as connection:
                columns = self._columns(connection, "portfolios")
            self.assertLessEqual(BROKER_COLUMNS, columns)
            self.assertEqual(alembic_revision(self.engine), REVISION_0010)
        finally:
            run_alembic("upgrade", "head")
            self.engine.dispose()

        self.assertLessEqual(TABLES_FROM_0011, table_names(self.engine))

    def test_downgrading_0010_replaces_the_cash_check_rather_than_removing_it(self):
        """0010 adds no table and touches no row, so what is checked is the constraint it
        swaps and the columns it adds. It is the one migration here whose constraint
        change is a *replacement*: the original rule survives, scoped to the rows it still
        applies to."""
        run_alembic("downgrade", REVISION_0009)
        try:
            with self.engine.connect() as connection:
                columns = self._columns(connection, "portfolios")
                holdings_columns = self._columns(connection, "holdings")
                constraints = self._constraints(connection, "portfolios")

            self.assertEqual(columns & BROKER_COLUMNS, set())
            self.assertEqual(holdings_columns & MARKET_COLUMNS, set())
            self.assertIn("ck_portfolios_cash_balance_nonnegative", constraints)
            self.assertNotIn("ck_portfolios_unlinked_cash_balance_nonnegative", constraints)
            self.assertEqual(alembic_revision(self.engine), REVISION_0009)
        finally:
            run_alembic("upgrade", "head")
            self.engine.dispose()

        with self.engine.connect() as connection:
            columns = self._columns(connection, "portfolios")
            constraints = self._constraints(connection, "portfolios")

        self.assertLessEqual(BROKER_COLUMNS, columns)
        self.assertNotIn("ck_portfolios_cash_balance_nonnegative", constraints)
        self.assertIn("ck_portfolios_unlinked_cash_balance_nonnegative", constraints)

    def test_the_cash_check_0010_leaves_behind_still_applies_to_an_unlinked_row(self):
        """The constraint is not decoration: the row it still governs has to be refused.

        A negative balance is legitimate once a broker reports it and a bug before that,
        and this is what tells the two apart in the database rather than in a comment.
        """
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                with self.assertRaises(IntegrityError):
                    connection.execute(
                        text(
                            "INSERT INTO portfolios (name, cash_balance, currency) "
                            "VALUES ('probe-unlinked', -5.00, 'USD')"
                        )
                    )
            finally:
                transaction.rollback()

    @staticmethod
    def _columns(connection, table: str) -> set[str]:
        return set(
            connection.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :table"
                ).bindparams(table=table)
            )
        )

    @staticmethod
    def _constraints(connection, table: str) -> set[str]:
        return set(
            connection.scalars(
                text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE table_name = :table"
                ).bindparams(table=table)
            )
        )

    def test_every_introduced_table_exists_at_head(self):
        self.assertLessEqual(ALL_INTRODUCED, table_names(self.engine))

    def test_downgrading_0008_puts_the_old_scope_value_back(self):
        """The one migration here that changes rows rather than shape, so it is checked."""
        run_alembic("downgrade", REVISION_0007)
        try:
            with self.engine.connect() as connection:
                scopes = set(
                    connection.scalars(text("SELECT DISTINCT scope FROM ingestion_runs"))
                )
                default = connection.scalar(
                    text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_name = 'ingestion_runs' AND column_name = 'scope'"
                    )
                )
            self.assertNotIn("form4", scopes)
            self.assertIn("nvda_form4", default)
        finally:
            run_alembic("upgrade", "head")
            self.engine.dispose()

        with self.engine.connect() as connection:
            self.assertNotIn(
                "nvda_form4",
                set(connection.scalars(text("SELECT DISTINCT scope FROM ingestion_runs"))),
            )

    def test_downgrading_0007_removes_only_what_it_added(self):
        """The manifest goes; the collection does not, because Qdrant is not ours to drop."""
        run_alembic("downgrade", REVISION_0004)
        try:
            remaining = table_names(self.engine)

            self.assertEqual(remaining & TABLES_FROM_0007, set())
            self.assertLessEqual(
                TABLES_FROM_0002 | TABLES_FROM_0003 | TABLES_FROM_0004, remaining
            )
            self.assertLessEqual(EXISTING_TABLES, remaining)
            self.assertEqual(alembic_revision(self.engine), REVISION_0004)
        finally:
            run_alembic("upgrade", "head")
            self.engine.dispose()

        self.assertLessEqual(TABLES_FROM_0007, table_names(self.engine))

    def test_downgrading_0004_removes_only_what_it_added(self):
        """0004 adds three tables and 0005/0006 alter columns; only tables disappear."""
        run_alembic("downgrade", REVISION_0003)
        try:
            remaining = table_names(self.engine)

            self.assertEqual(remaining & TABLES_FROM_0004, set())
            self.assertLessEqual(
                TABLES_FROM_0002 | TABLES_FROM_0003,
                remaining,
                "0004's downgrade reached tables that belong to earlier migrations",
            )
            self.assertLessEqual(EXISTING_TABLES, remaining)
            self.assertEqual(alembic_revision(self.engine), REVISION_0003)
        finally:
            run_alembic("upgrade", "head")
            self.engine.dispose()

        self.assertLessEqual(TABLES_FROM_0004, table_names(self.engine))

    def test_downgrading_0003_removes_only_what_it_added(self):
        """0003 adds a table and alters others; only the table should disappear."""
        run_alembic("downgrade", REVISION_0002)
        try:
            remaining = table_names(self.engine)

            self.assertEqual(remaining & TABLES_FROM_0003, set())
            self.assertLessEqual(
                TABLES_FROM_0002,
                remaining,
                "0003's downgrade reached tables that belong to 0002",
            )
            self.assertLessEqual(EXISTING_TABLES, remaining)
            self.assertEqual(alembic_revision(self.engine), REVISION_0002)
        finally:
            # Restore head even if an assertion above failed, so one failure here
            # does not cascade into every later test in the suite.
            run_alembic("upgrade", "head")
            # Drop pooled connections opened against the pre-downgrade schema, so a
            # stale connection cannot serve the checks below.
            self.engine.dispose()

        self.assertLessEqual(TABLES_FROM_0003, table_names(self.engine))

    def test_downgrading_all_the_way_leaves_the_portfolio_tables_alone(self):
        run_alembic("downgrade", REVISION_0001)
        try:
            remaining = table_names(self.engine)

            self.assertEqual(remaining & ALL_INTRODUCED, set())
            self.assertLessEqual(
                EXISTING_TABLES,
                remaining,
                "the downgrade reached tables no migration of ours created",
            )
            self.assertEqual(alembic_revision(self.engine), REVISION_0001)
        finally:
            run_alembic("upgrade", "head")
            self.engine.dispose()

        self.assertLessEqual(ALL_INTRODUCED, table_names(self.engine))

    def test_the_models_and_the_migration_describe_the_same_tables(self):
        """Guards the gap `create_all` would have left open.

        The tests build their schema by running the migration, and the application
        reads it through the models. If the two ever drifted, one of them would be
        wrong in a way nothing else here would catch.
        """
        self.assertEqual(
            set(Base.metadata.tables),
            table_names(self.engine) - {"alembic_version"},
        )


if __name__ == "__main__":
    unittest.main()
