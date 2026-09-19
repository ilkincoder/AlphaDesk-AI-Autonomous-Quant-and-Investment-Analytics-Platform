"""The three dates a run distinguishes, and the one that must never be swapped for another.

Two of them are routinely different numbers, and the difference is not cosmetic. A run asked
today about a window that ended days ago has an information date of today and a comparison
period ending days ago. A filing accepted in between is readable for the filing discussion and
**excluded from the market comparison** -- so an answer that quotes the market tool's cutoff as
the run's own can attribute evidence to a comparison that ruled it out.

That is what happened in the first live conversation: the run's `as_of` was 2026-09-19 while
`market_insider_analysis` reported 2026-09-18T04:00Z, and the answer presented the tool's figure
as the overall cutoff. These tests pin the distinction.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date

from app.agent.context import ExplicitArguments, KnownSymbols, resolve_request, RunContext
from app.agent.run import STATUS_CLARIFICATION_NEEDED, STATUS_COMPLETED, run_analysis
from app.agent.evidence import EvidenceMap
from app.agent.supervisor import (
    DATE_RULES,
    Destination,
    SupervisorRoute,
    compose_request,
)
from app.analysis import information_cutoff
from tests.agent_doubles import ScriptedModel, call, calls, findings, route, say, tool_sessions
from tests.test_tools_contracts import ToolTestCase

# The live run's shape: a window ending on the 17th, asked on the 19th.
WINDOW_END = date(2026, 9, 17)
ASKED_ON = date(2026, 9, 19)

KNOWN = KnownSymbols(companies=("AAPL", "NVDA"), holdings=("AAPL", "MSFT", "NVDA"))


def a_context(*, as_of: date, start: date | None, end: date | None) -> RunContext:
    outcome = resolve_request(
        reference_date=ASKED_ON,
        explicit=ExplicitArguments(),
        symbol="NVDA",
        period="explicit" if start else "none",
        start_date=start,
        end_date=end,
        as_of=as_of,
    )
    assert outcome.settled, outcome.conflict
    return RunContext(
        question="Compare NVDA's price movement with insider activity.",
        reference_date=ASKED_ON,
        resolved=outcome.resolved,
        explicit=ExplicitArguments(),
        known=KNOWN,
    )


class DateDescriptionTests(unittest.TestCase):
    """What the prompts are told, and where those values come from."""

    def test_the_comparison_period_and_the_information_date_are_labelled_separately(self):
        described = a_context(as_of=ASKED_ON, start=date(2026, 8, 6), end=WINDOW_END)

        text = described.dates_for_prompt()

        self.assertIn("Market comparison period: 2026-08-06 to 2026-09-17", text)
        self.assertIn("Overall information date (as_of): 2026-09-19", text)

    def test_a_question_with_no_window_says_so_rather_than_showing_blank_dates(self):
        described = a_context(as_of=ASKED_ON, start=None, end=None)

        text = described.dates_for_prompt()

        self.assertIn("does not compare a market window", text)
        self.assertIn("Overall information date (as_of): 2026-09-19", text)

    def test_the_values_are_the_ones_the_application_resolved(self):
        described = a_context(as_of=ASKED_ON, start=date(2026, 8, 6), end=WINDOW_END)

        text = described.describe_for_prompt()

        # Taken from `resolved`, so the prompt and the structured result cannot disagree.
        self.assertIn(described.resolved.as_of.isoformat(), text)
        self.assertIn(described.resolved.start_date.isoformat(), text)
        self.assertIn(described.resolved.end_date.isoformat(), text)

    def test_a_tool_cutoff_is_not_offered_as_one_of_this_run_s_dates(self):
        """The third date is stated as a rule, never as a number.

        Any number here would have to be read out of a tool payload, which is exactly the
        confusion this distinguishes against.
        """
        described = a_context(as_of=ASKED_ON, start=date(2026, 8, 6), end=WINDOW_END)

        text = described.dates_for_prompt()

        self.assertNotIn(information_cutoff(WINDOW_END).isoformat(), text)
        self.assertIn("Dates this run distinguishes", text)


class CompositionRuleTests(unittest.TestCase):
    """The rule the answer writer is given, and that it reaches the prompt."""

    def test_the_attribution_rule_is_in_the_composition_prompt(self):
        described = a_context(as_of=ASKED_ON, start=date(2026, 8, 6), end=WINDOW_END)

        prompt = compose_request(
            context=described,
            route=SupervisorRoute(destination=Destination.MODULE1_ANALYSIS),
            module1=None,
            evidence=EvidenceMap(),
        )

        self.assertIn("Market comparison period", prompt)
        self.assertIn("Overall information date", prompt)
        self.assertIn("Do **not** attribute anything admitted under the wider information date", prompt)

    def test_the_rule_names_all_three_dates(self):
        for expected in (
            "market comparison period",
            "overall information date",
            "its own",
        ):
            with self.subTest(part=expected):
                self.assertIn(expected, DATE_RULES)

    def test_the_rule_says_a_tool_cutoff_is_not_the_run_s(self):
        self.assertIn("never this run's", DATE_RULES)


class RunCutoffTests(ToolTestCase):
    """The machine-readable cutoff on the result."""

    def setUp(self) -> None:
        super().setUp()
        self.company = self.add_company()
        self.add_price_range(self.company, date(2026, 8, 6), 5, first="100", step="2")
        self.sessions = tool_sessions(self.session)
        self.sessions.__enter__()
        self.addCleanup(lambda: self.sessions.__exit__(None, None, None))

    def run_it(self, script, **overrides):
        arguments = {
            "question": "Compare the price with insider activity.",
            "reference_date": ASKED_ON,
            "symbol": "NVDA",
            "start_date": date(2026, 8, 6),
            "end_date": WINDOW_END,
            "known_symbols": KNOWN,
            "run_id": "run-test",
        }
        arguments.update(overrides)
        return run_analysis(client=ScriptedModel(list(script)), **arguments)

    def analysis_script(self):
        # The model picks the information date, as it did live: today, which is later than the
        # window it was asked about. That is what makes the two cutoffs different numbers.
        return [
            route(as_of=ASKED_ON.isoformat()),
            calls("Checking.", call("c1", "market_insider_analysis")),
            say("Enough."),
            findings("Checked.", refs=("E1",)),
            say("It moved [E1]."),
        ]

    def test_the_run_s_cutoff_is_derived_from_its_own_information_date(self):
        result = self.run_it(self.analysis_script())

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(result.resolved["as_of"], ASKED_ON.isoformat())
        self.assertEqual(result.information_cutoff, information_cutoff(ASKED_ON))

    def test_it_differs_from_the_market_tool_s_own_cutoff(self):
        """The live run's shape: a window ending on the 17th, asked on the 19th."""
        result = self.run_it(self.analysis_script())
        self.assertEqual(result.resolved["as_of"], ASKED_ON.isoformat())

        marketplace = result.evidence[0]["summary"]["period"]["information_cutoff"]
        self.assertEqual(marketplace, information_cutoff(WINDOW_END).isoformat())
        self.assertEqual(
            result.information_cutoff.isoformat(), information_cutoff(ASKED_ON).isoformat()
        )
        # Two different instants, and the run's is the later one.
        self.assertNotEqual(result.information_cutoff.isoformat(), marketplace)
        self.assertGreater(result.information_cutoff, information_cutoff(WINDOW_END))

    def test_a_run_with_no_settled_request_has_no_cutoff(self):
        """A clarification settles nothing, so it has no information date to report."""
        result = self.run_it(
            [
                route(
                    "clarification_needed",
                    symbol="NVDA",
                    period="none",
                    start_date=None,
                    end_date=None,
                    clarification_question="Which period?",
                ),
                say("Which period?"),
            ],
            start_date=None,
            end_date=None,
        )

        self.assertEqual(result.status, STATUS_CLARIFICATION_NEEDED)
        self.assertIsNone(result.resolved)
        self.assertIsNone(result.information_cutoff)

    def test_the_result_survives_json_with_the_cutoff(self):
        """The instant is what matters; the spelling of UTC is the serialiser's business.

        Pydantic writes a UTC datetime as `...Z` where `isoformat()` writes `+00:00`. Comparing
        the two as strings would fail on a difference that is not a difference, so the test
        parses them back.
        """
        import json
        from datetime import datetime

        result = self.run_it(self.analysis_script())
        document = json.loads(json.dumps(result.as_json()))

        self.assertIsInstance(document["information_cutoff"], str)
        self.assertEqual(
            datetime.fromisoformat(document["information_cutoff"]),
            information_cutoff(ASKED_ON),
        )


class KnownSymbolsDescriptionTests(unittest.TestCase):
    """The two lists, and the third line that says what they do not mean."""

    def test_held_but_not_ingested_symbols_are_named(self):
        described = KNOWN.describe_for_prompt()

        self.assertIn("held but NOT ingested", described)
        self.assertIn("MSFT", described)

    def test_the_capability_of_each_list_is_stated(self):
        described = KNOWN.describe_for_prompt()

        self.assertIn("market, insiders, financial facts, filing text", described)
        self.assertIn("ownership questions only", described)

    def test_nothing_extra_is_claimed_when_every_held_symbol_is_ingested(self):
        known = KnownSymbols(companies=("AAPL", "NVDA"), holdings=("AAPL", "NVDA"))

        self.assertEqual(known.held_only, ())
        self.assertNotIn("held but NOT ingested", known.describe_for_prompt())

    def test_held_only_ignores_case_and_whitespace(self):
        known = KnownSymbols(companies=("nvda",), holdings=(" NVDA ", "MSFT"))

        self.assertEqual(known.held_only, ("MSFT",))

    def test_a_company_that_is_ingested_but_not_held_is_not_held_only(self):
        known = KnownSymbols(companies=("AAPL", "NVDA"), holdings=("AAPL",))

        self.assertEqual(known.held_only, ())


if __name__ == "__main__":
    unittest.main()
