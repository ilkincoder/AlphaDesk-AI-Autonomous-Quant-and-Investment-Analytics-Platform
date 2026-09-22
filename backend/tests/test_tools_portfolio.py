"""Capability D: the portfolio context, and the three answers it must not blur together.

The valuation arithmetic is `app.valuation`'s and is tested in `test_valuation.py`. What is
tested here is what the *tool* promises: that "no portfolio", "not held" and "held but
unpriceable" are different outcomes, that the demo basis is stated on every answer, that a
stored quantity is still reported when the valuation declines, and that none of it is dressed
up as a historical position.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models import Holding, Portfolio
from app.portfolio_identity import BROKER_PORTFOLIO_NAME, DEMO_PORTFOLIO_NAME
from app.tools import portfolio
from app.tools.portfolio import PortfolioContextRequest
from app.tools.results import ToolStatus
from app.valuation import DEMO_PRICES
from tests.test_tools_contracts import ToolTestCase


class PortfolioToolTestCase(ToolTestCase):
    def add_portfolio(self, *, cash: str = "10000.00") -> Portfolio:
        record = Portfolio(
            name=DEMO_PORTFOLIO_NAME, currency="USD", cash_balance=Decimal(cash)
        )
        self.session.add(record)
        self.session.flush()
        return record

    def add_holding(self, record: Portfolio, symbol: str, quantity: str, price: str):
        self.session.add(
            Holding(
                portfolio_id=record.id,
                symbol=symbol,
                quantity=Decimal(quantity),
                average_buy_price=Decimal(price),
            )
        )
        self.session.flush()

    def context(self, symbol: str = "NVDA"):
        return portfolio.run(self.session, PortfolioContextRequest(symbol=symbol))


class MissingPortfolioTests(PortfolioToolTestCase):
    def test_no_portfolio_is_its_own_outcome_not_an_empty_holding(self):
        result = self.context()

        self.assertEqual(result.status, ToolStatus.UNAVAILABLE)
        self.assertEqual(result.reason, "portfolio_not_found")
        self.assertIsNone(result.data)
        self.assertIn("no portfolio named", " ".join(result.warnings).lower())
        self.assertIn("python -m app.seed", " ".join(result.warnings))

    def test_the_standing_caveats_are_on_that_answer_too(self):
        """A reader who gets nothing still must not read it as "you hold nothing"."""
        result = self.context()

        joined = " ".join(result.warnings)
        self.assertIn("not a historical position", joined)
        self.assertIn("not available in this system", joined)
        # The price-source caveat is deliberately absent: there is no portfolio, so no
        # value on this answer came from a price, and a sentence about which prices were
        # used would be noise. It is on every answer that carries a figure.
        self.assertNotIn("fictional demo price table", joined)


class HeldTests(PortfolioToolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.portfolio = self.add_portfolio()
        self.add_holding(self.portfolio, "NVDA", "10", "120.00")
        self.add_holding(self.portfolio, "AAPL", "5", "180.00")

    def test_a_held_symbol_reports_its_quantity_and_demo_valuation(self):
        result = self.context("NVDA")

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertTrue(result.data["held"])

        holding = result.data["holding"]
        self.assertEqual(holding["symbol"], "NVDA")
        self.assertEqual(holding["quantity"], "10.000000")
        # The recorded purchase price and the valuation price are different numbers, and are
        # reported side by side rather than one standing in for the other.
        self.assertEqual(holding["average_buy_price"], "120.0000")
        self.assertEqual(holding["price"], "150.00")
        self.assertEqual(holding["holding_value"], "1500.00")
        # 1500 of 12500: the holding's share of the whole portfolio, cash included.
        self.assertEqual(holding["allocation_percent"], "12.00")

    def test_the_portfolio_totals_are_the_existing_valuations(self):
        result = self.context("NVDA")

        valuation = result.data["valuation"]
        self.assertEqual(valuation["cash_balance"], "10000.00")
        self.assertEqual(valuation["holdings_value"], "2500.00")
        self.assertEqual(valuation["total_value"], "12500.00")
        self.assertEqual(
            [item["symbol"] for item in valuation["holdings"]], ["AAPL", "NVDA"]
        )

    def test_the_demo_basis_is_stated_and_never_presented_as_market_data(self):
        result = self.context("NVDA")

        self.assertEqual(result.data["price_source"], "demo")
        self.assertIn("fictional", result.data["price_source_note"])
        self.assertIn("Not imported market prices", result.data["price_source_note"])
        joined = " ".join(result.warnings)
        self.assertIn("fictional demo price table", joined)
        self.assertIn("must not be combined with stored daily prices", joined)

    def test_the_answer_is_about_now_and_says_so(self):
        """These are current holdings; dating them to an analysis cutoff would be invented."""
        result = self.context("NVDA")

        self.assertIsNone(result.as_of)
        self.assertIsNone(result.information_cutoff)
        self.assertIsNotNone(result.data["read_at"])
        joined = " ".join(result.warnings)
        self.assertIn("not a historical position", joined)
        self.assertIn("not available in this system", joined)

    def test_the_symbol_is_matched_case_insensitively(self):
        result = self.context("  nvda ")

        self.assertEqual(result.symbol, "NVDA")
        self.assertTrue(result.data["held"])


class NotHeldTests(PortfolioToolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.portfolio = self.add_portfolio()
        self.add_holding(self.portfolio, "NVDA", "10", "120.00")

    def test_an_unheld_symbol_is_an_answer_rather_than_an_absence_of_data(self):
        result = self.context("TSLA")

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertFalse(result.data["held"])
        self.assertIsNone(result.data["holding"])
        # The portfolio is still valued: the question was about one symbol, not the portfolio.
        self.assertEqual(result.data["valuation"]["total_value"], "11500.00")
        self.assertIn("is not a holding of", " ".join(result.warnings))
        self.assertIn("not a statement that it was never held", " ".join(result.warnings))

    def test_the_held_symbols_are_named_so_the_answer_is_checkable(self):
        result = self.context("TSLA")

        self.assertIn("NVDA", " ".join(result.warnings))


class UnpriceableHoldingTests(PortfolioToolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.portfolio = self.add_portfolio()
        self.add_holding(self.portfolio, "NVDA", "10", "120.00")
        # A held symbol the demo price table has never heard of. The valuation refuses to
        # produce any total at all rather than one that quietly omits it.
        self.add_holding(self.portfolio, "ZZZZ", "3", "10.00")

    def test_an_unpriceable_holding_is_partial_and_withholds_every_derived_figure(self):
        with self.assertLogs("app.tools.portfolio", level="WARNING"):
            result = self.context("NVDA")

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(result.reason, "valuation_unavailable_missing_price")
        self.assertIsNone(result.data["valuation"])
        self.assertEqual(
            result.data["valuation_unavailable_reason"],
            "valuation_unavailable_missing_price",
        )
        self.assertIn("deliberate refusal", " ".join(result.warnings))

    def test_a_stored_quantity_survives_even_though_no_value_can_be_derived(self):
        """Quantity is read from the row. A missing price elsewhere does not make it untrue."""
        with self.assertLogs("app.tools.portfolio", level="WARNING"):
            result = self.context("NVDA")

        holding = result.data["holding"]
        self.assertEqual(holding["quantity"], "10.000000")
        self.assertEqual(holding["average_buy_price"], "120.0000")
        # A lookup in the same table the valuation uses, so it is shown; the derived figures
        # are not, because producing them means doing the valuation's arithmetic here.
        self.assertEqual(holding["price"], "150.00")
        self.assertIsNone(holding["holding_value"])
        self.assertIsNone(holding["allocation_percent"])

    def test_the_unpriceable_symbol_is_not_reported_as_unheld(self):
        with self.assertLogs("app.tools.portfolio", level="WARNING"):
            result = self.context("NVDA")

        self.assertTrue(result.data["held"])
        self.assertIsNotNone(result.data["holding"])


class SynchronisedPortfolioTests(PortfolioToolTestCase):
    """Once the portfolio holds real positions, the demo table is not an option.

    The tool is the one place a fictional price could reach an answer the Supervisor
    repeats as fact, so what it does with a broker-linked portfolio is checked here rather
    than left to the endpoints that share the same resolution.
    """

    SYNCED_AT = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)

    def link(self, record: Portfolio) -> Portfolio:
        # The name moves with the link, exactly as a real sync moves it.
        record.name = BROKER_PORTFOLIO_NAME
        record.broker = "alpaca_paper"
        record.broker_account_id = "8f3a2b10-4c5d-4e6f-8a9b-0c1d2e3f4a5b"
        record.broker_equity = Decimal("12345.67")
        record.last_synced_at = self.SYNCED_AT
        self.session.flush()
        return record

    def add_priced_holding(
        self, record: Portfolio, symbol: str, quantity: str, entry: str, price: str
    ) -> None:
        self.session.add(
            Holding(
                portfolio_id=record.id,
                symbol=symbol,
                quantity=Decimal(quantity),
                average_buy_price=Decimal(entry),
                market_price=Decimal(price),
                # The broker's own figure, not recomputed from quantity x price.
                market_value=Decimal(price) * Decimal(quantity),
            )
        )
        self.session.flush()

    def test_a_synchronised_portfolio_is_valued_at_the_brokers_prices(self):
        record = self.link(self.add_portfolio(cash="2500.25"))
        self.add_priced_holding(record, "AAPL", "5", "180.10", "201.25")

        result = self.context("AAPL")

        self.assertEqual(result.data["price_source"], "alpaca_paper")
        self.assertEqual(result.data["holding"]["price"], "201.25")
        self.assertEqual(result.data["holding"]["holding_value"], "1006.25")
        self.assertEqual(result.data["valuation"]["total_value"], "12345.67")

    def test_the_broker_basis_is_stated_and_the_demo_caveat_is_not(self):
        record = self.link(self.add_portfolio())
        self.add_priced_holding(record, "AAPL", "5", "180.10", "201.25")

        result = self.context("AAPL")

        joined = " ".join(result.warnings)
        self.assertIn("prices the broker last reported", joined)
        self.assertIn(self.SYNCED_AT.isoformat(), joined)
        # The demo sentence would be false here, so it is absent -- and its absence is what
        # keeps both sentences worth reading.
        self.assertNotIn("fictional demo price table", joined)
        self.assertNotIn("fictional", result.data["price_source_note"])

    def test_a_synchronised_holding_with_no_stored_price_is_never_given_a_demo_one(self):
        """AAPL has a demo price. Using it against a real quantity is exactly the mistake
        this basis exists to prevent."""
        record = self.link(self.add_portfolio())
        self.add_holding(record, "AAPL", "5", "180.10")

        with self.assertLogs("app.tools.portfolio", level="WARNING"):
            result = self.context("AAPL")

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(result.reason, "valuation_unavailable_missing_price")
        self.assertIsNone(result.data["valuation"])
        self.assertIsNone(result.data["holding"]["price"])
        # The basis is still named, so the answer says whose prices are missing rather
        # than leaving the reader to guess.
        self.assertEqual(result.data["price_source"], "alpaca_paper")


class ReadOnlyTests(PortfolioToolTestCase):
    def test_reading_the_portfolio_changes_nothing(self):
        record = self.add_portfolio()
        self.add_holding(record, "NVDA", "10", "120.00")

        before = self.counts()
        self.context("NVDA")
        self.context("TSLA")
        self.assert_wrote_nothing(before)

    def test_the_tool_uses_the_shared_demo_price_table(self):
        """No second valuation system and no substituted imported prices."""
        record = self.add_portfolio()
        for symbol in DEMO_PRICES:
            self.add_holding(record, symbol, "1", "1.00")

        result = self.context("NVDA")

        self.assertEqual(result.data["holding"]["price"], str(DEMO_PRICES["NVDA"]))
        self.assertEqual(
            result.data["valuation"]["holdings"][0]["price"],
            str(DEMO_PRICES[sorted(DEMO_PRICES)[0]]),
        )

    def test_no_holding_row_is_created_or_modified(self):
        record = self.add_portfolio()
        self.add_holding(record, "NVDA", "10", "120.00")
        before = self.session.scalars(
            select(Holding.quantity).order_by(Holding.symbol)
        ).all()

        self.context("NVDA")

        after = self.session.scalars(
            select(Holding.quantity).order_by(Holding.symbol)
        ).all()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
