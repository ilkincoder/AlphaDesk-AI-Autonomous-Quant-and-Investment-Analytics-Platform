"""Tests for the valuation calculation.

Run from the backend directory (inside the container, /app):

    python -m unittest discover -s tests -t .

No database and no HTTP: `calculate_valuation` is pure, so these run in milliseconds and
need no service running. The endpoint itself is checked by hand with curl, because it
reads the shared demo portfolio and these tests must not write to it.
"""

import unittest
from dataclasses import dataclass
from decimal import Decimal

from app.valuation import DEMO_PRICES, MissingPriceError, calculate_valuation


@dataclass(frozen=True)
class StubHolding:
    """The smallest thing that satisfies the PricedHolding protocol.

    Note what is missing: no `average_buy_price`. If the calculation ever fell back to
    the purchase price when a valuation price was absent, every test here would fail
    with an AttributeError instead of passing.

    `market_price` is here for the same reason, from the other side: it is the column a
    synchronised portfolio stores its broker price in, so a stub without one is a
    portfolio the price resolution must refuse rather than value.
    """

    symbol: str
    quantity: Decimal
    market_price: Decimal | None = None


def holding(symbol: str, quantity: str) -> StubHolding:
    return StubHolding(symbol=symbol, quantity=Decimal(quantity))


class DemoPortfolioTest(unittest.TestCase):
    """The seeded demo portfolio at the demo prices.

    Deliberately passed in a non-alphabetical order, so that the sorting inside the
    calculation is exercised rather than assumed.
    """

    def setUp(self) -> None:
        self.valuation = calculate_valuation(
            [holding("NVDA", "10"), holding("AAPL", "5"), holding("MSFT", "8")],
            Decimal("10000.00"),
            DEMO_PRICES,
        )

    def test_holdings_are_sorted_by_symbol(self):
        self.assertEqual(
            [item.symbol for item in self.valuation.holdings],
            ["AAPL", "MSFT", "NVDA"],
        )

    def test_holding_values(self):
        self.assertEqual(
            [item.holding_value for item in self.valuation.holdings],
            [Decimal("1000.00"), Decimal("3600.00"), Decimal("1500.00")],
        )

    def test_holding_allocations(self):
        self.assertEqual(
            [item.allocation_percent for item in self.valuation.holdings],
            [Decimal("6.21"), Decimal("22.36"), Decimal("9.32")],
        )

    def test_prices_are_reported_alongside_the_values(self):
        self.assertEqual(
            [item.price for item in self.valuation.holdings],
            [Decimal("200.00"), Decimal("450.00"), Decimal("150.00")],
        )

    def test_totals(self):
        self.assertEqual(self.valuation.holdings_value, Decimal("6100.00"))
        self.assertEqual(self.valuation.cash_balance, Decimal("10000.00"))
        self.assertEqual(self.valuation.total_value, Decimal("16100.00"))
        self.assertEqual(self.valuation.cash_allocation_percent, Decimal("62.11"))


class RoundingTest(unittest.TestCase):
    def test_rounds_half_up_not_half_even(self):
        # 0.005 is exactly representable as a Decimal, so the rounding mode alone
        # decides: ROUND_HALF_UP gives 0.01, Python's round() (half-to-even) gives 0.00.
        valuation = calculate_valuation(
            [holding("X", "0.005")], Decimal("0"), {"X": Decimal("1.00")}
        )
        self.assertEqual(valuation.holdings[0].holding_value, Decimal("0.01"))

    def test_fractional_quantity_rounds_on_the_value_not_the_quantity(self):
        # 3.333333 shares at 30.00 is 99.99999, which rounds to 100.00 as a value.
        valuation = calculate_valuation(
            [holding("X", "3.333333")], Decimal("0"), {"X": Decimal("30.00")}
        )
        self.assertEqual(valuation.holdings[0].holding_value, Decimal("100.00"))
        self.assertEqual(valuation.total_value, Decimal("100.00"))
        self.assertEqual(valuation.holdings[0].allocation_percent, Decimal("100.00"))

    def test_rounded_percentages_need_not_sum_to_100(self):
        # Documented consequence of rounding only the output, not a bug: three equal
        # thirds each round to 33.33, leaving 99.99.
        valuation = calculate_valuation(
            [holding("A", "1"), holding("B", "1"), holding("C", "1")],
            Decimal("0"),
            {"A": Decimal("1.00"), "B": Decimal("1.00"), "C": Decimal("1.00")},
        )
        percents = [item.allocation_percent for item in valuation.holdings]
        self.assertEqual(percents, [Decimal("33.33")] * 3)
        self.assertEqual(sum(percents), Decimal("99.99"))


class CashOnlyTest(unittest.TestCase):
    def test_portfolio_without_holdings_is_all_cash(self):
        valuation = calculate_valuation([], Decimal("250.00"), DEMO_PRICES)

        self.assertEqual(valuation.holdings, [])
        self.assertEqual(valuation.holdings_value, Decimal("0.00"))
        self.assertEqual(valuation.total_value, Decimal("250.00"))
        self.assertEqual(valuation.cash_allocation_percent, Decimal("100.00"))


class ZeroTotalTest(unittest.TestCase):
    def test_empty_portfolio_has_null_percentages(self):
        valuation = calculate_valuation([], Decimal("0"), DEMO_PRICES)

        self.assertEqual(valuation.total_value, Decimal("0.00"))
        self.assertIsNone(valuation.cash_allocation_percent)

    def test_zero_priced_holding_has_null_percentages(self):
        # The other route to a zero denominator: a holding whose price is zero.
        valuation = calculate_valuation(
            [holding("X", "1")], Decimal("0"), {"X": Decimal("0.00")}
        )

        self.assertEqual(valuation.total_value, Decimal("0.00"))
        self.assertIsNone(valuation.holdings[0].allocation_percent)
        self.assertIsNone(valuation.cash_allocation_percent)


class MissingPriceTest(unittest.TestCase):
    def test_missing_price_is_an_error_not_a_fallback(self):
        with self.assertRaises(MissingPriceError) as caught:
            calculate_valuation(
                [holding("AAPL", "5"), holding("XYZ", "1")],
                Decimal("10000.00"),
                DEMO_PRICES,
            )

        self.assertEqual(caught.exception.symbols, ["XYZ"])
        self.assertIn("XYZ", str(caught.exception))

    def test_every_missing_symbol_is_reported(self):
        with self.assertRaises(MissingPriceError) as caught:
            calculate_valuation(
                [holding("ZZZ", "1"), holding("XYZ", "1")], Decimal("0"), {}
            )

        self.assertEqual(caught.exception.symbols, ["XYZ", "ZZZ"])


class ReportedTotalTest(unittest.TestCase):
    """A broker's own equity, used as the total instead of the arithmetic.

    The two are different numbers on purpose: the broker's account and its positions are
    two reads, so equity need not equal positions plus cash to the cent, and the figure the
    broker states is the one to report.
    """

    def test_a_reported_total_is_the_total(self):
        result = calculate_valuation(
            [holding("AAPL", "5"), holding("NVDA", "10")],
            Decimal("10000.00"),
            DEMO_PRICES,
            reported_total_value=Decimal("12400.00"),
        )

        self.assertEqual(result.total_value, Decimal("12400.00"))
        self.assertEqual(result.holdings_value, Decimal("2500.00"))
        self.assertEqual(result.cash_balance, Decimal("10000.00"))

    def test_allocations_are_shares_of_the_reported_total(self):
        """The parts and the whole stay in one frame: a percentage taken against the
        computed total while the headline shows the broker's would not add up on screen."""
        result = calculate_valuation(
            [holding("AAPL", "5")],
            Decimal("10000.00"),
            DEMO_PRICES,
            reported_total_value=Decimal("12000.00"),
        )

        # 1000.00 / 12000.00, not 1000.00 / 11000.00.
        self.assertEqual(result.holdings[0].allocation_percent, Decimal("8.33"))
        self.assertEqual(result.cash_allocation_percent, Decimal("83.33"))

    def test_omitting_it_leaves_the_arithmetic_exactly_as_it_was(self):
        holdings = [holding("AAPL", "5"), holding("NVDA", "10")]

        computed = calculate_valuation(holdings, Decimal("10000.00"), DEMO_PRICES)
        explicit = calculate_valuation(
            holdings, Decimal("10000.00"), DEMO_PRICES, reported_total_value=None
        )

        self.assertEqual(computed, explicit)
        self.assertEqual(computed.total_value, Decimal("12500.00"))

    def test_a_reported_total_of_zero_is_a_total_not_an_absence(self):
        result = calculate_valuation(
            [],
            Decimal("0.00"),
            DEMO_PRICES,
            reported_total_value=Decimal("0.00"),
        )

        self.assertEqual(result.total_value, Decimal("0.00"))
        self.assertIsNone(result.cash_allocation_percent)


if __name__ == "__main__":
    unittest.main()
