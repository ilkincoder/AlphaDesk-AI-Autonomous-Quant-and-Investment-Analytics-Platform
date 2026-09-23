"""Tests for the rebalance calculation.

Run from the backend directory (inside the container, /app):

    python -m unittest discover -s tests -t .

No database, no HTTP, no model. `calculate_rebalance` is pure, so these run in milliseconds.
What they are for is the arithmetic no model is trusted with: which way a quantity rounds, what
a sale funds, and which requests are refused rather than quietly repaired.
"""

import unittest
from dataclasses import dataclass
from decimal import Decimal

from app.rebalance import (
    REASON_INCOMPLETE_TARGETS,
    REASON_INVALID_PRICE,
    REASON_INVALID_WEIGHT,
    REASON_NEGATIVE_CASH,
    REASON_NO_HOLDINGS,
    REASON_TARGETS_EXCEED_TOTAL,
    REASON_UNFUNDABLE,
    REASON_UNKNOWN_TARGET,
    REASON_UNPRICED_HOLDINGS,
    REASON_ZERO_VALUE,
    Action,
    Outcome,
    RebalanceUnavailable,
    calculate_rebalance,
)


@dataclass(frozen=True)
class StubHolding:
    """The smallest thing that satisfies the PricedHolding protocol.

    No purchase price and no market price, deliberately: this calculation is never allowed to
    fall back to either, and a stub that carried them would let that mistake pass.
    """

    symbol: str
    quantity: Decimal


def holding(symbol: str, quantity: str) -> StubHolding:
    return StubHolding(symbol=symbol, quantity=Decimal(quantity))


PRICES = {
    "AAPL": Decimal("200.00"),
    "MSFT": Decimal("100.00"),
    "NVDA": Decimal("50.00"),
}

# One holding and a hundred dollars a share, so every figure below is checkable by hand.
FLAT = {"AAPL": Decimal("100.00")}


class RoundingTest(unittest.TestCase):
    """A buy rounds down and a sell rounds up. Both directions, and why."""

    def test_a_buy_rounds_down(self):
        """10 shares at 100 plus 1,000 cash is 2,000. A target of 58 percent is 1,160, which is
        11.6 shares -- so 1 whole share is bought, not 2. Rounding to the nearest would spend
        cash the target did not allow."""
        result = calculate_rebalance(
            [holding("AAPL", "10")], Decimal("1000.00"), FLAT, {"AAPL": Decimal("0.58")}
        )

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.action, Action.BUY)
        self.assertEqual(trade.quantity, Decimal("1"))
        self.assertEqual(trade.estimated_value, Decimal("100.00"))
        self.assertEqual(result.cash_after, Decimal("900.00"))

    def test_a_sell_rounds_up(self):
        """The mirror: a target of 42 percent is 8.4 shares of the same position, and 2 are
        sold, not 1. A sale is never smaller than the target asked for, which is what keeps a
        sale-funded purchase from falling short."""
        result = calculate_rebalance(
            [holding("AAPL", "10")], Decimal("1000.00"), FLAT, {"AAPL": Decimal("0.42")}
        )

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.action, Action.SELL)
        self.assertEqual(trade.quantity, Decimal("2"))
        self.assertEqual(trade.estimated_value, Decimal("200.00"))
        self.assertEqual(result.cash_after, Decimal("1200.00"))

    def test_a_fractional_remainder_is_kept_rather_than_rounded_away(self):
        """10.5 shares can sell 10. The 0.5 is not a share, is not sold, and is not treated as
        zero -- it stays on the position."""
        result = calculate_rebalance(
            [holding("AAPL", "10.5")], Decimal("0"), FLAT, {"AAPL": Decimal("0")}
        )

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.action, Action.SELL)
        self.assertEqual(trade.quantity, Decimal("10"))
        self.assertEqual(trade.quantity_after, Decimal("0.5"))

    def test_a_target_half_a_share_away_produces_no_trade(self):
        """10 shares at 100 plus 400 cash is 1,400. A target of 75 percent is 1,050 -- half a
        share more than is held, which is a purchase of zero whole shares."""
        result = calculate_rebalance(
            [holding("AAPL", "10")], Decimal("400.00"), FLAT, {"AAPL": Decimal("0.75")}
        )

        self.assertEqual(result.outcome, Outcome.NO_CHANGE)
        self.assertEqual(result.trades, ())
        self.assertTrue(
            any("No change is recommended" in note for note in result.limitations)
        )

    def test_an_exact_target_produces_no_trade(self):
        result = calculate_rebalance(
            [holding("AAPL", "50")], Decimal("0"), PRICES, {"AAPL": Decimal("1")}
        )

        self.assertEqual(result.outcome, Outcome.NO_CHANGE)
        self.assertEqual(result.after.total_value, result.before.total_value)


class CashTest(unittest.TestCase):
    """What the trades do to cash, and the one case where they cannot be afforded."""

    def test_a_sale_funds_a_purchase_and_the_dependency_is_reported(self):
        """AAPL 50 at 200 and MSFT 100 at 100, plus 100 of cash, is 20,100. Moving to 20/80
        sells 30 AAPL and buys 60 MSFT -- and the 6,000 of purchases is far more than the 100
        of cash, so it depends on the sale."""
        result = calculate_rebalance(
            [holding("AAPL", "50"), holding("MSFT", "100")],
            Decimal("100.00"),
            PRICES,
            {"AAPL": Decimal("0.2"), "MSFT": Decimal("0.8")},
        )

        sold = {trade.symbol for trade in result.trades if trade.action is Action.SELL}
        bought = {trade.symbol for trade in result.trades if trade.action is Action.BUY}
        self.assertEqual(sold, {"AAPL"})
        self.assertEqual(bought, {"MSFT"})
        self.assertEqual(result.sell_proceeds, Decimal("6000.00"))
        self.assertEqual(result.buy_cost, Decimal("6000.00"))
        self.assertTrue(result.buys_depend_on_sells)
        self.assertEqual(result.cash_after, Decimal("100.00"))
        self.assertTrue(
            any("depend on the proposed sales" in note for note in result.limitations)
        )

    def test_cash_alone_funding_a_purchase_is_not_reported_as_depending_on_sales(self):
        """A purchase the account's own cash exactly covers does not depend on a sale."""
        result = calculate_rebalance(
            [holding("AAPL", "50")], Decimal("5000.00"), PRICES, {"AAPL": Decimal("1")}
        )

        self.assertEqual(result.buy_cost, Decimal("5000.00"))
        self.assertFalse(result.buys_depend_on_sells)
        self.assertEqual(result.cash_after, Decimal("0.00"))
        self.assertTrue(
            all("depend on the proposed sales" not in note for note in result.limitations)
        )

    def test_a_fractional_clamp_can_break_funding_and_is_then_refused(self):
        """The clamp is what makes the cash check necessary rather than decorative.

        10.5 AAPL at 200 and 20 MSFT at 100 is 4,100. Asking for all of it in MSFT sells AAPL
        and buys 21 MSFT. The sale wants 10.5 shares and can only make 10, so it raises 2,000
        against a 2,100 purchase -- and the account would be 100 short.
        """
        with self.assertRaises(RebalanceUnavailable) as caught:
            calculate_rebalance(
                [holding("AAPL", "10.5"), holding("MSFT", "20")],
                Decimal("0"),
                PRICES,
                {"AAPL": Decimal("0"), "MSFT": Decimal("1")},
            )

        self.assertEqual(caught.exception.reason, REASON_UNFUNDABLE)
        self.assertIn("Nothing was adjusted to make it fit", caught.exception.detail)

    def test_a_pure_sale_is_always_funded(self):
        """The other side of the same rule: a plan that only sells can never take cash below
        zero, however the rounding lands."""
        result = calculate_rebalance(
            [holding("AAPL", "1.2")], Decimal("0"), PRICES, {"AAPL": Decimal("0")}
        )

        self.assertEqual([trade.action for trade in result.trades], [Action.SELL])
        self.assertEqual(result.buy_cost, Decimal("0"))
        self.assertFalse(result.buys_depend_on_sells)
        self.assertEqual(result.cash_after, result.sell_proceeds)


class RefusalTest(unittest.TestCase):
    """Every state this workflow cannot express, refused by name."""

    def assertRefused(self, reason: str, *args, **kwargs):  # noqa: N802 - unittest style
        with self.assertRaises(RebalanceUnavailable) as caught:
            calculate_rebalance(*args, **kwargs)
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def test_no_holdings(self):
        detail = self.assertRefused(REASON_NO_HOLDINGS, [], Decimal("1000"), {}, {})
        self.assertIn("cash-only", detail.detail)

    def test_negative_cash_is_unsupported_by_a_cash_only_workflow(self):
        """A margin account's negative cash is a stored fact. This workflow has no way to
        represent a balance, so it says so instead of proposing trades against one."""
        detail = self.assertRefused(
            REASON_NEGATIVE_CASH,
            [holding("AAPL", "10")],
            Decimal("-500.00"),
            PRICES,
            {"AAPL": Decimal("1")},
        )
        self.assertIn("margin", detail.detail)

    def test_an_unpriced_holding(self):
        self.assertRefused(
            REASON_UNPRICED_HOLDINGS,
            [holding("AAPL", "10")],
            Decimal("0"),
            {},
            {"AAPL": Decimal("1")},
        )

    def test_a_price_that_is_not_finite_and_positive(self):
        for price in (Decimal("0"), Decimal("-5"), Decimal("NaN")):
            with self.subTest(price=price):
                self.assertRefused(
                    REASON_INVALID_PRICE,
                    [holding("AAPL", "10")],
                    Decimal("0"),
                    {"AAPL": price},
                    {"AAPL": Decimal("1")},
                )

    def test_a_target_for_a_symbol_that_is_not_held(self):
        detail = self.assertRefused(
            REASON_UNKNOWN_TARGET,
            [holding("AAPL", "10")],
            Decimal("0"),
            PRICES,
            {"AAPL": Decimal("0.5"), "TSLA": Decimal("0.5")},
        )
        self.assertIn("never opens a new position", detail.detail)

    def test_a_held_symbol_with_no_target(self):
        """The refusal that stops a truncated reply liquidating a position by omission."""
        self.assertRefused(
            REASON_INCOMPLETE_TARGETS,
            [holding("AAPL", "10"), holding("MSFT", "10")],
            Decimal("0"),
            PRICES,
            {"AAPL": Decimal("1")},
        )

    def test_a_weight_outside_zero_to_one(self):
        for weight in (Decimal("-0.1"), Decimal("1.5"), Decimal("NaN")):
            with self.subTest(weight=weight):
                self.assertRefused(
                    REASON_INVALID_WEIGHT,
                    [holding("AAPL", "10")],
                    Decimal("0"),
                    PRICES,
                    {"AAPL": weight},
                )

    def test_weights_that_add_up_to_more_than_the_portfolio(self):
        detail = self.assertRefused(
            REASON_TARGETS_EXCEED_TOTAL,
            [holding("AAPL", "10"), holding("MSFT", "10")],
            Decimal("0"),
            PRICES,
            {"AAPL": Decimal("0.7"), "MSFT": Decimal("0.5")},
        )
        self.assertIn("refused rather than scaled down", detail.detail)

    def test_a_portfolio_worth_nothing(self):
        """A percentage of zero is undefined, so there is no allocation to move toward."""
        self.assertRefused(
            REASON_ZERO_VALUE,
            [holding("AAPL", "0")],
            Decimal("0"),
            PRICES,
            {"AAPL": Decimal("1")},
        )


class AllocationTest(unittest.TestCase):
    """The two sides, and the numbers the page draws."""

    def test_before_matches_the_valuation_the_portfolio_page_shows(self):
        """The current allocation is the existing calculation's, over the existing basis --
        the broker's own equity as the denominator when there is one."""
        result = calculate_rebalance(
            [holding("AAPL", "50"), holding("MSFT", "10")],
            Decimal("1000.00"),
            PRICES,
            {"AAPL": Decimal("0.5"), "MSFT": Decimal("0.5")},
            reported_total_value=Decimal("12000.00"),
        )

        self.assertEqual(result.before.total_value, Decimal("12000.00"))
        by_symbol = {item.symbol: item for item in result.before.holdings}
        self.assertEqual(by_symbol["AAPL"].allocation_percent, Decimal("83.33"))
        self.assertEqual(result.before.cash_allocation_percent, Decimal("8.33"))

    def test_the_after_side_is_computed_from_holdings_and_cash(self):
        """The broker's equity describes the account as it is, so it cannot be the denominator
        of a portfolio that does not exist yet."""
        result = calculate_rebalance(
            [holding("AAPL", "50"), holding("MSFT", "10")],
            Decimal("1000.00"),
            PRICES,
            {"AAPL": Decimal("0.5"), "MSFT": Decimal("0.5")},
            reported_total_value=Decimal("12000.00"),
        )

        after_total = sum(
            (item.holding_value for item in result.after.holdings), start=Decimal("0")
        ) + result.after.cash_balance
        self.assertEqual(result.after.total_value, after_total.quantize(Decimal("0.01")))

    def test_the_two_bases_are_reconciled_rather_than_smoothed_over(self):
        result = calculate_rebalance(
            [holding("AAPL", "50")],
            Decimal("1000.00"),
            PRICES,
            {"AAPL": Decimal("1")},
            reported_total_value=Decimal("10500.00"),
        )

        self.assertEqual(result.reconciliation.reported_total_value, Decimal("10500.00"))
        self.assertEqual(result.reconciliation.summed_total_value, Decimal("11000.00"))
        self.assertEqual(result.reconciliation.difference, Decimal("-500.00"))
        self.assertTrue(
            any("differ" in note for note in result.limitations),
            "a reconciliation gap has to reach the reader",
        )

    def test_no_broker_equity_means_there_is_nothing_to_reconcile(self):
        result = calculate_rebalance(
            [holding("AAPL", "50")], Decimal("0"), PRICES, {"AAPL": Decimal("1")}
        )

        self.assertIsNone(result.reconciliation.reported_total_value)
        self.assertIsNone(result.reconciliation.difference)

    def test_the_largest_position_is_reported_on_both_sides(self):
        result = calculate_rebalance(
            [holding("AAPL", "50"), holding("MSFT", "10")],
            Decimal("0"),
            PRICES,
            {"AAPL": Decimal("0.2"), "MSFT": Decimal("0.8")},
        )

        self.assertEqual(result.largest_before.symbol, "AAPL")
        self.assertEqual(result.largest_after.symbol, "MSFT")
        self.assertEqual(result.largest_after.allocation_percent, Decimal("80.00"))

    def test_the_scenario_is_the_disclosed_assumption_on_the_largest_holding(self):
        """The shock is a constant of this module applied to the largest position, priced by
        the existing scenario calculation -- never a number a model produced."""
        result = calculate_rebalance(
            [holding("AAPL", "50"), holding("MSFT", "10")],
            Decimal("1000.00"),
            PRICES,
            {"AAPL": Decimal("0.9"), "MSFT": Decimal("0.09")},
        )

        self.assertEqual(result.scenario.symbol, "AAPL")
        self.assertEqual(result.scenario.price_before, Decimal("200.00"))
        self.assertEqual(result.scenario.price_after, Decimal("180.00"))
        self.assertEqual(result.scenario.total_value_before, Decimal("12000.00"))
        self.assertEqual(result.scenario.total_value_after, Decimal("11000.00"))

    def test_the_target_weights_are_carried_back_for_the_reader(self):
        result = calculate_rebalance(
            [holding("AAPL", "50"), holding("MSFT", "10")],
            Decimal("1000"),
            PRICES,
            {"AAPL": Decimal("0.8"), "MSFT": Decimal("0.2")},
        )

        self.assertEqual(result.targets, {"AAPL": Decimal("0.8"), "MSFT": Decimal("0.2")})
        self.assertEqual(result.cash_target, Decimal("0.00"))

    def test_every_proposal_says_it_was_never_executed(self):
        for targets in ({"AAPL": Decimal("1")}, {"AAPL": Decimal("0.5")}):
            with self.subTest(targets=targets):
                result = calculate_rebalance(
                    [holding("AAPL", "50")], Decimal("0"), PRICES, targets
                )
                self.assertTrue(
                    any("Nothing was sent to a broker" in note for note in result.limitations)
                )


if __name__ == "__main__":
    unittest.main()
