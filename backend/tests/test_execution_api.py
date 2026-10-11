"""The decision and Orders routes: what they answer, and what they refuse to accept.

The real route functions against the real database, with the broker scripted. The route functions
are called directly rather than through a `TestClient`, which is how the rest of this suite tests
handlers.

Two things this file is really about. **What a client may send**: the request schema forbids
everything it does not define, so a body carrying a quantity is refused rather than quietly
ignored. And **what a client may read**: an id that is not this portfolio's is answered as not
found, and no route takes an account, a symbol or a quantity from the caller.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError

from app import orders
from app.execution_api import get_batch, get_orders, post_decision
from app.order_execution import decide
from app.models import ExecutionBatch, Portfolio
from app.schemas import ProposalDecisionRequest
from tests.conversation_doubles import SessionProxy
from tests.execution_doubles import (
    NOW,
    FailureSafeTransaction,
    FakeBroker,
    a_proposal,
    a_sell_and_a_buy,
    execution_sessions,
    fake_broker,
    fake_portfolio_sync,
    store_proposal,
)
from tests.test_tools_contracts import ToolTestCase


class ApiTestCase(FailureSafeTransaction, ToolTestCase):
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

        from app.agent import checkpointing

        self.saver = mock.patch(
            "app.agent.checkpointing.get_checkpointer",
            return_value=checkpointing.in_memory(),
        )
        self.saver.start()
        self.addCleanup(self.saver.stop)

        # **The route reads the wall clock; this suite's broker answers from a canned one.**
        # Binding the two together is what makes the session check a comparison between two
        # agreed instants rather than between a day in September and whatever today is. Only the
        # clock is supplied -- the real `decide` still runs, so the route is still being driven
        # through the code it actually calls.
        #
        # The instant is read from `self.clock` on every call rather than bound once, so a test
        # that is *about* time -- two approvals, newest first -- can move it. Every other test
        # leaves it alone and gets the fixture's instant.
        self.clock = NOW
        clock = mock.patch(
            "app.execution_api.decide", lambda **kwargs: decide(now=self.clock, **kwargs)
        )
        clock.start()
        self.addCleanup(clock.stop)

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

    def store(self, proposal_id: str = "prop-1") -> None:
        store_proposal(
            self.session,
            a_proposal(self.portfolio, trades=a_sell_and_a_buy(), proposal_id=proposal_id),
        )

    def decide(self, *, proposal_id: str = "prop-1", **overrides):
        body = {"decision": "approve", "idempotency_key": "key-1", **overrides}
        return post_decision(proposal_id, ProposalDecisionRequest(**body))

    def body(self, response) -> dict:
        return json.loads(response.body)


class ApproveRouteTest(ApiTestCase):
    def test_approving_answers_with_the_batch_the_orders_page_should_open(self):
        self.store()

        response = self.decide()

        self.assertEqual(response.status_code, 200)
        payload = self.body(response)
        self.assertEqual(payload["outcome"], "approved")
        self.assertEqual(payload["decision"]["decision"], "approved")
        self.assertIsNotNone(payload["batch_id"])
        self.assertEqual(
            [item["symbol"] for item in payload["plan"]], ["AAPL", "MSFT"]
        )
        self.assertEqual([item["side"] for item in payload["plan"]], ["sell", "buy"])
        self.assertEqual(self.broker.submissions, [])

    def test_the_response_carries_no_account_identifier_and_no_credentials(self):
        self.store()

        payload = self.body(self.decide())

        rendered = json.dumps(payload)
        self.assertNotIn("acct-1", rendered)
        self.assertNotIn("broker_account_id", rendered)
        self.assertNotIn("client_order_id", rendered)

    def test_rejecting_answers_with_the_decision_and_no_batch(self):
        self.store()

        response = self.decide(decision="reject", reason="Not now.")

        payload = self.body(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["outcome"], "rejected")
        self.assertIsNone(payload["batch_id"])
        self.assertEqual(payload["decision"]["reason"], "Not now.")
        self.assertEqual(self.session.query(ExecutionBatch).count(), 0)

    def test_a_second_click_is_answered_from_the_decision_that_exists(self):
        self.store()
        first = self.body(self.decide())

        second = self.decide(idempotency_key="key-2")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.body(second)["outcome"], "already_decided")
        self.assertEqual(self.body(second)["batch_id"], first["batch_id"])

    def test_a_conflicting_decision_is_a_conflict(self):
        self.store()
        self.decide(decision="reject")

        conflict = self.decide(idempotency_key="key-2")

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.body(conflict)["reason"], "decision_conflict")

    def test_a_proposal_that_is_not_stored_is_a_404(self):
        response = self.decide()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.body(response)["reason"], "proposal_not_found")

    def test_a_market_that_is_closed_is_a_400_with_a_sentence(self):
        self.store()
        self.broker.is_open = False

        response = self.decide()

        self.assertEqual(response.status_code, 400)
        payload = self.body(response)
        self.assertEqual(payload["reason"], "market_closed")
        self.assertIn("closed", payload["detail"])

    def test_a_session_with_too_little_left_is_a_400(self):
        """The close is measured against the clock the decision was made at.

        The fixture's broker closes an hour after its own instant, so this refusal only happens
        if the injected clock is the one `prepare` compares against. Read from the wall clock
        instead, the subtraction would be about a session that ended months ago -- and a plan
        approved in the last minutes before a close is a DAY order sent to expire unfilled.
        """
        self.store()
        self.broker.next_close = NOW + timedelta(seconds=30)

        response = self.decide()

        self.assertEqual(response.status_code, 400)
        payload = self.body(response)
        self.assertEqual(payload["reason"], "session_too_short")
        self.assertIsNone(payload["batch_id"])
        self.assertEqual(self.session.query(ExecutionBatch).count(), 0)

    def test_an_open_market_approves_and_dates_the_decision_at_the_request_s_own_clock(self):
        """Market open, and the instant the decision carries is the one it was judged at.

        Both halves are the fix. `prepare` approves because an hour of the fixture's session is
        left, and `record` stamps `decided_at` with that same instant -- one answer to "what time
        is it" rather than two. A decision dated at some other moment would be one no reader
        could reproduce from the record.
        """
        self.store()

        response = self.decide()

        self.assertEqual(response.status_code, 200)
        payload = self.body(response)
        self.assertEqual(payload["outcome"], "approved")
        self.assertEqual(datetime.fromisoformat(payload["decision"]["decided_at"]), NOW)

    def test_an_unreachable_broker_is_reported_as_not_now(self):
        from app.alpaca import ProviderUnavailableError

        self.store()

        with mock.patch(
            "app.order_execution.fetch_snapshot",
            side_effect=ProviderUnavailableError("Alpaca did not respond in time."),
        ):
            response = self.decide()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.body(response)["reason"], "broker_unreachable")

    def test_a_plan_whose_prices_moved_comes_back_for_a_second_confirmation(self):
        self.store()
        self.broker.positions["AAPL"] = ("50", "250.00")

        first = self.body(self.decide())

        self.assertEqual(first["outcome"], "renewal_required")
        self.assertEqual(
            [item["estimated_price"] for item in first["plan"]], ["250.00", "450.00"]
        )

        confirmed = self.body(self.decide(idempotency_key="key-2", confirm=True))

        self.assertEqual(confirmed["outcome"], "approved")
        self.assertEqual(
            [item["symbol"] for item in confirmed["plan"]],
            [item["symbol"] for item in first["plan"]],
        )


class RequestSchemaTest(unittest.TestCase):
    def test_a_body_carrying_a_quantity_is_refused_rather_than_ignored(self):
        """`extra="forbid"` doing real work: a client cannot send a trade, only a decision."""
        for forbidden in ({"quantity": "10"}, {"symbol": "AAPL"}, {"side": "buy"}, {"price": "1"}):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(ValidationError):
                    ProposalDecisionRequest(
                        decision="approve", idempotency_key="key-1", **forbidden
                    )

    def test_only_the_two_decisions_are_accepted(self):
        for word in ("approve", "reject"):
            ProposalDecisionRequest(decision=word, idempotency_key="key-1")

        for word in ("approved", "sell", "", "APPROVE"):
            with self.subTest(word=word):
                with self.assertRaises(ValidationError):
                    ProposalDecisionRequest(decision=word, idempotency_key="key-1")

    def test_an_idempotency_key_is_required(self):
        with self.assertRaises(ValidationError):
            ProposalDecisionRequest(decision="approve", idempotency_key="")

    def test_the_confirm_flag_defaults_to_false(self):
        """A first approval must not have to claim it is a confirmation."""
        parsed = ProposalDecisionRequest(decision="approve", idempotency_key="key-1")
        self.assertFalse(parsed.confirm)


class OrdersRouteTest(ApiTestCase):
    def test_an_empty_orders_page_says_nothing_was_submitted(self):
        view = get_orders()

        self.assertEqual(view.batches, [])
        self.assertIsNone(view.active_batch_id)

    def test_a_recorded_batch_is_listed_with_its_orders(self):
        self.store()
        batch_id = self.body(self.decide())["batch_id"]

        view = get_orders()

        self.assertEqual(view.active_batch_id, batch_id)
        self.assertEqual(len(view.batches), 1)
        batch = view.batches[0]
        self.assertEqual(batch.batch_id, batch_id)
        self.assertEqual(batch.proposal_id, "prop-1")
        self.assertEqual(batch.status, orders.BATCH_PENDING)
        self.assertEqual(batch.sync_status, orders.SYNC_PENDING)
        self.assertEqual([item.symbol for item in batch.orders], ["AAPL", "MSFT"])
        self.assertEqual([item.side for item in batch.orders], ["sell", "buy"])
        self.assertEqual([item.status for item in batch.orders], ["planned", "planned"])
        self.assertTrue(batch.notes)

    def test_the_estimates_and_the_actual_fill_are_separate_fields(self):
        """The page has to distinguish them, so the record must not blur them into one number."""
        self.store()
        batch_id = self.body(self.decide())["batch_id"]

        order = get_batch(batch_id).orders[0]

        self.assertEqual(order.estimated_price, Decimal("200.00"))
        self.assertEqual(order.filled_quantity, Decimal("0"))
        self.assertIsNone(order.filled_avg_price)

    def test_a_batch_that_is_not_this_portfolio_s_is_not_found(self):
        self.store()
        self.body(self.decide())

        with self.assertRaises(HTTPException) as raised:
            get_batch("00000000000000000000000000000000")

        self.assertEqual(raised.exception.status_code, 404)

    def test_a_batch_belonging_to_another_portfolio_is_not_served(self):
        """This application has one portfolio. An id that is not its own is not something it can
        confirm exists, so it is a 404 rather than a 403."""
        self.store()
        batch_id = self.body(self.decide())["batch_id"]

        other = Portfolio(name="Someone Else", currency="USD", cash_balance=Decimal("1.00"))
        self.session.add(other)
        self.session.flush()
        self.session.query(ExecutionBatch).one().portfolio_id = other.id
        self.session.flush()
        self.session.expire_all()

        with self.assertRaises(HTTPException) as raised:
            get_batch(batch_id)

        self.assertEqual(raised.exception.status_code, 404)

    def test_history_is_kept_and_newest_is_first(self):
        self.store("prop-1")
        self.body(self.decide(proposal_id="prop-1"))
        self.session.query(ExecutionBatch).one().status = orders.BATCH_COMPLETED
        self.session.flush()

        # A minute later, because "newest first" is a claim about two approvals made at
        # different times. Two sharing one instant would be a tie, and what a tie falls back to
        # is a uuid comparison rather than anything about which was agreed to first.
        self.clock = NOW + timedelta(minutes=1)
        self.store("prop-2")
        self.body(self.decide(proposal_id="prop-2", idempotency_key="key-2"))

        view = get_orders()

        self.assertEqual([item.proposal_id for item in view.batches], ["prop-2", "prop-1"])
        self.assertEqual(view.active_batch_id, view.batches[0].batch_id)

    def test_the_proposal_endpoint_reports_the_decision_and_the_batch(self):
        """A page restoring itself reads the proposal and needs to know it was already decided."""
        from app.rebalance_api import get_proposal

        self.store()
        batch_id = self.body(self.decide())["batch_id"]

        view = get_proposal()

        self.assertEqual(view.proposal.decision.decision, "approved")
        self.assertEqual(view.proposal.batch_id, batch_id)
        # The generation status is untouched: a decision is not a stage of the run.
        self.assertEqual(view.proposal.status, "proposed")

    def test_a_rejection_is_visible_on_the_proposal_after_a_reload(self):
        from app.rebalance_api import get_proposal

        self.store()
        self.decide(decision="reject")

        view = get_proposal()

        self.assertEqual(view.proposal.decision.decision, "rejected")
        self.assertIsNone(view.proposal.batch_id)

    def test_an_undecided_proposal_reports_no_decision(self):
        from app.rebalance_api import get_proposal

        self.store()

        view = get_proposal()

        self.assertIsNone(view.proposal.decision)
        self.assertIsNone(view.proposal.batch_id)


class PersistenceAcrossSessionsTest(ApiTestCase):
    def test_the_decision_and_its_orders_are_readable_by_a_later_request(self):
        """Nothing crosses the store's boundary as an ORM instance, so a reader in a later
        session -- which is every request -- sees the same record."""
        self.store()
        response = self.decide()

        self.session.expire_all()

        view = get_orders()
        batch = view.batches[0]

        self.assertEqual(batch.batch_id, self.body(response)["batch_id"])
        self.assertEqual(batch.proposal_id, "prop-1")
        self.assertEqual([item.quantity for item in batch.orders], [Decimal("10"), Decimal("2")])
        self.assertEqual(batch.orders[0].client_order_id, orders.client_order_id(batch.batch_id, 1))
        self.assertEqual(view.active_batch_id, batch.batch_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
