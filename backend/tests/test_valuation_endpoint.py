"""Error-path and mapping checks for GET /portfolio/valuation.

The arithmetic is covered in test_valuation.py. What is checked here is how its outcomes
become HTTP: a missing portfolio is a 404, a missing price is a 503 naming the symbol,
and a valid portfolio is mapped onto the response model.

Every case uses a stub session, so nothing here can read or write the real demo data --
the persistent portfolio is never modified to provoke an error.

Importing app.main builds the SQLAlchemy engine, so DATABASE_URL must be set (it is,
inside the container). No connection is opened: engines connect lazily.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import HTTPException

from app.main import get_portfolio_valuation
from tests.test_valuation import StubHolding


@dataclass
class StubPortfolio:
    """Only the attributes the handler reads off a Portfolio row."""

    id: int = 1
    currency: str = "USD"
    cash_balance: Decimal = Decimal("10000.00")
    holdings: list[StubHolding] = field(default_factory=list)


class StubSession:
    """Stands in for a SQLAlchemy Session. `scalar` is the only method the handler uses."""

    def __init__(self, portfolio: StubPortfolio | None) -> None:
        self._portfolio = portfolio

    def scalar(self, statement):  # noqa: ANN001 - mirrors Session.scalar's signature
        return self._portfolio


class ValuationEndpointTest(unittest.TestCase):
    def test_unseeded_portfolio_is_404(self):
        with self.assertRaises(HTTPException) as caught:
            get_portfolio_valuation(session=StubSession(None))

        self.assertEqual(caught.exception.status_code, 404)

    def test_missing_price_is_503_naming_the_symbol(self):
        portfolio = StubPortfolio(
            holdings=[
                StubHolding(symbol="AAPL", quantity=Decimal("5")),
                StubHolding(symbol="XYZ", quantity=Decimal("1")),
            ]
        )

        with self.assertRaises(HTTPException) as caught:
            get_portfolio_valuation(session=StubSession(portfolio))

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("XYZ", caught.exception.detail)
        # The priced symbol must not appear as missing, and no total is reported at all:
        # the exception is the whole response.
        self.assertNotIn("AAPL", caught.exception.detail)

    def test_valid_portfolio_maps_onto_the_response_model(self):
        portfolio = StubPortfolio(
            holdings=[StubHolding(symbol="AAPL", quantity=Decimal("5"))]
        )

        response = get_portfolio_valuation(session=StubSession(portfolio))

        self.assertEqual(response.portfolio_id, 1)
        self.assertEqual(response.currency, "USD")
        self.assertEqual(response.price_source, "demo")
        self.assertEqual(response.cash_balance, Decimal("10000.00"))
        self.assertEqual(response.holdings_value, Decimal("1000.00"))
        self.assertEqual(response.total_value, Decimal("11000.00"))
        self.assertEqual(response.cash_allocation_percent, Decimal("90.91"))
        self.assertEqual([item.symbol for item in response.holdings], ["AAPL"])


if __name__ == "__main__":
    unittest.main()
