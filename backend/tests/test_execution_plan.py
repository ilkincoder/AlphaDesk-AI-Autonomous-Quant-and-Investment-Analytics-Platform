"""Resolving an approved proposal into orders, and refusing when it may not be.

Pure: a portfolio built in memory, a proposal built from it, and a broker that answers from
dictionaries. Nothing here touches a database or a network, because the question this file is
about -- may these orders be sent now, and what are they -- is arithmetic and policy.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import timedelta
from decimal import Decimal

from app.execution_plan import (
    CASH_BUFFER_MINIMUM,
    DRIFT_TOLERANCE_PERCENT,
    REASON_ACCOUNT_MISMATCH,
    REASON_CURRENCY_MISMATCH,
    REASON_FRESHNESS_UNKNOWN,
    REASON_INSUFFICIENT_CASH,
    REASON_INSUFFICIENT_SHARES,
    REASON_MARKET_CLOSED,
    REASON_PORTFOLIO_CHANGED,
    REASON_PROPOSAL_NOT_ACTIONABLE,
    REASON_RENEWAL_REQUIRED,
    REASON_SESSION_TOO_SHORT,
    REASON_TRADING_RESTRICTED,
    REASON_UNBOUNDED_PENDING_ORDER,
    REASON_UNPRICED_SYMBOL,
    PlanRefusal,
    ResolvedPlan,
    resolve,
)
from app.models import Holding, Portfolio
from tests.execution_doubles import (
    NOW,
    FakeBroker,
    a_proposal,
    a_sell_and_a_buy,
    a_trade,
    an_external_order,
)


def a_portfolio(
    *,
    cash: str = "1000.00",
    holdings: tuple[tuple[str, str, str], ...] = (
        ("AAPL", "50", "200.00"),
        ("MSFT", "10", "450.00"),
    ),
    account_id: str | None = "acct-1",
    currency: str = "USD",
) -> Portfolio:
    """The portfolio the proposal was computed from: linked to a broker, and priced.

    Built in memory rather than stored. Everything the resolution reads -- the quantities, the
    prices, the cash and the account binding -- is right here, and a database would add a
    transaction to roll back without adding a fact.
    """
    portfolio = Portfolio(
        name="Alpaca Paper",
        currency=currency,
        cash_balance=Decimal(cash),
        broker="alpaca_paper" if account_id else None,
        broker_account_id=account_id,
        broker_equity=Decimal("15500.00") if account_id else None,
        last_synced_at=NOW if account_id else None,
    )
    for symbol, quantity, price in holdings:
        portfolio.holdings.append(
            Holding(
                symbol=symbol,
                quantity=Decimal(quantity),
                average_buy_price=Decimal("100.00"),
                market_price=Decimal(price),
                market_value=(Decimal(quantity) * Decimal(price)).quantize(Decimal("0.01")),
            )
        )
    return portfolio


def a_broker(**overrides) -> FakeBroker:
    """The same account, as Alpaca reports it."""
    settings = {
        "cash": "1000.00",
        "positions": {"AAPL": ("50", "200.00"), "MSFT": ("10", "450.00")},
    }
    settings.update(overrides)
    return FakeBroker(**settings)


class ResolveTestCase(unittest.TestCase):
    def resolve(self, *, portfolio=None, broker=None, trades=None, proposal=None, open_orders=()):
        portfolio = portfolio if portfolio is not None else a_portfolio()
        broker = broker if broker is not None else a_broker()
        proposal = proposal if proposal is not None else a_proposal(
            portfolio, trades=trades if trades is not None else a_sell_and_a_buy()
        )
        return resolve(
            proposal=proposal,
            portfolio=portfolio,
            broker=broker.snapshot(),
            clock=broker.clock(),
            open_orders=open_orders,
            now=NOW,
        )

    def assertRefused(self, result, reason: str) -> PlanRefusal:
        self.assertIsInstance(result, PlanRefusal, f"expected a refusal, got {result!r}")
        self.assertEqual(result.reason, reason)
        self.assertTrue(result.detail, "a refusal must always carry a sentence to show")
        return result


class OrderResolutionTest(ResolveTestCase):
    def test_the_orders_are_the_proposal_s_trades_and_the_sales_come_first(self):
        """The sequence is what the submission order is built from, so a buy numbered before a
        sell would be a buy submitted first."""
        plan = self.resolve()

        self.assertIsInstance(plan, ResolvedPlan)
        self.assertEqual(
            [(order.symbol, order.side, order.sequence) for order in plan.orders],
            [("AAPL", "sell", 1), ("MSFT", "buy", 2)],
        )

    def test_the_quantities_are_the_proposal_s_and_the_broker_s_prices_are_the_estimate(self):
        """What was approved is the order; what the broker says right now is only the estimate
        it is priced at."""
        plan = self.resolve()

        self.assertEqual([str(order.quantity) for order in plan.orders], ["10", "2"])
        self.assertEqual([str(order.estimated_price) for order in plan.orders], ["200.00", "450.00"])
        self.assertEqual([str(order.quantity_before) for order in plan.orders], ["50", "10"])

    def test_whole_share_market_day_orders_and_nothing_else(self):
        plan = self.resolve()
        payload = plan.as_dict()

        self.assertTrue(all(set(order) >= {"symbol", "side", "quantity"} for order in payload))
        self.assertEqual(len(payload), 2)

    def test_the_plan_is_digested_and_round_trips_through_its_payload(self):
        """The digest is what the approval binds to, and the payload is what survives a restart
        in the checkpoint -- so the two have to describe the same orders."""
        plan = self.resolve()

        rebuilt = ResolvedPlan.from_payload(plan.payload())

        self.assertEqual(rebuilt.fingerprint, plan.fingerprint)
        self.assertEqual(rebuilt.as_dict(), plan.as_dict())
        self.assertEqual(rebuilt.cash_at_approval, plan.cash_at_approval)


class ActionableTest(ResolveTestCase):
    def test_a_proposal_that_proposed_nothing_cannot_be_approved(self):
        result = self.resolve(trades=[])

        self.assertRefused(result, REASON_PROPOSAL_NOT_ACTIONABLE)

    def test_a_proposal_that_failed_cannot_be_approved(self):
        portfolio = a_portfolio()
        proposal = a_proposal(portfolio, trades=a_sell_and_a_buy(), status="unavailable")

        result = self.resolve(portfolio=portfolio, proposal=proposal)

        self.assertRefused(result, REASON_PROPOSAL_NOT_ACTIONABLE)

    def test_a_proposal_still_being_generated_cannot_be_approved(self):
        portfolio = a_portfolio()
        proposal = a_proposal(portfolio, trades=a_sell_and_a_buy(), status="generating")

        result = self.resolve(portfolio=portfolio, proposal=proposal)

        self.assertRefused(result, REASON_PROPOSAL_NOT_ACTIONABLE)


class AccountTest(ResolveTestCase):
    def test_a_portfolio_bound_to_another_account_is_refused(self):
        """The credentials decide which account an order reaches, so a plan computed for one
        account must never be sent to another."""
        result = self.resolve(broker=a_broker(account_id="acct-2"))

        self.assertRefused(result, REASON_ACCOUNT_MISMATCH)

    def test_an_unlinked_portfolio_is_refused(self):
        result = self.resolve(portfolio=a_portfolio(account_id=None))

        self.assertRefused(result, REASON_ACCOUNT_MISMATCH)

    def test_a_currency_mismatch_is_refused(self):
        result = self.resolve(broker=a_broker(currency="EUR"))

        self.assertRefused(result, REASON_CURRENCY_MISMATCH)


class EligibilityTest(ResolveTestCase):
    def test_a_blocked_account_is_refused(self):
        result = self.resolve(broker=a_broker(account_blocked=True))

        self.assertRefused(result, REASON_TRADING_RESTRICTED)

    def test_an_account_with_trading_blocked_is_refused(self):
        result = self.resolve(broker=a_broker(trading_blocked=True))

        self.assertRefused(result, REASON_TRADING_RESTRICTED)

    def test_an_account_suspended_by_its_owner_is_refused(self):
        result = self.resolve(broker=a_broker(trade_suspended_by_user=True))

        self.assertRefused(result, REASON_TRADING_RESTRICTED)

    def test_an_account_that_is_not_active_is_refused(self):
        result = self.resolve(broker=a_broker(status="ACCOUNT_UPDATED"))

        self.assertRefused(result, REASON_TRADING_RESTRICTED)


class SessionTest(ResolveTestCase):
    def test_a_closed_market_is_refused_and_nothing_is_queued(self):
        result = self.resolve(broker=a_broker(is_open=False))

        refusal = self.assertRefused(result, REASON_MARKET_CLOSED)
        self.assertIn("never queued overnight", refusal.detail)

    def test_too_little_of_the_session_left_is_refused(self):
        """A DAY order sent in the last minutes expires unfilled, which looks like a failure and
        is really the wrong time of day to have asked."""
        result = self.resolve(broker=a_broker(next_close=NOW + timedelta(seconds=30)))

        self.assertRefused(result, REASON_SESSION_TOO_SHORT)


class FreshnessTest(ResolveTestCase):
    def test_a_portfolio_that_moved_is_refused(self):
        """The arithmetic was computed against a different portfolio, so regenerating is the only
        honest answer. Nothing is resized to fit."""
        portfolio = a_portfolio()
        proposal = a_proposal(portfolio, trades=a_sell_and_a_buy())
        portfolio.holdings[0].quantity = Decimal("48")
        portfolio.holdings[0].market_price = Decimal("200.00")

        result = self.resolve(portfolio=portfolio, proposal=proposal)

        self.assertRefused(result, REASON_PORTFOLIO_CHANGED)

    def test_a_cash_change_alone_is_a_changed_portfolio(self):
        portfolio = a_portfolio()
        proposal = a_proposal(portfolio, trades=a_sell_and_a_buy())
        portfolio.cash_balance = Decimal("5000.00")

        result = self.resolve(portfolio=portfolio, proposal=proposal)

        self.assertRefused(result, REASON_PORTFOLIO_CHANGED)

    def test_a_price_only_change_is_not_a_changed_portfolio(self):
        """A price that ticked is handled by the drift check, which is a much smaller claim."""
        portfolio = a_portfolio()
        proposal = a_proposal(portfolio, trades=a_sell_and_a_buy())
        portfolio.holdings[0].market_price = Decimal("201.00")

        result = self.resolve(portfolio=portfolio, proposal=proposal)

        self.assertIsInstance(result, ResolvedPlan)

    def test_a_proposal_with_no_snapshot_is_refused_rather_than_assumed_current(self):
        portfolio = a_portfolio()
        proposal = a_proposal(portfolio, trades=a_sell_and_a_buy())
        proposal["snapshot"] = {}

        result = self.resolve(portfolio=portfolio, proposal=proposal)

        self.assertRefused(result, REASON_FRESHNESS_UNKNOWN)

    def test_a_symbol_the_broker_cannot_price_is_refused(self):
        result = self.resolve(broker=a_broker(positions={"AAPL": ("50", "200.00")}))

        self.assertRefused(result, REASON_UNPRICED_SYMBOL)


class SharesTest(ResolveTestCase):
    def test_a_sale_larger_than_the_holding_is_refused(self):
        result = self.resolve(trades=[a_trade("AAPL", "sell", "60", "200.00")])

        refusal = self.assertRefused(result, REASON_INSUFFICIENT_SHARES)
        self.assertIn("not resized", refusal.detail)

    def test_shares_already_committed_to_a_working_sell_are_not_available(self):
        """An order placed directly in Alpaca has claimed the shares it would deliver, and the
        account does not distinguish it from one placed here."""
        result = self.resolve(
            trades=[a_trade("AAPL", "sell", "10", "200.00")],
            open_orders=[an_external_order("their-sell", "AAPL", "sell", "45")],
        )

        self.assertRefused(result, REASON_INSUFFICIENT_SHARES)


class CashTest(ResolveTestCase):
    def test_a_purchase_beyond_the_cash_is_refused(self):
        result = self.resolve(
            trades=[a_trade("MSFT", "buy", "10", "450.00", quantity_before="10")],
            broker=a_broker(cash="2000.00"),
        )

        refusal = self.assertRefused(result, REASON_INSUFFICIENT_CASH)
        self.assertIn("Buying power is deliberately not used", refusal.detail)

    def test_a_purchase_is_funded_by_the_plan_s_own_sale_proceeds(self):
        """The calculation already said the purchase depended on the sale, and the plan is
        checked against the same arithmetic -- then the cash is revalidated before each buy."""
        plan = self.resolve(
            trades=[
                a_trade("AAPL", "sell", "10", "200.00"),
                a_trade("MSFT", "buy", "2", "450.00", quantity_before="10"),
            ],
            broker=a_broker(cash="100.00"),
        )

        self.assertIsInstance(plan, ResolvedPlan)

    def test_cash_already_committed_to_a_working_buy_is_not_spendable(self):
        result = self.resolve(
            trades=[a_trade("MSFT", "buy", "2", "450.00", quantity_before="10")],
            broker=a_broker(cash="1000.00"),
            open_orders=[an_external_order("their-buy", "MSFT", "buy", "2", limit_price="450.00")],
        )

        self.assertRefused(result, REASON_INSUFFICIENT_CASH)

    def test_a_working_market_buy_with_no_price_to_bound_it_is_refused(self):
        """Its cost is genuinely unknown, and an unknown cost is not a cost of zero."""
        result = self.resolve(
            open_orders=[an_external_order("their-buy", "TSLA", "buy", "3")],
        )

        self.assertRefused(result, REASON_UNBOUNDED_PENDING_ORDER)

    def test_the_buffer_is_two_percent_of_what_the_buys_will_cost(self):
        """A market order is not a guaranteed price, so the plan may not spend to the last cent.

        Two percent of 450.00 is 9.00, so an account holding 459.00 can buy 450.00 of stock and
        one holding 458.99 cannot -- which is the buffer doing exactly what it says.
        """
        trades = [a_trade("MSFT", "buy", "1", "450.00", quantity_before="10")]

        plan = self.resolve(trades=trades, broker=a_broker(cash="459.00"))

        self.assertIsInstance(plan, ResolvedPlan)
        self.assertEqual(plan.reserved_cash, Decimal("9.00"))

        refused = self.resolve(trades=trades, broker=a_broker(cash="458.99"))
        self.assertRefused(refused, REASON_INSUFFICIENT_CASH)

    def test_a_small_purchase_keeps_a_whole_dollar_back_rather_than_two_percent(self):
        """Two percent of a forty dollar buy is eighty cents, which is no protection at all."""
        portfolio = a_portfolio(holdings=(("ZED", "100", "10.00"),))
        trades = [a_trade("ZED", "buy", "4", "10.00", quantity_before="100")]

        plan = self.resolve(
            portfolio=portfolio,
            trades=trades,
            broker=a_broker(cash="41.00", positions={"ZED": ("100", "10.00")}),
        )

        self.assertIsInstance(plan, ResolvedPlan)
        self.assertEqual(plan.reserved_cash, CASH_BUFFER_MINIMUM)

        refused = self.resolve(
            portfolio=portfolio,
            trades=trades,
            broker=a_broker(cash="40.99", positions={"ZED": ("100", "10.00")}),
        )
        self.assertRefused(refused, REASON_INSUFFICIENT_CASH)


class DriftTest(ResolveTestCase):
    def test_a_move_inside_the_tolerance_proceeds_and_is_recorded(self):
        """The figure a person approved is still the figure they are getting, near enough that a
        second click would be ceremony."""
        result = self.resolve(broker=a_broker(positions={
            "AAPL": ("50", "201.00"),
            "MSFT": ("10", "450.00"),
        }))

        self.assertIsInstance(result, ResolvedPlan)
        self.assertLessEqual(result.price_drift_percent, DRIFT_TOLERANCE_PERCENT)
        self.assertTrue(
            any("largest move" in note for note in result.notes),
            "the drift is disclosed in the plan's own notes",
        )

    def test_a_move_beyond_the_tolerance_requires_renewal_and_shows_the_refreshed_estimates(self):
        """Nothing is sent under an approval that describes a different price, and nothing is
        silently re-priced: the refreshed plan goes back to the person."""
        result = self.resolve(broker=a_broker(positions={
            "AAPL": ("50", "250.00"),
            "MSFT": ("10", "450.00"),
        }))

        refusal = self.assertRefused(result, REASON_RENEWAL_REQUIRED)
        self.assertTrue(refusal.is_renewal)
        self.assertIsNotNone(refusal.plan, "a renewal shows the orders it is asking about")
        self.assertEqual(
            [str(order.estimated_price) for order in refusal.plan.orders],
            ["250.00", "450.00"],
        )
        self.assertIn("nothing has been sent", refusal.detail)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
