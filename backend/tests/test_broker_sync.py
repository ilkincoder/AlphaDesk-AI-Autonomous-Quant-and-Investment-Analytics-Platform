"""Writing a broker snapshot into the portfolio, and everything that must not happen.

Against a real PostgreSQL (migrated, rolled back per test), with a snapshot built by hand
rather than fetched: `apply_snapshot` takes one, which is what lets every case here run
without a broker, a transport or a network.

The properties this file exists for, in the order they matter:

* a failed read leaves the previous snapshot **and its timestamp** exactly as they were;
* repeating a sync cannot duplicate a holding, and a position the broker no longer reports
  is removed -- an empty response means zero holdings, not "nothing to say";
* two syncs cannot interleave, and an older read cannot overwrite a newer one;
* the account this portfolio is bound to is never switched silently.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import broker_sync
from app.alpaca import BROKER, BrokerPosition, BrokerSnapshot, ProviderUnavailableError
from app.broker_sync import (
    AccountMismatchError,
    CurrencyMismatchError,
    PortfolioNotStoredError,
    SyncInProgressError,
    apply_snapshot,
    sync,
)
from app import seed as seed_module
from app.models import Holding, Portfolio
from app.portfolio_identity import BROKER_PORTFOLIO_NAME, DEMO_PORTFOLIO_NAME
from tests.testdb import test_engine

STARTED_AT = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)
ACCOUNT_ID = "8f3a2b10-4c5d-4e6f-8a9b-0c1d2e3f4a5b"
OTHER_ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"


def position(
    symbol: str,
    quantity: str,
    entry: str,
    current: str,
    market_value: str,
) -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol,
        quantity=Decimal(quantity),
        average_entry_price=Decimal(entry),
        current_price=Decimal(current),
        market_value=Decimal(market_value),
    )


def snapshot(
    *positions: BrokerPosition,
    started_at: datetime = STARTED_AT,
    account_id: str = ACCOUNT_ID,
    cash: str = "2500.25",
    equity: str = "12345.67",
    currency: str = "USD",
) -> BrokerSnapshot:
    return BrokerSnapshot(
        started_at=started_at,
        account_id=account_id,
        account_number="PA3TESTACCOUNT",
        currency=currency,
        cash=Decimal(cash),
        equity=Decimal(equity),
        positions=positions,
    )


# The three holdings the milestone's sanity check expects, plus the account figures.
AAPL = position("AAPL", "5", "180.10", "201.25", "1006.25")
MSFT = position("MSFT", "8", "400.00", "510.50", "4084.00")
NVDA = position("NVDA", "10", "120.00", "178.40", "1784.00")
SANITY_CHECK = snapshot(AAPL, MSFT, NVDA)


class SyncTestCase(unittest.TestCase):
    """A real database, and a session inside a transaction that is always rolled back."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = test_engine()

    def setUp(self) -> None:
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(bind=self.connection)
        self.addCleanup(self.session.close)
        self.addCleanup(self.transaction.rollback)
        self.addCleanup(self.connection.close)

    def add_portfolio(
        self,
        *,
        name: str | None = None,
        cash: str = "10000.00",
        currency: str = "USD",
        account_id: str | None = None,
    ) -> Portfolio:
        """An unlinked portfolio by default, named as the seed names it.

        A linked one is named as the sync names it, because that is the state the
        application can actually produce -- the rename and the link are one write.
        """
        linked = account_id is not None
        record = Portfolio(
            name=name
            or (BROKER_PORTFOLIO_NAME if linked else DEMO_PORTFOLIO_NAME),
            currency=currency,
            cash_balance=Decimal(cash),
            broker=BROKER if linked else None,
            broker_account_id=account_id,
            broker_equity=Decimal("0.00") if linked else None,
            last_synced_at=STARTED_AT - timedelta(hours=1) if linked else None,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def add_holding(
        self, portfolio: Portfolio, symbol: str, quantity: str, entry: str
    ) -> Holding:
        holding = Holding(
            portfolio_id=portfolio.id,
            symbol=symbol,
            quantity=Decimal(quantity),
            average_buy_price=Decimal(entry),
        )
        self.session.add(holding)
        self.session.flush()
        return holding

    def holdings(self, portfolio: Portfolio) -> dict[str, Holding]:
        self.session.expire_all()
        rows = self.session.scalars(
            select(Holding).where(Holding.portfolio_id == portfolio.id)
        )
        return {row.symbol: row for row in rows}


class FirstSyncTests(SyncTestCase):
    def test_a_first_sync_stores_every_figure_exactly(self):
        portfolio = self.add_portfolio()

        outcome = apply_snapshot(self.session, SANITY_CHECK)

        self.assertTrue(outcome.applied)
        self.assertEqual(outcome.position_count, 3)
        self.assertEqual(outcome.cash_balance, Decimal("2500.25"))
        self.assertEqual(outcome.equity, Decimal("12345.67"))

        stored = self.holdings(portfolio)
        self.assertEqual(sorted(stored), ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(stored["AAPL"].quantity, Decimal("5"))
        self.assertEqual(stored["AAPL"].average_buy_price, Decimal("180.1000"))
        self.assertEqual(stored["AAPL"].market_price, Decimal("201.25"))
        self.assertEqual(stored["AAPL"].market_value, Decimal("1006.25"))

        self.assertEqual(portfolio.broker, BROKER)
        self.assertEqual(portfolio.broker_account_id, ACCOUNT_ID)
        self.assertEqual(portfolio.cash_balance, Decimal("2500.25"))
        self.assertEqual(portfolio.broker_equity, Decimal("12345.67"))
        self.assertEqual(portfolio.last_synced_at, STARTED_AT)

    def test_the_first_sync_renames_the_row_now_that_it_is_an_account(self):
        """The name is a statement about what the row is, and this is the moment it stops
        being the placeholder portfolio. Renaming it earlier -- in a migration, or at
        seed time -- would label made-up figures as a real account's."""
        portfolio = self.add_portfolio()
        self.assertEqual(portfolio.name, DEMO_PORTFOLIO_NAME)

        apply_snapshot(self.session, SANITY_CHECK)

        self.assertEqual(portfolio.name, BROKER_PORTFOLIO_NAME)

    def test_a_later_sync_leaves_the_name_where_it_is(self):
        portfolio = self.add_portfolio()
        apply_snapshot(self.session, SANITY_CHECK)

        apply_snapshot(
            self.session,
            snapshot(AAPL, MSFT, NVDA, started_at=STARTED_AT + timedelta(seconds=30)),
        )

        self.assertEqual(portfolio.name, BROKER_PORTFOLIO_NAME)

    def test_a_refused_snapshot_does_not_rename_anything(self):
        """The name moves with the link, and a snapshot that wrote nothing did not link
        anything."""
        portfolio = self.add_portfolio()
        apply_snapshot(self.session, SANITY_CHECK)

        refused = snapshot(started_at=STARTED_AT - timedelta(minutes=5))
        outcome = apply_snapshot(self.session, refused)

        self.assertFalse(outcome.applied)
        self.assertEqual(portfolio.name, BROKER_PORTFOLIO_NAME)

    def test_a_refused_snapshot_on_an_unlinked_portfolio_leaves_the_demo_name(self):
        portfolio = self.add_portfolio()
        self.add_holding(portfolio, "NVDA", "10", "120.00")

        with self.assertRaises(CurrencyMismatchError):
            apply_snapshot(self.session, snapshot(currency="EUR"))

        self.assertEqual(portfolio.name, DEMO_PORTFOLIO_NAME)
        self.assertIsNone(portfolio.broker)

    def test_a_first_sync_replaces_the_placeholder_holdings_rather_than_adding_to_them(self):
        """The seeded portfolio starts with made-up positions. A snapshot is the whole
        truth about what is held, so a symbol it does not mention is not held."""
        portfolio = self.add_portfolio()
        self.add_holding(portfolio, "TSLA", "3", "250.00")

        apply_snapshot(self.session, SANITY_CHECK)

        self.assertEqual(sorted(self.holdings(portfolio)), ["AAPL", "MSFT", "NVDA"])

    def test_repeating_a_sync_does_not_duplicate_anything(self):
        portfolio = self.add_portfolio()

        apply_snapshot(self.session, SANITY_CHECK)
        second = apply_snapshot(self.session, snapshot(AAPL, MSFT, NVDA, started_at=STARTED_AT + timedelta(seconds=30)))

        self.assertTrue(second.applied)
        self.assertEqual(second.position_count, 3)
        self.assertEqual(sorted(self.holdings(portfolio)), ["AAPL", "MSFT", "NVDA"])

    def test_an_unchanged_repeat_updates_the_row_rather_than_replacing_it(self):
        """The row's identity survives a refresh. A later feature will hang history from
        it, and a delete-and-reinsert every thirty seconds would make that impossible."""
        portfolio = self.add_portfolio()

        apply_snapshot(self.session, SANITY_CHECK)
        first_id = self.holdings(portfolio)["AAPL"].id

        apply_snapshot(self.session, snapshot(AAPL, MSFT, NVDA, started_at=STARTED_AT + timedelta(seconds=30)))

        self.assertEqual(self.holdings(portfolio)["AAPL"].id, first_id)

    def test_a_changed_quantity_and_price_are_written_over_the_old_ones(self):
        portfolio = self.add_portfolio()
        apply_snapshot(self.session, SANITY_CHECK)

        moved = position("AAPL", "7", "190.00", "205.00", "1435.00")
        apply_snapshot(self.session, snapshot(moved, MSFT, NVDA, started_at=STARTED_AT + timedelta(seconds=30)))

        stored = self.holdings(portfolio)["AAPL"]
        self.assertEqual(stored.quantity, Decimal("7"))
        self.assertEqual(stored.average_buy_price, Decimal("190.0000"))
        self.assertEqual(stored.market_price, Decimal("205.00"))


class ClosedAndEmptyTests(SyncTestCase):
    def test_a_closed_position_is_removed(self):
        portfolio = self.add_portfolio()
        apply_snapshot(self.session, SANITY_CHECK)

        apply_snapshot(self.session, snapshot(AAPL, MSFT, started_at=STARTED_AT + timedelta(seconds=30)))

        self.assertEqual(sorted(self.holdings(portfolio)), ["AAPL", "MSFT"])

    def test_an_empty_response_means_zero_holdings_not_nothing_to_say(self):
        """The distinction the whole delete path rests on: `[]` is a complete answer that
        the account holds nothing, and it must empty the portfolio rather than leave the
        previous positions on screen."""
        portfolio = self.add_portfolio()
        apply_snapshot(self.session, SANITY_CHECK)

        outcome = apply_snapshot(
            self.session, snapshot(started_at=STARTED_AT + timedelta(seconds=30))
        )

        self.assertTrue(outcome.applied)
        self.assertEqual(outcome.position_count, 0)
        self.assertEqual(self.holdings(portfolio), {})
        # Cash and equity are still stored: an account with no positions still has both.
        self.assertEqual(portfolio.cash_balance, Decimal("2500.25"))

    def test_another_portfolios_holdings_are_never_touched(self):
        other = self.add_portfolio(name="Someone Else's Portfolio")
        other_holding = self.add_holding(other, "TSLA", "3", "250.00")
        portfolio = self.add_portfolio()

        apply_snapshot(self.session, SANITY_CHECK)
        apply_snapshot(self.session, snapshot(started_at=STARTED_AT + timedelta(seconds=30)))

        self.session.expire_all()
        self.assertIsNotNone(
            self.session.scalar(select(Holding).where(Holding.id == other_holding.id))
        )
        self.assertEqual(self.holdings(portfolio), {})


class RefusalTests(SyncTestCase):
    def test_an_unseeded_portfolio_is_reported_rather_than_created(self):
        with self.assertRaises(PortfolioNotStoredError) as caught:
            apply_snapshot(self.session, SANITY_CHECK)

        self.assertIn("app.seed", str(caught.exception))

    def test_a_different_account_is_refused_and_nothing_is_written(self):
        portfolio = self.add_portfolio(account_id=OTHER_ACCOUNT_ID)

        with self.assertRaises(AccountMismatchError) as caught:
            apply_snapshot(self.session, SANITY_CHECK)

        self.assertIn(OTHER_ACCOUNT_ID, str(caught.exception))
        self.assertIn(ACCOUNT_ID, str(caught.exception))
        self.assertEqual(portfolio.last_synced_at, STARTED_AT - timedelta(hours=1))
        self.assertEqual(self.holdings(portfolio), {})

    def test_the_same_account_is_not_a_mismatch(self):
        portfolio = self.add_portfolio(account_id=ACCOUNT_ID)

        outcome = apply_snapshot(self.session, SANITY_CHECK)

        self.assertTrue(outcome.applied)
        self.assertEqual(portfolio.broker_account_id, ACCOUNT_ID)

    def test_a_currency_the_portfolio_does_not_use_is_refused(self):
        portfolio = self.add_portfolio()

        with self.assertRaises(CurrencyMismatchError) as caught:
            apply_snapshot(self.session, snapshot(currency="EUR"))

        self.assertIn("USD", str(caught.exception))
        self.assertIsNone(portfolio.broker)


class NegativeCashTests(SyncTestCase):
    """The cash constraint is scoped to unlinked portfolios, and both halves of that are
    checked here rather than only the half the sync happens to exercise."""

    def test_the_brokers_negative_balance_is_stored_exactly_as_reported(self):
        portfolio = self.add_portfolio()

        outcome = apply_snapshot(self.session, snapshot(cash="-1250.75", equity="500.00"))

        self.assertTrue(outcome.applied)
        self.assertEqual(portfolio.cash_balance, Decimal("-1250.75"))
        self.assertEqual(outcome.cash_balance, Decimal("-1250.75"))

    def test_an_already_linked_portfolio_can_be_synced_to_a_negative_balance(self):
        portfolio = self.add_portfolio(account_id=ACCOUNT_ID)
        apply_snapshot(self.session, SANITY_CHECK)

        apply_snapshot(
            self.session,
            snapshot(
                AAPL,
                MSFT,
                NVDA,
                cash="-99.50",
                started_at=STARTED_AT + timedelta(seconds=30),
            ),
        )

        self.assertEqual(portfolio.cash_balance, Decimal("-99.50"))

    def test_an_unlinked_portfolio_still_cannot_hold_a_negative_balance(self):
        """The original constraint, kept where it still means something: this figure is
        one the application chose, and a negative one here is a bug, not a fact."""
        portfolio = self.add_portfolio()

        portfolio.cash_balance = Decimal("-1.00")
        with self.assertRaises(IntegrityError):
            self.session.flush()

        self.session.rollback()


class OrderingTests(SyncTestCase):
    def test_a_snapshot_older_than_the_stored_one_is_refused(self):
        """Two syncs can finish out of order. The slow one started first, so its figures
        are the older ones, and letting it write would move the portfolio backwards."""
        portfolio = self.add_portfolio()
        apply_snapshot(self.session, SANITY_CHECK)

        stale = snapshot(
            position("AAPL", "99", "1.00", "2.00", "198.00"),
            started_at=STARTED_AT - timedelta(minutes=5),
            cash="1.00",
        )
        outcome = apply_snapshot(self.session, stale)

        self.assertFalse(outcome.applied)
        # The figures reported are the *stored* ones: a refused snapshot's numbers would
        # describe a state the database is not in.
        self.assertEqual(outcome.cash_balance, Decimal("2500.25"))
        self.assertEqual(outcome.equity, Decimal("12345.67"))
        self.assertEqual(outcome.position_count, 3)
        self.assertEqual(sorted(self.holdings(portfolio)), ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(portfolio.last_synced_at, STARTED_AT)
        self.assertEqual(portfolio.cash_balance, Decimal("2500.25"))

    def test_a_snapshot_from_the_same_instant_is_accepted(self):
        """The same moment read twice is not older data, and refusing it would make a
        retry a no-op for no reason."""
        portfolio = self.add_portfolio()
        apply_snapshot(self.session, SANITY_CHECK)

        outcome = apply_snapshot(self.session, SANITY_CHECK)

        self.assertTrue(outcome.applied)

    def test_an_overlapping_sync_is_refused_rather_than_queued(self):
        """Held here is exactly the state a second concurrent call finds. The refusal
        happens before the fetch, which is what the assertion on the broker proves."""
        self.add_portfolio()
        self.assertTrue(broker_sync._SYNC_LOCK.acquire(blocking=False))
        self.addCleanup(broker_sync._SYNC_LOCK.release)

        with mock.patch.object(
            broker_sync, "fetch_snapshot", side_effect=AssertionError("read the broker")
        ):
            with self.assertRaises(SyncInProgressError) as caught:
                sync(self.session)

        self.assertIn("already running", str(caught.exception))


class FailedFetchTests(SyncTestCase):
    """The read failed, so the write must not have happened. Nothing here is mocked at the
    database level: the rows are read back and compared."""

    def test_a_failed_read_leaves_the_previous_snapshot_and_timestamp_intact(self):
        portfolio = self.add_portfolio()
        apply_snapshot(self.session, SANITY_CHECK)

        with mock.patch.object(
            broker_sync,
            "fetch_snapshot",
            side_effect=ProviderUnavailableError("Alpaca did not respond in time."),
        ):
            with self.assertRaises(ProviderUnavailableError):
                sync(self.session)

        stored = self.holdings(portfolio)
        self.assertEqual(sorted(stored), ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(stored["AAPL"].quantity, Decimal("5"))
        self.assertEqual(stored["AAPL"].market_price, Decimal("201.25"))
        self.assertEqual(portfolio.cash_balance, Decimal("2500.25"))
        self.assertEqual(portfolio.broker_equity, Decimal("12345.67"))
        # The timestamp is part of the snapshot. Moving it would claim a sync that did
        # not happen, and the page would show fresh-looking figures that are not fresh.
        self.assertEqual(portfolio.last_synced_at, STARTED_AT)

    def test_a_failed_read_on_a_never_synced_portfolio_leaves_it_unlinked(self):
        portfolio = self.add_portfolio()
        self.add_holding(portfolio, "NVDA", "10", "120.00")

        with mock.patch.object(
            broker_sync, "fetch_snapshot", side_effect=ProviderUnavailableError("down")
        ):
            with self.assertRaises(ProviderUnavailableError):
                sync(self.session)

        self.assertIsNone(portfolio.broker)
        self.assertIsNone(portfolio.last_synced_at)
        self.assertEqual(portfolio.cash_balance, Decimal("10000.00"))
        self.assertEqual(sorted(self.holdings(portfolio)), ["NVDA"])


class SeedingTests(SyncTestCase):
    """`python -m app.seed` writes the columns the sync writes, so it has to stand down
    once there is real data in them.

    The seed gets the test session rather than its own, so this runs against the same
    database the rest of the file uses, inside the same rolled-back transaction.
    """

    def run_seed(self) -> None:
        with mock.patch.object(
            seed_module, "SessionLocal", return_value=Session(bind=self.connection)
        ):
            seed_module.seed()

    def test_seeding_a_linked_portfolio_leaves_every_column_alone(self):
        portfolio = self.add_portfolio(account_id=ACCOUNT_ID)
        apply_snapshot(self.session, SANITY_CHECK)
        self.session.expire_all()

        self.run_seed()

        stored = self.holdings(portfolio)
        self.assertEqual(sorted(stored), ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(stored["AAPL"].market_price, Decimal("201.25"))
        self.assertEqual(stored["AAPL"].average_buy_price, Decimal("180.1000"))
        self.assertEqual(portfolio.cash_balance, Decimal("2500.25"))
        self.assertEqual(portfolio.last_synced_at, STARTED_AT)

    def test_seeding_a_linked_portfolio_does_not_resurrect_a_closed_position(self):
        """The placeholder holdings include NVDA. Once it is closed, re-running the seed
        must not put it back -- a made-up position beside real ones, with no price."""
        portfolio = self.add_portfolio(account_id=ACCOUNT_ID)
        apply_snapshot(self.session, SANITY_CHECK)
        apply_snapshot(
            self.session,
            snapshot(AAPL, MSFT, started_at=STARTED_AT + timedelta(seconds=30)),
        )
        self.session.expire_all()

        self.run_seed()

        self.assertEqual(sorted(self.holdings(portfolio)), ["AAPL", "MSFT"])

    def test_seeding_an_unlinked_portfolio_still_creates_the_placeholders(self):
        """The unlinked path is unchanged: this is how a fresh install gets something on
        screen before the first sync."""
        self.run_seed()

        portfolio = self.session.scalar(
            select(Portfolio).where(Portfolio.name == DEMO_PORTFOLIO_NAME)
        )
        self.assertIsNotNone(portfolio)
        self.assertIsNone(portfolio.broker)
        self.assertEqual(portfolio.cash_balance, Decimal("10000.00"))
        self.assertEqual(
            sorted(self.holdings(portfolio)), ["AAPL", "MSFT", "NVDA"]
        )

    def test_running_the_seed_twice_changes_nothing_the_second_time(self):
        self.run_seed()

        def portfolio() -> Portfolio:
            return self.session.scalar(
                select(Portfolio).where(Portfolio.name == DEMO_PORTFOLIO_NAME)
            )

        first = {
            symbol: (row.quantity, row.average_buy_price)
            for symbol, row in self.holdings(portfolio()).items()
        }

        self.run_seed()

        second = {
            symbol: (row.quantity, row.average_buy_price)
            for symbol, row in self.holdings(portfolio()).items()
        }
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
