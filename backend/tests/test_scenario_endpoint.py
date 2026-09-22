"""Error paths and mapping for POST /portfolio/scenario.

The arithmetic is covered in test_scenario.py. This file checks how its outcomes become
HTTP, using a stub session so nothing here can read or write the real demo data.

The -100..+100 bound is enforced by `ScenarioRequest`, which FastAPI validates before the
handler is ever called, so it cannot be reached from here. It is checked against the live
server instead — see the curl commands in the README.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from decimal import Decimal

from fastapi import HTTPException

from app.main import post_portfolio_scenario
from app.schemas import ScenarioRequest
from tests.test_valuation import StubHolding
from tests.test_valuation_endpoint import SYNCED_AT, StubPortfolio, StubSession


def request(symbol: str = "NVDA", percent: str = "-10") -> ScenarioRequest:
    return ScenarioRequest(symbol=symbol, price_change_percent=Decimal(percent))


class ScenarioEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.portfolio = StubPortfolio(
            holdings=[
                StubHolding(symbol="AAPL", quantity=Decimal("5")),
                StubHolding(symbol="NVDA", quantity=Decimal("10")),
            ]
        )

    def test_unseeded_portfolio_is_404(self):
        with self.assertRaises(HTTPException) as caught:
            post_portfolio_scenario(request(), session=StubSession(None))

        self.assertEqual(caught.exception.status_code, 404)

    def test_a_symbol_the_portfolio_does_not_hold_is_404(self):
        with self.assertRaises(HTTPException) as caught:
            post_portfolio_scenario(request(symbol="TSLA"), session=StubSession(self.portfolio))

        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("TSLA", caught.exception.detail)
        # The message says what *is* held, so the caller can correct the request.
        self.assertIn("AAPL", caught.exception.detail)

    def test_a_percentage_becomes_a_fraction(self):
        response = post_portfolio_scenario(
            request(symbol="NVDA", percent="-10"), session=StubSession(self.portfolio)
        )

        # -10 percent, not -10 as a fraction: the conversion happens at this boundary.
        self.assertEqual(response.price_before, Decimal("150.00"))
        self.assertEqual(response.price_after, Decimal("135.00"))
        self.assertEqual(response.price_change_percent, Decimal("-10"))

    def test_the_response_carries_both_sides_and_the_change(self):
        response = post_portfolio_scenario(
            request(symbol="NVDA", percent="-10"), session=StubSession(self.portfolio)
        )

        self.assertEqual(response.portfolio_id, 1)
        self.assertEqual(response.currency, "USD")
        self.assertEqual(response.price_source, "demo")
        self.assertEqual(response.holding_value_before, Decimal("1500.00"))
        self.assertEqual(response.holding_value_after, Decimal("1350.00"))
        # AAPL (5 x 200.00 = 1000.00) plus NVDA plus the stub's 10000.00 cash.
        self.assertEqual(response.total_value_before, Decimal("12500.00"))
        self.assertEqual(response.total_value_after, Decimal("12350.00"))
        self.assertEqual(response.change_value, Decimal("-150.00"))
        # -150.00 / 12500.00.
        self.assertEqual(response.change_percent, Decimal("-1.20"))

    def test_a_fractional_percentage_survives_the_round_trip(self):
        response = post_portfolio_scenario(
            request(symbol="AAPL", percent="-7.5"), session=StubSession(self.portfolio)
        )

        self.assertEqual(response.price_change_percent, Decimal("-7.5"))
        self.assertEqual(response.price_before, Decimal("200.00"))
        self.assertEqual(response.price_after, Decimal("185.00"))

    def test_a_synchronised_portfolio_moves_the_brokers_price_not_a_demo_one(self):
        """AAPL is held here and the demo table also has a price for it. What-if is the
        page where falling back to that price would be least visible and most wrong."""
        portfolio = StubPortfolio(
            broker="alpaca_paper",
            broker_equity=Decimal("12345.67"),
            last_synced_at=SYNCED_AT,
            holdings=[
                StubHolding(
                    symbol="AAPL", quantity=Decimal("5"), market_price=Decimal("201.25")
                )
            ],
        )

        response = post_portfolio_scenario(
            request(symbol="AAPL", percent="-10"), session=StubSession(portfolio)
        )

        self.assertEqual(response.price_source, "alpaca_paper")
        self.assertEqual(response.last_synced_at, SYNCED_AT)
        # 201.25, not the demo table's 200.00.
        self.assertEqual(response.price_before, Decimal("201.25"))
        self.assertEqual(response.price_after, Decimal("181.13"))
        self.assertEqual(response.holding_value_before, Decimal("1006.25"))
        self.assertEqual(response.holding_value_after, Decimal("905.63"))
        # The broker's equity moves by the one price that moved, and nothing else.
        self.assertEqual(response.total_value_before, Decimal("12345.67"))
        self.assertEqual(response.total_value_after, Decimal("12245.05"))
        self.assertEqual(response.change_value, Decimal("-100.62"))


class ScenarioRequestValidationTest(unittest.TestCase):
    """The bounds, checked on the model directly rather than through HTTP."""

    def test_the_percentage_is_bounded(self):
        for out_of_range in ("-100.01", "100.01", "500"):
            with self.assertRaises(ValueError):
                request(percent=out_of_range)

    def test_the_bounds_themselves_are_allowed(self):
        for allowed in ("-100", "100", "0"):
            self.assertEqual(
                request(percent=allowed).price_change_percent, Decimal(allowed)
            )

    def test_a_percentage_sent_as_a_string_parses_exactly(self):
        # What the frontend sends. Going through a float would be lossy for some of
        # these; Decimal("-7.5") is exact.
        parsed = ScenarioRequest(symbol="NVDA", price_change_percent="-7.5")  # type: ignore[arg-type]

        self.assertEqual(parsed.price_change_percent, Decimal("-7.5"))

    def test_an_empty_symbol_is_rejected(self):
        with self.assertRaises(ValueError):
            request(symbol="")


if __name__ == "__main__":
    unittest.main()
