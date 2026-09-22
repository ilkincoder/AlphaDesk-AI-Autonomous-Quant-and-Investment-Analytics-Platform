"""Which of the three availability branches a question falls into, and why.

This is the correction §6 of the step asks for: an un-ingested company used to be reported as
an *unsupported capability*, which tells a user this system cannot do something when the truth
is that it holds nothing. Trading is unsupported. Tesla is simply absent. Those send a reader
to different places, so they are different outcomes.

The tests that matter most here are the ones about what a branch must **not** do: an
unrecognised name is not proof of absence, and a database that cannot be read is not either.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date
from unittest import mock

from sqlalchemy.exc import OperationalError

from app.agent.context import KnownSymbols
from app.agent.run import (
    STATUS_CLARIFICATION_NEEDED,
    STATUS_COMPANY_NOT_STORED,
    STATUS_COMPLETED,
    STATUS_SERVICE_FAILED,
    STATUS_UNSUPPORTED_CAPABILITY,
    run_analysis,
)
from app.agent.supervisor import Destination, looks_like_a_ticker
from tests.agent_doubles import ScriptedModel, call, calls, findings, route, say, tool_sessions
from tests.test_tools_contracts import ToolTestCase

REFERENCE = date(2026, 9, 17)

# What the development database actually holds: two ingested companies, and a portfolio that
# also owns one that has never been ingested. That third symbol is the whole point.
KNOWN = KnownSymbols(
    companies=("AAPL", "NVDA"),
    holdings=("AAPL", "MSFT", "NVDA"),
)


class AvailabilityTestCase(ToolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.company = self.add_company()
        self.add_price_range(self.company, date(2026, 8, 6), 5, first="100", step="2")
        self.sessions = tool_sessions(self.session)
        self.sessions.__enter__()
        self.addCleanup(lambda: self.sessions.__exit__(None, None, None))

    def run_it(self, script=(), **overrides):
        arguments = {
            "question": "What is happening with this company?",
            "reference_date": REFERENCE,
            "known_symbols": KNOWN,
            "run_id": "run-test",
        }
        arguments.update(overrides)
        model = arguments.pop("client", None) or ScriptedModel(list(script))
        return run_analysis(client=model, **arguments), model


class TickerShapeTests(unittest.TestCase):
    """What counts as a definite claim about a company, and what is a failed lookup."""

    def test_real_tickers_are_recognised(self):
        for symbol in ("NVDA", "AAPL", "BRK.B", "RDS-A", "A", "GOOGL"):
            with self.subTest(symbol=symbol):
                self.assertTrue(looks_like_a_ticker(symbol))

    def test_a_company_name_is_not_a_definite_ticker(self):
        """Shape alone would accept "Apple", which is why the case decides it."""
        for symbol in ("Apple Inc.", "the chip maker", "Apple", "a company", "aapl", "Tesla"):
            with self.subTest(symbol=symbol):
                self.assertFalse(looks_like_a_ticker(symbol))

    def test_an_all_caps_word_is_taken_as_a_definite_claim(self):
        """The residual: "APPLE" reads as a ticker, and is answered as one.

        The answer names what *is* stored, so the correction costs one turn.
        """
        self.assertTrue(looks_like_a_ticker("APPLE"))

    def test_nothing_is_not_a_ticker(self):
        for symbol in (None, "", "   ", "1234"):
            with self.subTest(symbol=symbol):
                self.assertFalse(looks_like_a_ticker(symbol))


class UnknownCompanyTests(AvailabilityTestCase):
    def test_a_definite_ticker_this_system_does_not_hold_is_its_own_outcome(self):
        result, model = self.run_it([route(symbol="TSLA", period="none", start_date=None, end_date=None)])

        self.assertEqual(result.status, STATUS_COMPANY_NOT_STORED)
        self.assertEqual(result.symbol, "TSLA")
        # Not an unsupported capability: nothing about the request was out of scope.
        self.assertNotEqual(result.status, STATUS_UNSUPPORTED_CAPABILITY)
        self.assertEqual(result.destination, Destination.COMPANY_NOT_STORED)

    def test_it_runs_no_analysis_and_calls_no_tool(self):
        """The database settles this, so no analysis is attempted.

        One routing request is still made, because that is how the company in the question is
        read out of it -- but nothing beyond it: no Module 1 request, no tool, no findings, no
        composition.
        """
        result, model = self.run_it(
            [route(symbol="TSLA", period="none", start_date=None, end_date=None)]
        )

        self.assertEqual(len(model.requests), 1)
        self.assertEqual(result.tool_executions, [])
        self.assertEqual(result.usage["tool_calls"], 0)
        self.assertEqual(result.findings, [])

    def test_an_explicitly_named_unknown_company_costs_nothing_at_all(self):
        """When the caller names it, there is nothing to read out of the question."""
        result, model = self.run_it(symbol="TSLA")

        self.assertEqual(result.status, STATUS_COMPANY_NOT_STORED)
        self.assertEqual(len(model.requests), 0)
        self.assertIsNotNone(result.answer)

    def test_the_answer_says_what_is_stored_without_saying_anything_about_the_company(self):
        result, _ = self.run_it(
            [route(symbol="TSLA", period="none", start_date=None, end_date=None)]
        )

        answer = result.answer or ""
        self.assertIn("TSLA", answer)
        self.assertIn("no data", answer.lower())
        self.assertIn("AAPL", answer)
        self.assertIn("NVDA", answer)
        # A limit of what is stored, explicitly not a missing feature and not a claim about
        # the company itself.
        self.assertIn("not a capability this system lacks", answer)
        self.assertIn("not a statement about the company", answer)

    def test_a_held_but_not_ingested_symbol_is_mentioned_as_askable(self):
        """MSFT is owned and never ingested -- the case a union of the two lists exists for."""
        result, _ = self.run_it(
            [route(symbol="TSLA", period="none", start_date=None, end_date=None)]
        )

        self.assertIn("MSFT", result.answer or "")
        self.assertIn("as a holding", result.answer or "")

    def test_the_answer_is_written_in_code_not_by_a_model(self):
        """So the refusal cannot fail because a provider is down or a budget ran out."""
        result, model = self.run_it(
            [route(symbol="TSLA", period="none", start_date=None, end_date=None)]
        )

        self.assertIsNotNone(result.answer)
        self.assertEqual(model.remaining, 0, "the script was consumed")

    def test_no_ingestion_happens(self):
        before = self.counts()
        self.run_it([route(symbol="TSLA", period="none", start_date=None, end_date=None)])

        after = self.counts()
        changed = {table for table in before if before[table] != after[table]}
        self.assertEqual(changed, set())


class HeldButNotIngestedTests(AvailabilityTestCase):
    """MSFT: owned by the demo portfolio, never ingested.

    The symbol that makes the two lists worth keeping apart. A question about *owning* it must
    work; a question about how it has *moved* has no data behind it. The tools already behave
    correctly -- `portfolio_context` reads holdings, the other three look up ingested companies
    -- so what these tests pin is that neither the routing nor the answer confuses the two.
    """

    def setUp(self) -> None:
        super().setUp()
        # The portfolio owns MSFT. Nothing about MSFT is ingested: setUp added NVDA only.
        self.add_holding("MSFT")

    def add_holding(self, symbol: str) -> None:
        from decimal import Decimal

        from app.models import Holding, Portfolio
        from app.portfolio_identity import DEMO_PORTFOLIO_NAME

        portfolio = Portfolio(
            name=DEMO_PORTFOLIO_NAME, currency="USD", cash_balance=Decimal("1000.00")
        )
        self.session.add(portfolio)
        self.session.flush()
        self.session.add(
            Holding(
                portfolio_id=portfolio.id,
                symbol=symbol,
                quantity=Decimal("4"),
                average_buy_price=Decimal("100.00"),
            )
        )
        self.session.flush()

    def portfolio_script(self, answer="You hold it [E1]."):
        return [
            route(symbol="MSFT", period="none", start_date=None, end_date=None),
            calls("Checking the portfolio.", call("c1", "portfolio_context")),
            say("That is enough."),
            findings("The holding was checked.", refs=("E1",)),
            say(answer),
        ]

    def test_a_portfolio_question_runs_and_is_not_refused(self):
        """Ownership is a capability the held list does grant."""
        result, model = self.run_it(self.portfolio_script(), question="Do I own any MSFT?")

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertNotEqual(result.status, STATUS_UNSUPPORTED_CAPABILITY)
        self.assertNotEqual(result.status, STATUS_COMPANY_NOT_STORED)
        self.assertEqual(result.symbol, "MSFT")
        self.assertEqual(result.tool_executions[0]["tool"], "portfolio_context")
        self.assertEqual(result.tool_executions[0]["status"], "ok")
        self.assertGreater(result.usage["model_requests"], 0)

    def test_the_portfolio_tool_returns_the_holding(self):
        result, _ = self.run_it(self.portfolio_script(), question="Do I own any MSFT?")

        holding = result.evidence[0]["summary"]["holding"]
        self.assertEqual(holding["symbol"], "MSFT")
        self.assertEqual(holding["quantity"], "4.000000")

    def test_a_market_question_runs_and_the_tool_reports_the_gap(self):
        """Not refused, and not answered: the market tool says nothing is stored."""
        script = [
            route(
                symbol="MSFT",
                period="explicit",
                start_date="2026-08-06",
                end_date="2026-09-17",
            ),
            calls("Checking.", call("c1", "market_insider_analysis")),
            say("That is enough."),
            findings("No market data was returned.", refs=()),
            say("There is no stored market data for MSFT."),
        ]

        result, _ = self.run_it(
            script,
            question="How has MSFT moved over August 2026?",
            symbol="MSFT",
            start_date=date(2026, 8, 6),
            end_date=date(2026, 9, 17),
        )

        execution = result.tool_executions[0]
        self.assertEqual(execution["tool"], "market_insider_analysis")
        self.assertEqual(execution["status"], "unavailable")
        # The tool's own reason is preserved rather than replaced by a generic refusal.
        self.assertEqual(execution["reason"], "unknown_company")
        self.assertNotEqual(result.status, STATUS_UNSUPPORTED_CAPABILITY)

        # The refusal is recorded as evidence -- "we looked and found nothing" is a fact an
        # answer may state -- but it carries no payload, so there is nothing to reason from
        # and no price to assert.
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0]["status"], "unavailable")
        self.assertIsNone(result.evidence[0]["summary"])
        self.assertEqual(result.citations, [])

    def test_a_mixed_question_returns_the_portfolio_part_and_names_the_gap(self):
        """The case this correction exists for.

        "Do I own MSFT and how has it moved?" has one answerable half and one that is not.
        The run must produce the ownership facts and say plainly that the market data is
        unavailable -- not refuse the lot, and not answer the market half from memory.
        """
        script = [
            route(
                symbol="MSFT",
                period="explicit",
                start_date="2026-08-06",
                end_date="2026-09-17",
            ),
            calls(
                "Checking both.",
                call("c1", "portfolio_context"),
                call("c2", "market_insider_analysis"),
            ),
            say("That is enough."),
            findings(
                "The portfolio holds MSFT.",
                refs=("E1",),
                limitations=["No market data is stored for MSFT."],
            ),
            say(
                "You hold 4 MSFT shares [E1]. The market comparison could not be run: no "
                "market, insider or filing data is stored for MSFT."
            ),
        ]

        result, _ = self.run_it(
            script,
            question="Do I own MSFT and how has it moved over August 2026?",
            symbol="MSFT",
            start_date=date(2026, 8, 6),
            end_date=date(2026, 9, 17),
        )

        self.assertEqual(result.status, STATUS_COMPLETED)
        statuses = {item["tool"]: item["status"] for item in result.tool_executions}
        self.assertEqual(statuses["portfolio_context"], "ok")
        self.assertEqual(statuses["market_insider_analysis"], "unavailable")
        # The ownership half is in the answer, and so is the gap.
        self.assertIn("4 MSFT shares", result.answer)
        self.assertIn("could not be run", result.answer)
        self.assertIn("No market data is stored for MSFT.", result.limitations)

    def test_the_prompt_names_it_as_held_but_not_ingested(self):
        """The application says which symbols those are; the model is not left to work it out."""
        script = self.portfolio_script()
        _, model = self.run_it(script, question="Do I own any MSFT?")

        prompt = model.requests[0]["messages"][1]["content"]
        self.assertIn("held but NOT ingested", prompt)
        self.assertIn("MSFT", prompt)
        self.assertIn("ownership questions only", prompt)

    def test_the_tool_prompt_says_which_tool_can_answer(self):
        """The agent is told which one applies, rather than discovering it by failing."""
        from app.agent.prompts import MODULE1_PROMPT

        self.assertIn("held but not ingested", MODULE1_PROMPT)
        self.assertIn("the other three look up ingested companies", MODULE1_PROMPT)

    def test_being_held_does_not_make_it_ingested(self):
        """The code-level fact the whole distinction rests on."""
        from app.agent.context import known_symbols

        known = known_symbols(self.session)

        self.assertIn("NVDA", known.companies)
        self.assertNotIn("MSFT", known.companies)
        self.assertIn("MSFT", known.holdings)
        self.assertEqual(known.held_only, ("MSFT",))
        self.assertTrue(known.recognises("MSFT"))

    def test_a_held_symbol_is_still_recognised_rather_than_refused(self):
        """Recognition uses the union; capability does not. This is the first, not the second."""
        result, model = self.run_it(self.portfolio_script(), question="Do I own any MSFT?")

        self.assertNotEqual(result.status, STATUS_COMPANY_NOT_STORED)
        self.assertGreater(len(model.requests), 0)


class HeldSymbolTests(AvailabilityTestCase):
    def test_an_ingested_company_still_runs_normally(self):
        script = [
            route(),
            calls("Checking.", call("c1", "market_insider_analysis")),
            say("Enough."),
            findings("Checked.", refs=("E1",)),
            say("It moved [E1]."),
        ]

        result, _ = self.run_it(script, question="How did NVDA move?")

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(result.symbol, "NVDA")


class UnresolvedNameTests(AvailabilityTestCase):
    def test_a_company_name_is_a_clarification_not_a_refusal(self):
        """Not recognising a name is not evidence that nothing is stored under it."""
        script = [
            route(symbol="Apple Inc.", period="none", start_date=None, end_date=None),
            say("Which ticker do you mean?"),
        ]

        result, _ = self.run_it(script, question="How is Apple doing?")

        self.assertEqual(result.status, STATUS_CLARIFICATION_NEEDED)
        self.assertNotEqual(result.status, STATUS_COMPANY_NOT_STORED)
        self.assertIsNotNone(result.answer)

    def test_the_clarification_names_the_symbol_it_could_not_resolve(self):
        script = [
            route(symbol="the chip maker", period="none", start_date=None, end_date=None),
            say("Which ticker do you mean?"),
        ]

        result, _ = self.run_it(script)

        self.assertIn("the chip maker", (result.route_reason or "").lower())


class DatabaseFailureTests(AvailabilityTestCase):
    def test_an_unreadable_database_is_a_service_failure_never_an_absence(self):
        """An outage must not become a confident falsehood about the data."""
        with mock.patch(
            "app.agent.run.read_known_symbols",
            side_effect=OperationalError("SELECT 1", {}, Exception("connection lost")),
        ):
            result, model = self.run_it(
                [route(symbol="TSLA", period="none", start_date=None, end_date=None)],
                known_symbols=None,
            )

        self.assertEqual(result.status, STATUS_SERVICE_FAILED)
        self.assertNotEqual(result.status, STATUS_COMPANY_NOT_STORED)
        self.assertIn("not a statement that any company is missing", " ".join(result.warnings))
        self.assertEqual(len(model.requests), 0)

    def test_a_missing_api_key_is_still_a_configuration_error(self):
        """The two used to share a status; they are different failures with different fixes."""
        with mock.patch("app.agent.run._settings") as settings:
            settings.return_value = mock.Mock(
                deepseek_api_key=None,
                deepseek_model="deepseek-flash",
                deepseek_base_url="https://api.deepseek.com",
                deepseek_timeout_seconds=60.0,
            )
            result = run_analysis(question="Anything?", reference_date=REFERENCE)

        self.assertEqual(result.status, "configuration_error")
        self.assertTrue(any("DEEPSEEK_API_KEY" in w for w in result.warnings))


class UnsupportedStillUnsupportedTests(AvailabilityTestCase):
    def test_trading_is_still_an_unsupported_capability(self):
        """The correction must not have swallowed the real unsupported cases."""
        script = [
            route(
                "unsupported_capability",
                symbol=None,
                period="none",
                start_date=None,
                end_date=None,
                unsupported_reason="placing orders is not available",
            ),
            say("I cannot place orders."),
        ]

        result, _ = self.run_it(script, question="Buy me 100 shares of NVDA.")

        self.assertEqual(result.status, STATUS_UNSUPPORTED_CAPABILITY)
        self.assertNotEqual(result.status, STATUS_COMPANY_NOT_STORED)


if __name__ == "__main__":
    unittest.main()
