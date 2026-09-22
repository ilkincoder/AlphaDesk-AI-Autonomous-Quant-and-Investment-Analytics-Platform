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
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException

from app.main import get_portfolio_valuation
from tests.test_valuation import StubHolding

SYNCED_AT = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)


@dataclass
class StubPortfolio:
    """Only the attributes the handler reads off a Portfolio row.

    `broker` and its companions default to None, which is the unlinked portfolio -- the
    demo basis. A test that wants broker prices sets all three, because a linked
    portfolio without an equity figure is a state the CHECK constraint forbids.
    """

    id: int = 1
    # The name the row has before it is linked. The scenario handler quotes it back when a
    # symbol is not held, so the stub needs the attribute even where the value is unused.
    name: str = "AlphaDesk Demo"
    currency: str = "USD"
    cash_balance: Decimal = Decimal("10000.00")
    holdings: list[StubHolding] = field(default_factory=list)
    broker: str | None = None
    broker_equity: Decimal | None = None
    last_synced_at: datetime | None = None


class _StubResult:
    """What `Session.scalars(...)` returns, as far as the handler is concerned."""

    def __init__(self, portfolio: StubPortfolio | None) -> None:
        self._portfolio = portfolio

    def first(self) -> StubPortfolio | None:
        return self._portfolio


class StubSession:
    """Stands in for a SQLAlchemy Session, over the one method the portfolio lookup uses.

    `scalars(...).first()` rather than `scalar(...)`: the real lookup takes the first of
    the portfolio's two possible names rather than asserting there is exactly one row, so
    the double has to offer the same shape.
    """

    def __init__(self, portfolio: StubPortfolio | None) -> None:
        self._portfolio = portfolio

    def scalars(self, statement):  # noqa: ANN001 - mirrors Session.scalars' signature
        return _StubResult(self._portfolio)


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
        # A portfolio that has never been synchronised has no sync time to report, and
        # `price_source` is what says so.
        self.assertIsNone(response.last_synced_at)

    def test_a_synchronised_portfolio_is_valued_at_the_brokers_own_prices(self):
        portfolio = StubPortfolio(
            cash_balance=Decimal("2500.25"),
            broker="alpaca_paper",
            broker_equity=Decimal("12345.67"),
            last_synced_at=SYNCED_AT,
            holdings=[
                StubHolding(
                    symbol="AAPL", quantity=Decimal("5"), market_price=Decimal("201.25")
                )
            ],
        )

        response = get_portfolio_valuation(session=StubSession(portfolio))

        self.assertEqual(response.price_source, "alpaca_paper")
        self.assertEqual(response.last_synced_at, SYNCED_AT)
        self.assertEqual(response.holdings[0].price, Decimal("201.25"))
        self.assertEqual(response.holdings_value, Decimal("1006.25"))
        # The broker's equity is the total, not the sum of the parts: the account and the
        # positions are two reads, and equity is the broker's own answer.
        self.assertEqual(response.total_value, Decimal("12345.67"))
        self.assertEqual(
            response.holdings[0].allocation_percent, Decimal("8.15")
        )

    def test_a_synchronised_holding_with_no_stored_price_is_503_never_a_demo_price(self):
        """AAPL has a demo price and this portfolio holds it. That price must not be used:
        against a real quantity it would be a fictional valuation wearing a real one's
        clothes."""
        portfolio = StubPortfolio(
            broker="alpaca_paper",
            broker_equity=Decimal("12345.67"),
            last_synced_at=SYNCED_AT,
            holdings=[StubHolding(symbol="AAPL", quantity=Decimal("5"))],
        )

        with self.assertRaises(HTTPException) as caught:
            get_portfolio_valuation(session=StubSession(portfolio))

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("AAPL", caught.exception.detail)
        self.assertIn("alpaca_paper", caught.exception.detail)
        self.assertNotIn("DEMO_PRICES", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
