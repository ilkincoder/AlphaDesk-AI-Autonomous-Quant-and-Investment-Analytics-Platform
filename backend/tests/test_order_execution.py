"""Approving and rejecting a proposal: the workflow, the interrupt, and the one-decision rule.

The real routes, the real store, the real graph and the real database. Two things are replaced:
the broker, which answers from dictionaries, and the checkpointer, which is in memory for the
tests about the workflow and the real PostgreSQL one for the test about surviving a process.

Nothing here submits an order. An approval records a decision and opens a batch; sending is
`app.order_executor`'s job and `tests/test_order_executor.py` is where it is checked -- which is
itself the property that "no submission without explicit approval" rests on.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from decimal import Decimal
from unittest import mock

from sqlalchemy import text

from app import orders, proposals
from app.agent import checkpointing, execution_graph
from app.db import SessionLocal
from app.execution_plan import REASON_RENEWAL_REQUIRED
from app.models import ExecutionBatch, OrderIntent, Portfolio, RebalanceDecision
from app.order_execution import (
    OUTCOME_ALREADY_DECIDED,
    OUTCOME_APPROVED,
    OUTCOME_REFUSED,
    OUTCOME_REJECTED,
    OUTCOME_RENEWAL_REQUIRED,
    decide,
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
    store_proposal,
)
from tests.test_tools_contracts import ToolTestCase


class ApprovalTestCase(FailureSafeTransaction, ToolTestCase):
    """A stored portfolio, a stored proposal, a scripted broker and an in-memory checkpoint store."""

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

        self.sessions_extra = mock.patch(
            "app.proposals.SessionLocal", return_value=SessionProxy(self.session)
        )
        self.sessions_extra.start()
        self.addCleanup(self.sessions_extra.stop)

        self.broker = FakeBroker(
            cash="1000.00",
            positions={"AAPL": ("50", "200.00"), "MSFT": ("10", "450.00")},
        )
        self.broker_patch = fake_broker(self.broker)
        self.broker_patch.__enter__()
        self.addCleanup(self.broker_patch.__exit__, None, None, None)

        self.checkpointer = checkpointing.in_memory()
        self.saver = mock.patch(
            "app.agent.checkpointing.get_checkpointer", return_value=self.checkpointer
        )
        self.saver.start()
        self.addCleanup(self.saver.stop)

    def add_position(self, symbol: str, quantity: str, price: str) -> None:
        from app.models import Holding

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

    def store(self, *, trades=None, status: str = "proposed", proposal_id: str = "prop-1") -> dict:
        proposal = a_proposal(
            self.portfolio,
            trades=trades if trades is not None else a_sell_and_a_buy(),
            status=status,
            proposal_id=proposal_id,
        )
        store_proposal(self.session, proposal)
        return proposal

    def approve(self, **overrides):
        """Decide about prop-1, in the words a request uses.

        The route maps "approve"/"reject" onto the recorded words before calling `decide`, so a
        helper that passed the request's word straight through would be testing a contract
        nothing calls -- and, before `decide` refused unknown words, silently approving every
        rejection a test asked for.
        """
        payload = {"decision": "approve", "idempotency_key": "key-1"}
        payload.update(overrides)
        word = payload.pop("decision")
        return decide(
            proposal_id="prop-1", decision={"approve": "approved", "reject": "rejected"}[word],
            **payload,
        )

    def stored_decisions(self):
        return list(self.session.query(RebalanceDecision).all())

    def stored_batches(self):
        return list(self.session.query(ExecutionBatch).all())

    def stored_intents(self):
        return list(self.session.query(OrderIntent).order_by(OrderIntent.sequence).all())


class ApprovalTest(ApprovalTestCase):
    def test_approving_records_the_decision_and_opens_a_batch_without_sending_anything(self):
        """The approval resolves and records; it does not submit. That separation is what makes
        "no order without an explicit approval" a property of the code rather than of a promise.
        """
        self.store()

        outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_APPROVED)
        self.assertIsNotNone(outcome.batch_id)

        decision = self.stored_decisions()[0]
        self.assertEqual(decision.decision, "approved")
        self.assertEqual(decision.proposal_id, "prop-1")
        self.assertEqual(decision.broker_account_id, "acct-1")
        self.assertEqual(decision.cash_at_approval, Decimal("1000.00"))
        self.assertTrue(decision.plan_fingerprint)

        batch = self.stored_batches()[0]
        self.assertEqual(batch.status, orders.BATCH_PENDING)
        self.assertEqual(batch.broker_account_id, "acct-1")
        self.assertEqual(batch.sync_status, orders.SYNC_PENDING)

        intents = self.stored_intents()
        self.assertEqual(
            [(item.symbol, item.side, str(item.quantity)) for item in intents],
            [("AAPL", "sell", "10.000000"), ("MSFT", "buy", "2.000000")],
        )
        self.assertEqual(
            [item.status for item in intents],
            [orders.INTENT_PLANNED, orders.INTENT_PLANNED],
        )
        self.assertEqual([item.order_type for item in intents], ["market", "market"])
        self.assertEqual([item.time_in_force for item in intents], ["day", "day"])

        self.assertEqual(
            self.broker.submissions, [], "approving must not submit anything to the broker"
        )

    def test_every_intent_carries_a_client_order_id_derived_from_its_place_in_the_batch(self):
        """Derived rather than generated, so a restart can reconstruct the id it would have used
        and ask the broker what became of the order."""
        self.store()

        self.approve()

        batch = self.stored_batches()[0]
        for intent in self.stored_intents():
            self.assertEqual(
                intent.client_order_id, orders.client_order_id(batch.id, intent.sequence)
            )
            self.assertTrue(intent.client_order_id.startswith("ad-"))
        self.assertEqual(len({item.client_order_id for item in self.stored_intents()}), 2)

    def test_the_decision_records_the_exact_orders_it_bound_to(self):
        self.store()

        self.approve()

        plan = self.stored_decisions()[0].order_plan
        self.assertEqual(
            [(item["symbol"], item["side"], item["quantity"]) for item in plan],
            [("AAPL", "sell", "10"), ("MSFT", "buy", "2")],
        )
        # The plan keeps the scale the proposal's calculation used. The six-decimal scale is the
        # intent column's, not this record's.
        self.assertEqual([item["quantity_before"] for item in plan], ["50", "10"])

    def test_a_proposal_that_proposed_nothing_is_refused(self):
        self.store(trades=[])

        outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_REFUSED)
        self.assertEqual(self.stored_decisions(), [])
        self.assertEqual(self.stored_batches(), [])

    def test_a_proposal_that_failed_is_refused(self):
        self.store(status="unavailable")

        outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_REFUSED)
        self.assertEqual(self.stored_decisions(), [])


class DecisionOnceTest(ApprovalTestCase):
    def test_approving_twice_records_one_decision_and_opens_one_batch(self):
        """Two clicks, one answer. The unique constraint on the proposal is what decides it, and
        the second request is answered from the row rather than refused as an error."""
        self.store()

        first = self.approve()
        second = self.approve(idempotency_key="key-2")

        self.assertEqual(first.outcome, OUTCOME_APPROVED)
        self.assertEqual(second.outcome, OUTCOME_ALREADY_DECIDED)
        self.assertEqual(second.batch_id, first.batch_id)
        self.assertEqual(len(self.stored_decisions()), 1)
        self.assertEqual(len(self.stored_batches()), 1)
        self.assertEqual(len(self.stored_intents()), 2)
        self.assertEqual(self.broker.submissions, [])

    def test_approving_after_rejecting_is_a_conflict(self):
        """A rejected proposal cannot be approved by asking a second time: what a person decided
        is not undone by a retry."""
        self.store()

        rejected = self.approve(decision="reject")
        conflict = self.approve(idempotency_key="key-2")

        self.assertEqual(rejected.outcome, OUTCOME_REJECTED)
        self.assertEqual(conflict.outcome, OUTCOME_ALREADY_DECIDED)
        self.assertEqual(conflict.reason, "decision_conflict")
        self.assertEqual(self.stored_decisions()[0].decision, "rejected")
        self.assertEqual(self.stored_batches(), [])
        self.assertEqual(self.broker.submissions, [])

    def test_rejecting_twice_is_answered_from_the_decision_that_exists(self):
        self.store()

        self.approve(decision="reject")
        again = self.approve(decision="reject", idempotency_key="key-2")

        self.assertEqual(again.outcome, OUTCOME_ALREADY_DECIDED)
        self.assertIsNone(again.reason)
        self.assertEqual(len(self.stored_decisions()), 1)

    def test_a_rejection_creates_no_orders_and_no_batch(self):
        self.store()

        outcome = self.approve(decision="reject")

        self.assertEqual(outcome.outcome, OUTCOME_REJECTED)
        decision = self.stored_decisions()[0]
        self.assertEqual(decision.decision, "rejected")
        self.assertEqual(decision.order_plan, [])
        self.assertIsNone(decision.broker_account_id)
        self.assertEqual(self.stored_intents(), [])
        self.assertEqual(self.broker.submissions, [])

    def test_a_rejection_is_available_even_when_the_market_is_closed(self):
        """Declining is always available and always means the same thing. A rejection that could
        be refused because the market happened to be shut would be a decision the application
        would not let somebody make."""
        self.store()
        self.broker.is_open = False

        outcome = self.approve(decision="reject")

        self.assertEqual(outcome.outcome, OUTCOME_REJECTED)


class RefusalTest(ApprovalTestCase):
    def test_approving_while_the_market_is_closed_is_refused_and_records_nothing(self):
        self.store()
        self.broker.is_open = False

        outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_REFUSED)
        self.assertEqual(outcome.reason, "market_closed")
        self.assertIn("never queued overnight", outcome.detail)
        self.assertEqual(self.stored_decisions(), [])
        self.assertEqual(self.stored_batches(), [])

    def test_approving_when_the_portfolio_has_moved_is_refused(self):
        self.store()
        holding = self.portfolio.holdings[0]
        holding.quantity = Decimal("48")
        self.session.flush()
        self.session.expire_all()

        outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_REFUSED)
        self.assertEqual(outcome.reason, "portfolio_changed")
        self.assertEqual(self.stored_decisions(), [])

    def test_approving_against_an_account_the_portfolio_is_not_bound_to_is_refused(self):
        self.store()
        self.broker.account_id = "acct-2"

        outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_REFUSED)
        self.assertEqual(outcome.reason, "account_mismatch")

    def test_approving_a_proposal_that_is_not_stored_is_refused(self):
        outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_REFUSED)
        self.assertEqual(outcome.reason, "proposal_not_found")

    def test_approving_without_a_checkpointer_is_refused_rather_than_done_without_one(self):
        """The interrupt needs somewhere to live, and a graph that cannot be paused cannot be
        resumed by a decision endpoint. Saying so is the honest answer."""
        self.store()

        with mock.patch("app.agent.checkpointing.get_checkpointer", return_value=None):
            outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_REFUSED)
        self.assertEqual(outcome.reason, "checkpointing_unavailable")
        self.assertEqual(self.stored_decisions(), [])

    def test_a_broker_that_cannot_be_read_is_refused_rather_than_approved_against_a_memory(self):
        from app.alpaca import ProviderUnavailableError

        self.store()

        with mock.patch(
            "app.order_execution.fetch_snapshot",
            side_effect=ProviderUnavailableError("Alpaca did not respond in time."),
        ):
            outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_REFUSED)
        self.assertEqual(outcome.reason, "broker_unreachable")
        self.assertEqual(self.stored_decisions(), [])


class InterruptTest(ApprovalTestCase):
    def test_the_approval_pauses_at_a_real_interrupt_and_the_thread_can_be_found_again(self):
        """The plan that was validated is in the checkpoint, which is what lets a confirmation
        arriving later -- or in another process -- confirm *those* orders."""
        self.store()
        from app.order_execution import prepare, record

        graph = execution_graph.build_execution_graph(
            prepare=prepare, record=record, checkpointer=self.checkpointer
        )

        state = execution_graph.start(graph, {"proposal_id": "prop-1", "idempotency_key": "k"})

        paused = execution_graph.interrupted(state)
        self.assertIsNotNone(paused, "the graph must pause for a human decision")
        self.assertEqual(paused["proposal_id"], "prop-1")
        self.assertFalse(paused["renewal"])
        self.assertEqual(
            [item["symbol"] for item in paused["plan"]["orders"]], ["AAPL", "MSFT"]
        )
        # Nothing has been decided by pausing.
        self.assertEqual(self.stored_decisions(), [])

        pending = execution_graph.pending_interrupt(graph, "prop-1")
        self.assertIsNotNone(pending)

    def test_the_decided_plan_is_the_one_the_interrupt_was_showing(self):
        self.store()
        from app.order_execution import prepare, record

        graph = execution_graph.build_execution_graph(
            prepare=prepare, record=record, checkpointer=self.checkpointer
        )
        paused = execution_graph.interrupted(
            execution_graph.start(graph, {"proposal_id": "prop-1", "idempotency_key": "k"})
        )

        execution_graph.resume(graph, "prop-1", confirmed=True)

        recorded = self.stored_decisions()[0]
        self.assertEqual(
            [item["symbol"] for item in recorded.order_plan],
            [item["symbol"] for item in paused["plan"]["orders"]],
        )
        self.assertEqual(recorded.plan_fingerprint, paused["plan"]["fingerprint"])

    def test_a_plan_whose_prices_moved_is_held_for_a_second_confirmation(self):
        """A move beyond the tolerance means the figures on screen are stale. Nothing is sent,
        nothing is re-priced silently, and the refreshed plan comes back to be looked at."""
        self.store()
        self.broker.positions["AAPL"] = ("50", "250.00")

        first = self.approve()

        self.assertEqual(first.outcome, OUTCOME_RENEWAL_REQUIRED)
        self.assertEqual(first.reason, REASON_RENEWAL_REQUIRED)
        self.assertEqual([item["estimated_price"] for item in first.plan], ["250.00", "450.00"])
        self.assertEqual(
            self.stored_decisions(), [], "a renewal is not a decision and records nothing"
        )
        self.assertEqual(self.broker.submissions, [])

    def test_confirming_a_renewal_resumes_the_same_thread_with_the_same_orders(self):
        """The second click confirms the orders it was shown. Re-resolving them would mean the
        person confirmed one set and got another."""
        self.store()
        self.broker.positions["AAPL"] = ("50", "250.00")
        first = self.approve()

        confirmed = self.approve(idempotency_key="key-2", confirm=True)

        self.assertEqual(confirmed.outcome, OUTCOME_APPROVED)
        recorded = self.stored_decisions()[0]
        self.assertEqual(
            [item["symbol"] for item in recorded.order_plan],
            [item["symbol"] for item in first.plan],
        )
        self.assertEqual(
            [item["estimated_price"] for item in recorded.order_plan],
            [item["estimated_price"] for item in first.plan],
        )

    def test_a_renewal_leaves_the_thread_paused_so_a_confirmation_can_find_it(self):
        self.store()
        self.broker.positions["AAPL"] = ("50", "250.00")
        from app.order_execution import prepare, record

        graph = execution_graph.build_execution_graph(
            prepare=prepare, record=record, checkpointer=self.checkpointer
        )
        self.approve()

        pending = execution_graph.pending_interrupt(graph, "prop-1")

        self.assertIsNotNone(pending)
        self.assertTrue(pending["renewal"])

    def test_the_resume_value_carries_only_whether_the_person_confirmed(self):
        """Not a permission and not a state: the only thing a decision endpoint can tell the
        graph is whether it is confirmed."""
        self.store()
        from app.order_execution import prepare, record

        graph = execution_graph.build_execution_graph(
            prepare=prepare, record=record, checkpointer=self.checkpointer
        )
        execution_graph.start(graph, {"proposal_id": "prop-1", "idempotency_key": "k"})

        state = execution_graph.resume(graph, "prop-1", confirmed=True)

        self.assertTrue(state["confirmed"])
        self.assertEqual(self.stored_decisions()[0].decision, "approved")


class ReplayTest(ApprovalTestCase):
    def test_replaying_the_approval_does_not_open_a_second_batch(self):
        """The graph is replayed from the beginning, which is what a retry does. The unique
        constraint on the proposal is what makes the second run a no-op rather than a second
        batch of orders."""
        self.store()

        first = self.approve()
        second = self.approve(idempotency_key="key-2")

        self.assertEqual(second.batch_id, first.batch_id)
        self.assertEqual(len(self.stored_batches()), 1)
        self.assertEqual(len(self.stored_intents()), 2)

    def test_an_approval_does_not_read_the_generation_thread(self):
        """A proposal generated before this milestone has a finished checkpoint and no interrupt
        on it. The approval starts a thread of its own and binds to the proposal's stored record,
        which is why those proposals need no compatibility path."""
        self.store()
        row = self.session.query(type(self.portfolio)).first()  # keep the session warm
        self.assertIsNotNone(row)

        outcome = self.approve()

        self.assertEqual(outcome.outcome, OUTCOME_APPROVED)
        self.assertNotEqual(
            execution_graph.thread_id("prop-1"),
            "rebalance-prop-1",
            "an approval thread must not be the generation thread",
        )

    def test_a_second_approval_of_the_same_orders_cannot_open_a_second_batch(self):
        """Two proposals are two decisions, but only one batch may be live per account -- so a
        second approval while the first is still working is refused rather than racing it."""
        self.store(proposal_id="prop-1")
        self.store(proposal_id="prop-2")
        self.approve()

        second = decide(
            proposal_id="prop-2", decision="approved", idempotency_key="key-2"
        )

        self.assertEqual(second.outcome, OUTCOME_REFUSED)
        self.assertEqual(second.reason, "decision_conflict")
        self.assertEqual(len(self.stored_batches()), 1)


class PersistenceTest(ApprovalTestCase):
    def test_the_decision_and_its_batch_survive_a_reader_that_never_saw_them_written(self):
        """Everything crosses the store's boundary as plain values, so a later request in a
        later session reads the same record rather than an instance that outlived its session."""
        self.store()
        outcome = self.approve()

        decision = orders.decision_for("prop-1")
        batch = orders.batch_for_proposal("prop-1")

        self.assertEqual(decision["decision"], "approved")
        self.assertEqual(batch["batch_id"], outcome.batch_id)
        self.assertEqual(len(batch["orders"]), 2)
        self.assertEqual(batch["orders"][0]["quantity"], "10.000000")
        self.assertEqual(orders.active_batch()["batch_id"], outcome.batch_id)

    def test_the_history_is_kept_rather_than_replaced(self):
        self.store(proposal_id="prop-1")
        self.approve()
        self.session.query(ExecutionBatch).one().status = orders.BATCH_COMPLETED
        self.session.flush()

        self.store(proposal_id="prop-2")
        decide(proposal_id="prop-2", decision="approved", idempotency_key="key-2")

        history = orders.batches()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["proposal_id"], "prop-2")


class PostgreSQLCheckpointTest(FailureSafeTransaction, ToolTestCase):
    """The approval interrupt, on the real PostgreSQL checkpointer.

    The one thing the in-memory tests cannot check: that the paused plan is written somewhere a
    *different process* would find it. Everything else is the same, including the fact that no
    order is ever submitted.

    The threads it creates are removed afterwards. They belong to LangGraph's tables rather than
    to a migration, so there is no cascade to rely on and a test that left them behind would be
    leaving rows in the database that the application reads.
    """

    THREAD_PREFIX = "execution-test-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.checkpointer = checkpointing.start()

    def setUp(self) -> None:
        super().setUp()
        self.guard_transaction()
        if self.checkpointer is None:  # pragma: no cover - the suite needs a database
            self.skipTest("the PostgreSQL checkpointer is not available")

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
            from app.models import Holding

            self.session.add(
                Holding(
                    portfolio_id=self.portfolio.id,
                    symbol=symbol,
                    quantity=Decimal(quantity),
                    average_buy_price=Decimal("100.00"),
                    market_price=Decimal(price),
                )
            )
        self.session.flush()
        self.session.expire_all()

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

        self.proposal = a_proposal(self.portfolio, trades=a_sell_and_a_buy(), proposal_id=self._id())
        store_proposal(self.session, self.proposal)
        self.addCleanup(self._forget_threads, self.proposal["proposal_id"])

    def _id(self) -> str:
        import uuid

        return f"{self.THREAD_PREFIX}{uuid.uuid4().hex[:12]}"

    def _forget_threads(self, proposal_id: str) -> None:
        """Remove the checkpoint rows this test wrote, in the application's own database."""
        thread = execution_graph.thread_id(proposal_id)
        with SessionLocal() as session:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                session.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :thread"), {"thread": thread}
                )
            session.commit()

    def test_the_paused_plan_is_written_where_another_process_would_find_it(self):
        """A fresh graph object -- a different process, in effect -- reads the interrupt and the
        orders that were validated, and can resume from them."""
        from app.order_execution import prepare, record

        first = execution_graph.build_execution_graph(
            prepare=prepare, record=record, checkpointer=self.checkpointer
        )
        paused = execution_graph.interrupted(
            execution_graph.start(
                first,
                {
                    "proposal_id": self.proposal["proposal_id"],
                    "idempotency_key": "key-1",
                },
            )
        )
        self.assertIsNotNone(paused)

        # A second graph, built from nothing the first one held in memory.
        second = execution_graph.build_execution_graph(
            prepare=prepare, record=record, checkpointer=self.checkpointer
        )
        pending = execution_graph.pending_interrupt(second, self.proposal["proposal_id"])

        self.assertIsNotNone(pending, "the interrupt must survive the process that made it")
        self.assertEqual(
            [item["symbol"] for item in pending["plan"]["orders"]],
            [item["symbol"] for item in paused["plan"]["orders"]],
        )

        state = execution_graph.resume(second, self.proposal["proposal_id"], confirmed=True)

        self.assertEqual(state["decision_id"], self.session.query(RebalanceDecision).one().id)
        self.assertEqual(self.broker.submissions, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
