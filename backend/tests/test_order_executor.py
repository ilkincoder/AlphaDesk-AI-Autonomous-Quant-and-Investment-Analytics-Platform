"""Carrying out an approved batch: sequencing, duplicates, partial fills, and recovery.

The real executor, the real store, the real database. The broker answers from dictionaries and
records every submission, and the portfolio sync is redirected to the test's transaction -- so
nothing here reaches Alpaca, and `broker.submissions` is the complete record of what would have
been sent.

`broker.submissions` is the assertion most of this file exists to make. "One order per approval"
is not a property that can be checked by reading the code that submits it; it is checked by
counting the POSTs that a replay, a restart, a timeout and a double click produce.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from app import orders
from app.alpaca import ProviderUnavailableError, SubmissionUncertainError
from app.models import Holding, OrderIntent, Portfolio
from app.order_execution import OUTCOME_APPROVED, OUTCOME_RENEWAL_REQUIRED, decide
from app.order_executor import (
    SYNC_RETRY_WINDOW_SECONDS,
    UNCERTAIN_CHECKS_REQUIRED,
    UNCERTAIN_GRACE_SECONDS,
    run_once,
)
from tests.conversation_doubles import SessionProxy
from tests.execution_doubles import (
    NOW,
    FailureSafeTransaction,
    FakeBroker,
    a_proposal,
    a_sell_and_a_buy,
    a_trade,
    execution_sessions,
    fake_broker,
    fake_portfolio_sync,
    store_proposal,
)
from tests.test_tools_contracts import ToolTestCase


class ExecutorTestCase(FailureSafeTransaction, ToolTestCase):
    """An approved batch, a scripted broker, and a `run_once` a test drives by hand."""

    def setUp(self) -> None:
        super().setUp()
        self.guard_transaction()

        self.portfolio = Portfolio(
            name="Alpaca Paper",
            currency="USD",
            cash_balance=Decimal("1000.00"),
            broker="alpaca_paper",
            broker_account_id="acct-1",
            broker_equity=Decimal("15500.00"),
            last_synced_at=NOW,
        )
        self.session.add(self.portfolio)
        self.session.flush()
        for symbol, quantity, price in (("AAPL", "50", "200.00"), ("MSFT", "10", "450.00")):
            self.add_position(symbol, quantity, price)

        self.sessions = execution_sessions(self.session)
        self.sessions.__enter__()
        self.addCleanup(self.sessions.__exit__, None, None, None)
        extra = mock.patch("app.proposals.SessionLocal", return_value=SessionProxy(self.session))
        extra.start()
        self.addCleanup(extra.stop)

        self.broker = FakeBroker(
            cash="1000.00",
            positions={"AAPL": ("50", "200.00"), "MSFT": ("10", "450.00")},
        )
        self.broker_patch = fake_broker(self.broker)
        self.broker_patch.__enter__()
        self.addCleanup(self.broker_patch.__exit__, None, None, None)

        self.sync = fake_portfolio_sync(self.broker)
        self.sync.__enter__()
        self.addCleanup(self.sync.__exit__, None, None, None)

        self.saver = mock.patch(
            "app.agent.checkpointing.get_checkpointer",
            return_value=__import__(
                "app.agent.checkpointing", fromlist=["in_memory"]
            ).in_memory(),
        )
        self.saver.start()
        self.addCleanup(self.saver.stop)

    def add_position(self, symbol: str, quantity: str, price: str) -> None:
        self.session.add(
            Holding(
                portfolio_id=self.portfolio.id,
                symbol=symbol,
                quantity=Decimal(quantity),
                average_buy_price=Decimal("100.00"),
                market_price=Decimal(price),
                market_value=(Decimal(quantity) * Decimal(price)).quantize(Decimal("0.01")),
            )
        )
        self.session.flush()
        self.session.expire_all()

    # --- driving it ---------------------------------------------------------------------

    def approve(self, *, trades=None, proposal_id: str = "prop-1") -> str:
        """Store a proposal and approve it, returning the batch id. Nothing is submitted."""
        store_proposal(
            self.session,
            a_proposal(
                self.portfolio,
                trades=trades if trades is not None else a_sell_and_a_buy(),
                proposal_id=proposal_id,
            ),
        )
        # The same fixed instant the broker's clock answers with, so the session check is a
        # comparison between two agreed times rather than between a canned clock and the wall.
        outcome = decide(
            proposal_id=proposal_id,
            decision="approved",
            idempotency_key=f"key-{proposal_id}",
            now=NOW,
        )
        self.assertEqual(outcome.outcome, OUTCOME_APPROVED, outcome.detail)
        return outcome.batch_id

    def run_once(self, batch_id: str | None = None) -> dict:
        return run_once(batch_id, now=NOW)

    def batch(self, batch_id: str) -> dict:
        return orders.batch(batch_id)

    def intents(self, batch_id: str) -> dict[str, dict]:
        return {item["symbol"]: item for item in self.batch(batch_id)["orders"]}

    def client_id(self, batch_id: str, sequence: int) -> str:
        return orders.client_order_id(batch_id, sequence)

    def submitted(self) -> list[tuple[str, str]]:
        return [(item["symbol"], item["side"]) for item in self.broker.submissions]

    def age_submission(self, batch_id: str, symbol: str, seconds: int) -> None:
        """Backdate one intent's submission, so the grace period can be reached without waiting."""
        row = (
            self.session.query(OrderIntent)
            .filter(OrderIntent.batch_id == batch_id, OrderIntent.symbol == symbol)
            .one()
        )
        # Aged against `NOW`, which is the clock every `run_once` in this file passes in. Ageing
        # against the wall clock instead would leave the submission permanently too young.
        row.submitted_at = NOW - timedelta(seconds=seconds)
        self.session.flush()
        self.session.expire_all()


class NoSubmissionWithoutApprovalTest(ExecutorTestCase):
    def test_a_pass_with_no_batch_does_nothing_at_all(self):
        self.assertIsNone(self.run_once())
        self.assertEqual(self.broker.submissions, [])

    def test_a_stored_proposal_that_was_never_approved_submits_nothing(self):
        """The gate is the decision, not the proposal. A proposal on screen is a calculation."""
        store_proposal(self.session, a_proposal(self.portfolio, trades=a_sell_and_a_buy()))

        self.assertIsNone(self.run_once())

        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(self.session.query(OrderIntent).count(), 0)

    def test_a_rejected_proposal_never_becomes_a_batch_and_never_submits(self):
        store_proposal(self.session, a_proposal(self.portfolio, trades=a_sell_and_a_buy()))
        decide(proposal_id="prop-1", decision="rejected", idempotency_key="key-1")

        self.assertIsNone(self.run_once())

        self.assertEqual(self.broker.submissions, [])


class SequencingTest(ExecutorTestCase):
    def test_the_sale_is_submitted_first_and_the_purchase_waits_for_it(self):
        """Not "accepted" -- filled. A sale that has not settled has not produced the money the
        purchase is funded with."""
        batch_id = self.approve()

        self.run_once(batch_id)

        self.assertEqual(self.submitted(), [("AAPL", "sell")])
        intents = self.intents(batch_id)
        self.assertEqual(intents["AAPL"]["status"], orders.INTENT_ACCEPTED)
        self.assertEqual(intents["MSFT"]["status"], orders.INTENT_PLANNED)
        self.assertEqual(self.batch(batch_id)["status"], orders.BATCH_SUBMITTING)

        # The pass that sends the sale ends there. It is the *next* pass, which finds the sale
        # still working, that says so on the record.
        self.run_once(batch_id)
        self.assertTrue(
            any("Waiting for every sale" in note for note in self.batch(batch_id)["notes"])
        )
        self.assertEqual(self.submitted(), [("AAPL", "sell")])

    def test_a_partially_filled_sale_still_holds_the_purchase_back(self):
        batch_id = self.approve()
        self.run_once(batch_id)

        self.broker.fill(self.client_id(batch_id, 1), quantity="4")
        self.run_once(batch_id)

        intents = self.intents(batch_id)
        self.assertEqual(intents["AAPL"]["status"], orders.INTENT_PARTIALLY_FILLED)
        self.assertEqual(intents["AAPL"]["filled_quantity"], "4.000000")
        self.assertEqual(self.submitted(), [("AAPL", "sell")])

    def test_the_purchase_follows_once_the_sale_has_filled(self):
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))

        self.run_once(batch_id)

        self.assertEqual(self.submitted(), [("AAPL", "sell"), ("MSFT", "buy")])

    def test_the_orders_are_whole_share_market_day_orders(self):
        batch_id = self.approve()

        self.run_once(batch_id)

        submitted = self.broker.submissions[0]
        self.assertEqual(submitted["type"], "market")
        self.assertEqual(submitted["time_in_force"], "day")
        # The six decimals are the intent column's scale, and Alpaca takes the quantity as a
        # string -- so this is the exact value the plan was approved at, not a rounded one.
        self.assertEqual(submitted["quantity"], "10.000000")
        self.assertEqual(submitted["symbol"], "AAPL")
        self.assertEqual(submitted["side"], "sell")

    def test_a_rejected_sale_stops_the_purchase_and_says_so(self):
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.reject(self.client_id(batch_id, 1))

        self.run_once(batch_id)

        self.assertEqual(self.submitted(), [("AAPL", "sell")])
        batch = self.batch(batch_id)
        self.assertEqual(batch["status"], orders.BATCH_HALTED)
        self.assertIn("did not sell", batch["halt_reason"])
        self.assertEqual(self.intents(batch_id)["MSFT"]["status"], orders.INTENT_HALTED)

    def test_an_expired_sale_stops_the_purchase(self):
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.reject(self.client_id(batch_id, 1), status="expired")

        self.run_once(batch_id)

        self.assertEqual(self.submitted(), [("AAPL", "sell")])
        self.assertEqual(self.intents(batch_id)["MSFT"]["status"], orders.INTENT_HALTED)

    def test_a_session_that_has_ended_stops_what_was_never_sent(self):
        """Nothing is queued for the next open: a decision made against this morning's prices
        should not be executed against tomorrow's."""
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        self.broker.is_open = False

        self.run_once(batch_id)

        self.assertEqual(self.submitted(), [("AAPL", "sell")])
        batch = self.batch(batch_id)
        self.assertEqual(batch["status"], orders.BATCH_HALTED)
        self.assertIn("market is closed", batch["halt_reason"])


class AffordabilityTest(ExecutorTestCase):
    def test_a_purchase_the_account_can_no_longer_afford_is_stopped_not_resized(self):
        """A smaller order is a different order than the one that was approved."""
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        # Something else spent the money between the sale settling and the purchase going out.
        self.broker.cash = Decimal("500.00")

        self.run_once(batch_id)

        self.assertEqual(self.submitted(), [("AAPL", "sell")])
        batch = self.batch(batch_id)
        self.assertEqual(batch["status"], orders.BATCH_HALTED)
        self.assertIn("not resized", batch["halt_reason"])
        self.assertEqual(self.intents(batch_id)["MSFT"]["quantity"], "2.000000")

    def test_a_portfolio_that_moved_underneath_the_batch_stops_the_remainder(self):
        """The plan was validated against a holding of 10 MSFT. If the account now shows 15,
        something this application did not do has traded, and the rest is not sent."""
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        self.broker.positions["MSFT"] = ("15", "450.00")

        self.run_once(batch_id)

        self.assertEqual(self.submitted(), [("AAPL", "sell")])
        batch = self.batch(batch_id)
        self.assertEqual(batch["status"], orders.BATCH_HALTED)
        self.assertIn("MSFT", batch["halt_reason"])
        self.assertIn("plan does not describe", batch["halt_reason"])

    def test_the_batch_reaches_completion_when_every_order_filled(self):
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        self.run_once(batch_id)

        self.broker.fill(self.client_id(batch_id, 2))
        self.run_once(batch_id)

        batch = self.batch(batch_id)
        self.assertEqual(batch["status"], orders.BATCH_COMPLETED)
        self.assertEqual(batch["sync_status"], orders.SYNC_OK)
        self.assertEqual([item["status"] for item in batch["orders"]], ["filled", "filled"])


class DuplicatePreventionTest(ExecutorTestCase):
    def test_an_unanswered_submission_is_unknown_and_is_not_sent_again(self):
        """The single most important case. A POST that got no answer may have created an order,
        so it is neither failed nor retried -- it is reconciled by its client order id."""
        batch_id = self.approve()
        self.broker.fail_next_submission = SubmissionUncertainError(
            "Alpaca did not answer the order submission in time. Whether the order reached the "
            "broker is not known, so nothing was retried and no second order was sent."
        )

        self.run_once(batch_id)

        intents = self.intents(batch_id)
        self.assertEqual(intents["AAPL"]["status"], orders.INTENT_UNKNOWN)
        self.assertIn("not known", intents["AAPL"]["error"])
        self.assertEqual(len(self.broker.submissions), 1)

        # The broker was asked about it and had nothing: still not a reason to send it again.
        self.run_once(batch_id)

        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.intents(batch_id)["AAPL"]["status"], orders.INTENT_UNKNOWN)

    def test_an_order_the_broker_did_receive_is_adopted_rather_than_resent(self):
        """The crash-after-acceptance case: the order exists at the broker, this application never
        heard, and reconciliation finds it by the id it derived."""
        batch_id = self.approve()
        self.broker.fail_next_submission = SubmissionUncertainError("no answer")
        self.run_once(batch_id)

        # The broker had in fact accepted it before the answer was lost.
        self.broker.receive(
            client_order_id=self.client_id(batch_id, 1),
            symbol="AAPL",
            side="sell",
            quantity="10",
        )
        self.broker.fill(self.client_id(batch_id, 1))

        self.run_once(batch_id)

        intent = self.intents(batch_id)["AAPL"]
        self.assertEqual(intent["status"], orders.INTENT_FILLED)
        self.assertEqual(intent["filled_quantity"], "10.000000")
        self.assertEqual(intent["filled_avg_price"], "200.0000")
        self.assertEqual(intent["broker_order_id"], f"broker-received-{self.client_id(batch_id, 1)}")

        # The sale appears exactly once among the POSTs this application made -- the one whose
        # answer was lost. Finding it at the broker did not produce a second one, and the
        # purchase follows because the sale is now known to have filled.
        self.assertEqual(self.submitted(), [("AAPL", "sell"), ("MSFT", "buy")])

    def test_a_submission_that_was_never_confirmed_is_settled_but_never_resent(self):
        """Enough checks, far enough apart, and the order is recorded as never confirmed -- which
        is the honest outcome, and a different one from quietly sending it a second time."""
        batch_id = self.approve()
        self.broker.fail_next_submission = SubmissionUncertainError("no answer")
        self.run_once(batch_id)

        self.age_submission(batch_id, "AAPL", UNCERTAIN_GRACE_SECONDS + 5)
        for _ in range(UNCERTAIN_CHECKS_REQUIRED):
            self.run_once(batch_id)

        intent = self.intents(batch_id)["AAPL"]
        self.assertEqual(intent["status"], orders.INTENT_NOT_FOUND)
        self.assertIn("has not been resent", intent["error"])
        self.assertEqual(len(self.broker.submissions), 1)

    def test_one_not_found_is_not_enough_to_settle_an_uncertain_submission(self):
        """A submission can be answered late. Settling on the first miss would turn a slow reply
        into an order this application believes never happened."""
        batch_id = self.approve()
        self.broker.fail_next_submission = SubmissionUncertainError("no answer")
        self.run_once(batch_id)
        self.age_submission(batch_id, "AAPL", UNCERTAIN_GRACE_SECONDS + 5)

        self.run_once(batch_id)

        self.assertEqual(self.intents(batch_id)["AAPL"]["status"], orders.INTENT_UNKNOWN)

    def test_a_crash_before_the_order_was_sent_leaves_it_unsubmitted(self):
        """The claim is taken and the POST never happens. Nothing is invented to fill the gap:
        the order is reported as never confirmed and the dependent work stops."""
        batch_id = self.approve()
        sell = self.intents(batch_id)["AAPL"]
        orders.claim_intent(sell["intent_id"], now=NOW)

        self.run_once(batch_id)
        self.age_submission(batch_id, "AAPL", UNCERTAIN_GRACE_SECONDS + 5)
        for _ in range(UNCERTAIN_CHECKS_REQUIRED):
            self.run_once(batch_id)

        self.assertEqual(self.submitted(), [])
        self.assertEqual(self.intents(batch_id)["AAPL"]["status"], orders.INTENT_NOT_FOUND)
        # And the purchase that depended on it was stopped.
        self.assertEqual(self.intents(batch_id)["MSFT"]["status"], orders.INTENT_HALTED)
        self.assertEqual(self.batch(batch_id)["status"], orders.BATCH_HALTED)

    def test_a_restart_derives_the_same_client_order_id(self):
        """Nothing about the id depends on process state, which is what lets a later process --
        or a different one entirely -- ask the broker about the same order."""
        batch_id = self.approve()
        expected = self.client_id(batch_id, 1)

        self.run_once(batch_id)

        self.assertEqual(self.broker.submissions[0]["client_order_id"], expected)
        self.assertEqual(
            orders.batch(batch_id)["orders"][0]["client_order_id"], expected
        )
        self.assertTrue(expected.startswith(f"ad-{batch_id[:16]}-"))
        self.assertLessEqual(len(expected), 128)

    def test_a_second_pass_does_not_resubmit_an_order_the_broker_already_has(self):
        batch_id = self.approve()
        self.run_once(batch_id)

        for _ in range(3):
            self.run_once(batch_id)

        self.assertEqual(len(self.broker.submissions), 1)

    def test_the_broker_s_own_words_are_kept_beside_the_mapped_status(self):
        batch_id = self.approve()

        self.run_once(batch_id)

        intent = self.intents(batch_id)["AAPL"]
        self.assertEqual(intent["broker_status"], "accepted")
        self.assertEqual(intent["status"], orders.INTENT_ACCEPTED)
        self.assertEqual(intent["broker_order_id"], "broker-1")

    def test_a_status_this_build_does_not_recognise_is_treated_as_unknown(self):
        """Never as accepted: an order in a state this build does not understand is exactly the
        one that must not be assumed to be progressing."""
        batch_id = self.approve()
        self.run_once(batch_id)

        order = self.broker.orders[self.client_id(batch_id, 1)]
        from app.alpaca import BrokerOrder

        self.broker.orders[self.client_id(batch_id, 1)] = BrokerOrder(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            filled_quantity=Decimal("0"),
            filled_avg_price=None,
            status="something_new",
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            limit_price=None,
            submitted_at=order.submitted_at,
        )

        self.run_once(batch_id)

        self.assertEqual(orders.map_broker_status("something_new"), orders.INTENT_UNKNOWN)
        self.assertEqual(self.intents(batch_id)["AAPL"]["broker_status"], "something_new")

    def test_a_broker_that_cannot_be_read_submits_nothing_and_says_so(self):
        batch_id = self.approve()

        with mock.patch(
            "app.order_executor.fetch_snapshot",
            side_effect=ProviderUnavailableError("Alpaca did not respond in time."),
        ):
            self.run_once(batch_id)

        self.assertEqual(self.broker.submissions, [])
        self.assertTrue(
            any(
                "could not be read" in note
                for note in self.batch(batch_id)["notes"]
            )
        )


class PortfolioSyncTest(ExecutorTestCase):
    def test_the_portfolio_is_written_from_the_broker_s_report_of_what_filled(self):
        """Cash and holdings come from the broker's own numbers, never from subtracting this
        application's estimates."""
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 2))
        self.run_once(batch_id)

        self.session.expire_all()
        stored = self.session.get(Portfolio, self.portfolio.id)
        quantities = {
            row.symbol: row.quantity
            for row in self.session.query(Holding)
            .filter(Holding.portfolio_id == self.portfolio.id)
            .all()
        }
        # 1000.00 + 10 x 200.00 - 2 x 450.00 = 2100.00
        self.assertEqual(stored.cash_balance, Decimal("2100.00"))
        self.assertEqual(quantities["AAPL"], Decimal("40.000000"))
        self.assertEqual(quantities["MSFT"], Decimal("12.000000"))

    def test_a_sync_failure_is_recorded_without_making_the_execution_look_failed(self):
        """Two different problems with two different fixes. A filled order that could not be
        written into the portfolio is not an order that did not fill."""
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 2))

        from app import broker_sync

        with fake_portfolio_sync(
            self.broker, failure=broker_sync.PortfolioNotStoredError("no portfolio")
        ):
            self.run_once(batch_id)

        batch = self.batch(batch_id)
        self.assertEqual(batch["status"], orders.BATCH_COMPLETED)
        self.assertEqual(batch["sync_status"], orders.SYNC_FAILED)
        self.assertIn("no portfolio", batch["sync_error"])

    def test_a_sync_that_failed_is_tried_again_while_the_batch_is_recent(self):
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 2))

        from app import broker_sync

        with fake_portfolio_sync(
            self.broker, failure=broker_sync.PortfolioNotStoredError("no portfolio")
        ):
            self.run_once(batch_id)

        self.run_once(None)

        self.assertEqual(self.batch(batch_id)["sync_status"], orders.SYNC_OK)

    def test_a_sync_is_retried_while_the_batch_is_recent_and_then_left_alone(self):
        """A sync still failing a quarter of an hour later needs a person, not another attempt.

        The assertion is on the number of *attempts*, because "it stopped retrying" is not a
        state the batch records -- it is the absence of further calls, and only a count shows it.
        """
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 2))

        from app import broker_sync

        attempts = []

        with fake_portfolio_sync(
            self.broker, failure=broker_sync.PortfolioNotStoredError("no portfolio")
        ) as counter:
            self.run_once(batch_id)   # finishes the batch and tries to sync -- and fails
            self.run_once(None)       # a later pass tries the sync again
            self.assertEqual(len(counter), 2, "a recent batch's sync should be retried")
            self.assertEqual(self.batch(batch_id)["sync_status"], orders.SYNC_FAILED)

            late = NOW + timedelta(seconds=SYNC_RETRY_WINDOW_SECONDS + 60)
            run_once(None, now=late)
            self.assertEqual(len(counter), 2, "a stale batch's sync should be left alone")

        self.assertEqual(self.batch(batch_id)["sync_status"], orders.SYNC_FAILED)


class PartialCompletionTest(ExecutorTestCase):
    def test_a_batch_that_could_only_do_part_of_what_was_approved_says_which_part(self):
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        self.run_once(batch_id)
        self.broker.reject(self.client_id(batch_id, 2))

        self.run_once(batch_id)

        batch = self.batch(batch_id)
        self.assertEqual(batch["status"], orders.BATCH_PARTIAL)
        self.assertIn("1 of 2 order(s) filled", " ".join(batch["notes"]))
        self.assertIn("MSFT (buy) is rejected", " ".join(batch["notes"]))
        self.assertEqual(
            [item["status"] for item in batch["orders"]], ["filled", "rejected"]
        )

    def test_a_halted_batch_keeps_what_already_filled(self):
        """Nothing completed is undone, and nothing remaining is rearranged to look tidier."""
        batch_id = self.approve()
        self.run_once(batch_id)
        self.broker.fill(self.client_id(batch_id, 1))
        # The money is gone before the pass that would send the purchase, so it never goes out.
        self.broker.cash = Decimal("10.00")

        self.run_once(batch_id)

        batch = self.batch(batch_id)
        self.assertEqual(batch["status"], orders.BATCH_HALTED)
        self.assertEqual(batch["orders"][0]["status"], orders.INTENT_FILLED)
        self.assertEqual(batch["orders"][0]["filled_quantity"], "10.000000")
        self.assertEqual(batch["orders"][1]["status"], orders.INTENT_HALTED)


class StaleBatchTest(ExecutorTestCase):
    def test_a_batch_that_outlives_a_session_is_halted_rather_than_chased_for_ever(self):
        """A DAY order cannot outlive the session it was sent in, so a batch still working after
        one has stopped being something to follow and started being something to report."""
        batch_id = self.approve()

        late = NOW + timedelta(hours=9)
        run_once(batch_id, now=late)

        batch = self.batch(batch_id)
        self.assertEqual(batch["status"], orders.BATCH_HALTED)
        self.assertIn("longer than the trading session", batch["halt_reason"])
        self.assertEqual(self.broker.submissions, [])

    def test_a_proposal_that_is_held_for_renewal_never_reaches_the_executor(self):
        """The drift refusal leaves nothing to execute: no decision, no batch, no orders."""
        store_proposal(self.session, a_proposal(self.portfolio, trades=a_sell_and_a_buy()))
        self.broker.positions["AAPL"] = ("50", "250.00")

        outcome = decide(
            proposal_id="prop-1", decision="approved", idempotency_key="key-1", now=NOW
        )

        self.assertEqual(outcome.outcome, OUTCOME_RENEWAL_REQUIRED)
        self.assertIsNone(self.run_once())
        self.assertEqual(self.broker.submissions, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
