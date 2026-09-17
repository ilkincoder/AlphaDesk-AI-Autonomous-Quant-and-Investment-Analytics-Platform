"""Tests for the scenario calculation.

    python -m unittest discover -s tests -t .

No database and no HTTP: `calculate_scenario` is pure. Note what is *not* tested here —
percentages. The function takes a fraction, so "-10%" appears nowhere below; converting a
percentage is the caller's job and is tested against the endpoint.
"""

import unittest
from decimal import Decimal

from app.valuation import (
    DEMO_PRICES,
    HoldingNotFoundError,
    MissingPriceError,
    calculate_scenario,
)
from tests.test_valuation import holding


def demo_holdings():
    return [holding("AAPL", "5"), holding("MSFT", "8"), holding("NVDA", "10")]


class DemoScenarioTest(unittest.TestCase):
    """NVDA down 10%, against the demo portfolio."""

    def setUp(self) -> None:
        self.scenario = calculate_scenario(
            demo_holdings(), Decimal("10000.00"), DEMO_PRICES, "NVDA", Decimal("-0.10")
        )

    def test_price_falls_by_a_tenth(self):
        self.assertEqual(self.scenario.price_before, Decimal("150.00"))
        self.assertEqual(self.scenario.price_after, Decimal("135.00"))

    def test_holding_value_follows_the_price(self):
        self.assertEqual(self.scenario.holding_value_before, Decimal("1500.00"))
        self.assertEqual(self.scenario.holding_value_after, Decimal("1350.00"))

    def test_portfolio_total_moves_by_the_same_amount(self):
        self.assertEqual(self.scenario.total_value_before, Decimal("16100.00"))
        self.assertEqual(self.scenario.total_value_after, Decimal("15950.00"))

    def test_change_in_dollars_and_percent(self):
        self.assertEqual(self.scenario.change_value, Decimal("-150.00"))
        self.assertEqual(self.scenario.change_percent, Decimal("-0.93"))

    def test_only_the_shocked_holding_moves(self):
        # The portfolio must move by exactly as much as the shocked holding did. If
        # cash or any other holding had shifted too, these two differences would
        # disagree — which is the whole claim the "remains unchanged" note makes.
        self.assertEqual(
            self.scenario.total_value_after - self.scenario.total_value_before,
            self.scenario.holding_value_after - self.scenario.holding_value_before,
        )


class RiseTest(unittest.TestCase):
    def test_a_rise_is_the_mirror_of_the_fall(self):
        scenario = calculate_scenario(
            demo_holdings(), Decimal("10000.00"), DEMO_PRICES, "NVDA", Decimal("0.10")
        )

        self.assertEqual(scenario.price_after, Decimal("165.00"))
        self.assertEqual(scenario.holding_value_after, Decimal("1650.00"))
        self.assertEqual(scenario.total_value_after, Decimal("16250.00"))
        self.assertEqual(scenario.change_value, Decimal("150.00"))
        self.assertEqual(scenario.change_percent, Decimal("0.93"))


class BoundaryTest(unittest.TestCase):
    def test_no_change_leaves_every_figure_alone(self):
        scenario = calculate_scenario(
            demo_holdings(), Decimal("10000.00"), DEMO_PRICES, "NVDA", Decimal("0")
        )

        self.assertEqual(scenario.change_value, Decimal("0.00"))
        self.assertEqual(scenario.change_percent, Decimal("0.00"))
        self.assertEqual(scenario.total_value_after, scenario.total_value_before)

    def test_a_total_loss_takes_the_price_to_zero(self):
        scenario = calculate_scenario(
            demo_holdings(), Decimal("10000.00"), DEMO_PRICES, "NVDA", Decimal("-1")
        )

        self.assertEqual(scenario.price_after, Decimal("0.00"))
        self.assertEqual(scenario.holding_value_after, Decimal("0.00"))
        # NVDA stops contributing. AAPL (1000) and MSFT (3600) are untouched, so the
        # portfolio is those plus the 10000 cash — not the cash alone.
        self.assertEqual(scenario.total_value_after, Decimal("14600.00"))
        self.assertEqual(scenario.change_value, Decimal("-1500.00"))

    def test_a_fall_beyond_a_total_loss_is_refused(self):
        # -1.5 is a price of negative 75.00, which is not a price.
        with self.assertRaises(ValueError):
            calculate_scenario(
                demo_holdings(), Decimal("10000.00"), DEMO_PRICES, "NVDA", Decimal("-1.01")
            )

    def test_the_input_fraction_is_reported_back_unchanged(self):
        scenario = calculate_scenario(
            demo_holdings(), Decimal("10000.00"), DEMO_PRICES, "NVDA", Decimal("-0.075")
        )

        self.assertEqual(scenario.price_change, Decimal("-0.075"))
        self.assertEqual(scenario.price_after, Decimal("138.75"))


class UnknownSymbolTest(unittest.TestCase):
    def test_a_symbol_that_is_not_held_is_its_own_error(self):
        with self.assertRaises(HoldingNotFoundError) as caught:
            calculate_scenario(
                demo_holdings(), Decimal("10000.00"), DEMO_PRICES, "TSLA", Decimal("-0.10")
            )

        self.assertEqual(caught.exception.symbol, "TSLA")

    def test_a_held_symbol_with_no_price_is_a_pricing_error(self):
        # Held, but absent from the price mapping: a different failure from the above,
        # and reported differently.
        with self.assertRaises(MissingPriceError) as caught:
            calculate_scenario(
                [holding("XYZ", "1")], Decimal("0"), {}, "XYZ", Decimal("-0.10")
            )

        self.assertEqual(caught.exception.symbols, ["XYZ"])

    def test_a_cash_only_portfolio_holds_nothing_to_shock(self):
        with self.assertRaises(HoldingNotFoundError):
            calculate_scenario([], Decimal("10000.00"), DEMO_PRICES, "NVDA", Decimal("-0.10"))


class ZeroValueTest(unittest.TestCase):
    def test_change_percent_is_null_when_there_was_nothing_to_lose(self):
        # A held symbol priced at zero: the portfolio is worth nothing before the move,
        # so the change has no denominator.
        scenario = calculate_scenario(
            [holding("X", "1")], Decimal("0"), {"X": Decimal("0.00")}, "X", Decimal("-0.10")
        )

        self.assertEqual(scenario.change_value, Decimal("0.00"))
        self.assertIsNone(scenario.change_percent)

    def test_cash_only_portfolio_cannot_be_shocked_at_all(self):
        # Documented above as an error rather than a zero result: there is no price to
        # move, and reporting a 0.00 change would imply one had been considered.
        with self.assertRaises(HoldingNotFoundError):
            calculate_scenario([], Decimal("500.00"), DEMO_PRICES, "AAPL", Decimal("0.50"))


if __name__ == "__main__":
    unittest.main()
