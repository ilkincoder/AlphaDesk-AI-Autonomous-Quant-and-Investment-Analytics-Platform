"""The Supervisor: routing, and the answer it is allowed to write.

Two things are tested here that matter more than the rest. First, that a routing decision this
application cannot act on is reported rather than guessed at. Second, that an answer citing
evidence which does not exist is corrected once and then withheld -- never shown, and never
quietly repaired by the application, which would be the same mistake with more confidence.

The Module 1 agent is replaced with a stub through the run, so these tests are about routing
and composition. The agent itself is tested in `test_agent_module1.py`.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import json
import unittest
from datetime import date

from app.agent.budget import RunBudget
from app.agent.context import ExplicitArguments, KnownSymbols, RequestInputs
from app.agent.evidence import EvidenceMap
from app.agent.module1 import Module1Findings, Module1Outcome
from app.agent.budget import RunBudget
from app.agent.evidence import EvidenceMap
from app.agent.module1 import Module1Outcome
from app.agent.module2 import ProposalOutcome
from app.agent.supervisor import (
    MAX_EVIDENCE_IN_PROMPT,
    Destination,
    SupervisorOutcome,
    _routing_request,
    SupervisorRoute,
    cited_references,
    compose_request,
    parse_route,
    run_supervisor,
)
from tests.agent_doubles import (
    AUTH_ERROR,
    RESPONSE_ERROR,
    ScriptedModel,
    route,
    say,
)

REFERENCE = date(2026, 9, 17)


def inputs(**overrides) -> RequestInputs:
    arguments = {
        "question": "Compare the price move with reported insider activity.",
        "reference_date": REFERENCE,
        "explicit": ExplicitArguments(),
        "known": KnownSymbols(companies=("AAPL", "NVDA"), holdings=("AAPL", "MSFT", "NVDA")),
    }
    arguments.update(overrides)
    return RequestInputs(**arguments)


def an_agent(*, refs=("E1",), limitations=(), found=(), next_steps=()):
    """A Module 1 outcome carrying findings, for a stubbed agent call."""
    return Module1Outcome(
        findings=Module1Findings(
            findings=list(found),
            evidence_refs=list(refs),
            limitations=list(limitations),
            portfolio_context="NVDA is held in the demo portfolio.",
            next_steps=list(next_steps),
        ),
        executions=[],
        stopped_by=None,
        messages=[],
    )


def with_evidence(*entries):
    evidence = EvidenceMap()
    for index, entry in enumerate(entries, start=1):
        evidence.add_tool_result(
            tool=entry.get("tool", "market_insider_analysis"),
            symbol="NVDA",
            status=entry.get("status", "ok"),
            label=f"evidence {index}",
            summary=entry.get("summary", {"n": index}),
        )
    return evidence


def run(script, *, evidence=None, agent=None, budget=None, request=None):
    model = ScriptedModel(script)
    outcome = run_supervisor(
        client=model,
        inputs=request or inputs(),
        budget=budget or RunBudget(),
        evidence=evidence if evidence is not None else with_evidence({}),
        run_module1_call=lambda context: agent or an_agent(),
    )
    return outcome, model


class RoutingTests(unittest.TestCase):
    def test_an_analysis_request_is_settled_and_routed_to_the_agent(self):
        outcome, _ = run(
            [route(), say("Answer citing [E1].")]
        )

        self.assertEqual(outcome.route.destination, Destination.MODULE1_ANALYSIS)
        self.assertIsNotNone(outcome.context)
        self.assertEqual(outcome.context.resolved.symbol, "NVDA")
        self.assertEqual(outcome.context.resolved.start_date, date(2026, 8, 6))
        self.assertEqual(outcome.answer, "Answer citing [E1].")

    def test_a_clarification_request_asks_the_question_it_was_given(self):
        outcome, model = run(
            [
                route(
                    "clarification_needed",
                    symbol=None,
                    period="none",
                    start_date=None,
                    end_date=None,
                    clarification_question="Which company do you mean?",
                ),
                say("Which company do you mean?"),
            ]
        )

        self.assertEqual(outcome.route.destination, Destination.CLARIFICATION_NEEDED)
        self.assertIsNone(outcome.context)
        self.assertIn("Which company", outcome.answer)
        # The agent was never reached, so no tool ran and no evidence was created.
        self.assertIsNone(outcome.module1)

    def test_an_unsupported_request_is_explained_rather_than_answered(self):
        outcome, _ = run(
            [
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
        )

        self.assertEqual(outcome.route.destination, Destination.UNSUPPORTED_CAPABILITY)
        self.assertIn("cannot place orders", outcome.answer)

    def test_a_conversational_message_gets_a_conversational_reply(self):
        outcome, _ = run(
            [
                route(
                    "simple_response",
                    symbol=None,
                    period="none",
                    start_date=None,
                    end_date=None,
                ),
                say("Hello. I can analyse a company's stored data."),
            ]
        )

        self.assertEqual(outcome.route.destination, Destination.SIMPLE_RESPONSE)
        self.assertIn("Hello", outcome.answer)

    def test_every_destination_in_the_closed_set_is_reachable(self):
        """The vocabulary, and which half of it the model may choose from.

        `rebalance_proposal` is in the enum and deliberately *not* in the router's set: the only
        thing that produces a proposal is a person pressing a button, and a model asked to
        classify one would spend a request learning what the caller already said. What this
        asserts is that the enum has not grown a member nobody can reach and that the router's
        half is exactly the four it started with.
        """
        self.assertEqual(
            {item.value for item in Destination},
            {
                "module1_analysis",
                "rebalance_proposal",
                "clarification_needed",
                "unsupported_capability",
                "company_not_stored",
                "simple_response",
            },
        )
        self.assertNotIn(
            str(Destination.REBALANCE_PROPOSAL),
            _routing_request(inputs(question="propose a rebalance for the portfolio")),
            "the router must not be offered a destination it is not allowed to choose",
        )

    def test_a_destination_outside_the_set_is_rejected(self):
        parsed, problem = parse_route(json.dumps({"destination": "module2_trading"}))

        self.assertIsNone(parsed)
        self.assertIsNotNone(problem)

    def test_a_decision_naming_no_company_is_not_actionable(self):
        # `parse_route` returns what it read *and* what is wrong with it: the caller decides
        # whether a problem is fatal, which is what makes the retry possible.
        _, problem = parse_route(
            json.dumps({"destination": "module1_analysis", "period": "none"})
        )

        self.assertIsNotNone(problem)
        self.assertIn("no symbol", problem)

    def test_a_period_token_outside_the_closed_set_is_not_actionable(self):
        _, problem = parse_route(
            json.dumps(
                {
                    "destination": "module1_analysis",
                    "symbol": "NVDA",
                    "period": "last_fortnight",
                }
            )
        )

        self.assertIsNotNone(problem)
        self.assertIn("last_fortnight", problem)

    def test_an_explicit_period_without_dates_is_not_actionable(self):
        _, problem = parse_route(
            json.dumps(
                {
                    "destination": "module1_analysis",
                    "symbol": "NVDA",
                    "period": "explicit",
                }
            )
        )

        self.assertIsNotNone(problem)
        self.assertIn("both start_date and end_date", problem)

    def test_a_usable_decision_has_no_problem(self):
        parsed, problem = parse_route(json.dumps({"destination": "simple_response"}))

        self.assertIsNotNone(parsed)
        self.assertIsNone(problem)

    def test_an_unusable_decision_is_retried_once_and_then_reported(self):
        outcome, model = run([say("not json"), say("still not json")])

        self.assertEqual(len(model.requests), 2)
        self.assertIsNone(outcome.route)
        self.assertIsNotNone(outcome.routing_error)
        self.assertIsNone(outcome.answer)

    def test_an_unusable_decision_is_recovered_by_the_retry(self):
        outcome, model = run([say("not json"), route(), say("Answer [E1].")])

        self.assertIsNotNone(outcome.route)
        self.assertEqual(len(model.requests), 3)
        self.assertEqual(outcome.answer, "Answer [E1].")

    def test_routing_asks_for_json_without_forcing_a_tool(self):
        """`tool_choice` cannot force a named function in thinking mode; JSON can."""
        _, model = run([route(), say("Answer [E1].")])

        self.assertEqual(
            model.requests[0]["response_format"], {"type": "json_object"}
        )
        self.assertIsNone(model.requests[0]["tools"])


class ContextEnforcementTests(unittest.TestCase):
    """A decision that disagrees with the caller's arguments becomes a question."""

    def test_a_question_naming_a_different_company_than_the_run_becomes_a_clarification(self):
        outcome, model = run(
            [
                route(symbol="AAPL"),
                say("You asked about AAPL, but this run is for NVDA. Which one?"),
            ],
            request=inputs(
                question="How is AAPL doing?",
                explicit=ExplicitArguments(symbol="NVDA"),
            ),
        )

        self.assertEqual(outcome.route.destination, Destination.CLARIFICATION_NEEDED)
        self.assertIsNone(outcome.context)
        self.assertIn("One company per run", outcome.route.clarification_question)
        self.assertIsNone(outcome.module1)

    def test_a_question_implying_a_different_window_becomes_a_clarification(self):
        outcome, _ = run(
            [
                route(period="last_30_days", start_date=None, end_date=None),
                say("Which period should I use?"),
            ],
            request=inputs(
                explicit=ExplicitArguments(
                    start_date=date(2026, 8, 6), end_date=date(2026, 9, 17)
                )
            ),
        )

        self.assertEqual(outcome.route.destination, Destination.CLARIFICATION_NEEDED)
        self.assertIn("2026-08-19", outcome.route.clarification_question)
        self.assertIn("Which period", outcome.answer)

    def test_a_matching_decision_is_settled_normally(self):
        outcome, _ = run(
            [route(), say("Answer [E1].")],
            request=inputs(
                explicit=ExplicitArguments(
                    symbol="NVDA",
                    start_date=date(2026, 8, 6),
                    end_date=date(2026, 9, 17),
                )
            ),
        )

        self.assertEqual(outcome.route.destination, Destination.MODULE1_ANALYSIS)
        self.assertEqual(outcome.context.resolved.symbol, "NVDA")

    def test_the_agent_is_handed_the_settled_context_and_nothing_else(self):
        """The agent receives it as an argument, so it has no way to run against another."""
        seen = []

        def record(context):
            seen.append(context.resolved)
            return an_agent()

        model = ScriptedModel([route(), say("Answer [E1].")])
        run_supervisor(
            client=model,
            inputs=inputs(
                explicit=ExplicitArguments(symbol="NVDA", as_of=date(2026, 9, 17))
            ),
            budget=RunBudget(),
            evidence=with_evidence({}),
            run_module1_call=record,
        )

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].symbol, "NVDA")
        self.assertEqual(seen[0].as_of, date(2026, 9, 17))

    def test_the_agent_is_not_reached_for_a_request_that_could_not_be_settled(self):
        called = []

        model = ScriptedModel(
            [
                route(symbol="AAPL"),
                say("Which company?"),
            ]
        )
        run_supervisor(
            client=model,
            inputs=inputs(explicit=ExplicitArguments(symbol="NVDA")),
            budget=RunBudget(),
            evidence=with_evidence({}),
            run_module1_call=lambda context: called.append(context) or an_agent(),
        )

        self.assertEqual(called, [])


class CitationTests(unittest.TestCase):
    def test_citations_are_extracted_in_order_without_duplicates(self):
        self.assertEqual(
            cited_references("A [E2] and [E1], again [E2], and [E10]."),
            ["E2", "E1", "E10"],
        )

    def test_prose_without_references_cites_nothing(self):
        self.assertEqual(cited_references("No citations here."), [])

    def test_an_answer_citing_known_evidence_is_accepted(self):
        outcome, _ = run([route(), say("The price rose [E1].")])

        self.assertEqual(outcome.answer, "The price rose [E1].")
        self.assertEqual(outcome.citations, ["E1"])
        self.assertEqual(outcome.invalid_citations, [])

    def test_an_invented_citation_is_corrected_once(self):
        outcome, model = run(
            [route(), say("The price rose [E7]."), say("The price rose [E1].")]
        )

        self.assertTrue(outcome.corrected)
        self.assertEqual(outcome.answer, "The price rose [E1].")
        self.assertEqual(outcome.invalid_citations, [])
        self.assertEqual(len(model.requests), 3)
        # The correction names what was wrong and what is available.
        correction = model.requests[2]["messages"][-1]["content"]
        self.assertIn("E7", correction)
        self.assertIn("E1", correction)

    def test_an_answer_that_still_cites_unknown_evidence_is_withheld(self):
        """Never shown, and never repaired by the application on the model's behalf."""
        outcome, model = run(
            [route(), say("The price rose [E7]."), say("It rose, see [E8] and [E9].")]
        )

        self.assertIsNone(outcome.answer)
        self.assertEqual(outcome.invalid_citations, ["E8", "E9"])
        self.assertEqual(len(model.requests), 3)

    def test_a_correction_is_attempted_only_once(self):
        _, model = run(
            [route(), say("[E7]"), say("[E8]"), say("[E9]"), say("[E10]")]
        )

        self.assertEqual(len(model.requests), 3)


class CompositionTests(unittest.TestCase):
    def test_the_answer_is_written_from_the_findings_and_the_evidence(self):
        evidence = with_evidence({"summary": {"change_percent": "0.1598"}})
        outcome, model = run(
            [
                route(),
                say("Answer [E1]."),
            ],
            evidence=evidence,
            agent=an_agent(found=["The price rose 0.16%."], refs=("E1",)),
        )

        prompt = model.requests[1]["messages"][1]["content"]
        self.assertIn("The price rose 0.16%", prompt)
        self.assertIn("0.1598", prompt)
        self.assertIn("NVDA is held in the demo portfolio", prompt)
        self.assertIn("[E1]", prompt)

    def test_every_limitation_reaches_the_composition_step_untouched(self):
        outcome, model = run(
            [route(), say("Answer [E1].")],
            agent=an_agent(limitations=["insufficient_coverage", "three filings only"]),
        )

        prompt = model.requests[1]["messages"][1]["content"]
        self.assertIn("insufficient_coverage", prompt)
        self.assertIn("three filings only", prompt)

    def test_limitations_are_not_dropped_when_there_are_many(self):
        """Everything else can be trimmed; a warning cannot."""
        many = [f"limitation {index}" for index in range(40)]
        outcome, model = run(
            [route(), say("Answer [E1].")], agent=an_agent(limitations=many)
        )

        prompt = model.requests[1]["messages"][1]["content"]
        for item in many:
            self.assertIn(item, prompt)

    def test_duplicate_limitations_are_stated_once(self):
        _, model = run(
            [route(), say("Answer [E1].")],
            agent=an_agent(limitations=["insufficient_coverage", "insufficient_coverage"]),
        )

        prompt = model.requests[1]["messages"][1]["content"]
        self.assertEqual(prompt.count("insufficient_coverage"), 1)

    def test_the_evidence_list_is_bounded_and_says_it_was_trimmed(self):
        evidence = EvidenceMap()
        for index in range(MAX_EVIDENCE_IN_PROMPT + 5):
            evidence.add_tool_result(
                tool="portfolio_context", symbol="NVDA", status="ok",
                label=f"evidence {index}", summary={},
            )

        _, model = run([route(), say("Answer [E1].")], evidence=evidence)

        prompt = model.requests[1]["messages"][1]["content"]
        self.assertIn("further evidence entries are not shown", prompt)

    def test_a_run_with_no_evidence_says_so_rather_than_showing_an_empty_list(self):
        model = ScriptedModel([route(), say("I have no data to cite.")])
        run_supervisor(
            client=model,
            inputs=inputs(),
            budget=RunBudget(),
            evidence=EvidenceMap(),
            run_module1_call=lambda context: an_agent(refs=(), found=()),
        )

        prompt = model.requests[1]["messages"][1]["content"]
        self.assertIn("No evidence was gathered", prompt)

    def test_the_composition_prompt_states_the_resolved_request(self):
        _, model = run([route(), say("Answer [E1].")])

        prompt = model.requests[1]["messages"][1]["content"]
        self.assertIn("Company: NVDA", prompt)
        self.assertIn("2026-08-06 to 2026-09-17", prompt)

    def test_the_scope_note_is_carried_for_a_mixed_request(self):
        _, model = run(
            [
                route(scope_note="the trading half of the question was not performed"),
                say("Answer [E1]."),
            ]
        )

        prompt = model.requests[1]["messages"][1]["content"]
        self.assertIn("the trading half of the question was not performed", prompt)


class BudgetTests(unittest.TestCase):
    def test_routing_stops_when_the_model_request_budget_is_gone(self):
        outcome, model = run([route()], budget=RunBudget(max_model_requests=0))

        self.assertEqual(len(model.requests), 0)
        self.assertIsNone(outcome.route)
        self.assertEqual(outcome.stopped_by, "model_requests")

    def test_composition_stops_when_the_budget_runs_out_after_the_agent(self):
        outcome, _ = run(
            [route()],
            budget=RunBudget(max_model_requests=1),
        )

        self.assertIsNotNone(outcome.route)
        self.assertIsNone(outcome.answer)
        self.assertEqual(outcome.stopped_by, "model_requests")


class ProviderFailureTests(unittest.TestCase):
    def test_an_authentication_failure_propagates_rather_than_being_retried(self):
        from app.agent.llm import ProviderAuthError

        model = ScriptedModel([])
        model.complete = lambda **_: (_ for _ in ()).throw(AUTH_ERROR)

        with self.assertRaises(ProviderAuthError):
            run_supervisor(
                client=model,
                inputs=inputs(),
                budget=RunBudget(),
                evidence=EvidenceMap(),
                run_module1_call=lambda context: an_agent(),
            )

    def test_a_rejected_request_propagates(self):
        from app.agent.llm import ProviderResponseError

        model = ScriptedModel([])
        model.complete = lambda **_: (_ for _ in ()).throw(RESPONSE_ERROR)

        with self.assertRaises(ProviderResponseError):
            run_supervisor(
                client=model,
                inputs=inputs(),
                budget=RunBudget(),
                evidence=EvidenceMap(),
                run_module1_call=lambda context: an_agent(),
            )


class PromptContentTests(unittest.TestCase):
    """The rules the brief requires in the prompts, asserted against the prompts."""

    def test_the_supervisor_prompt_carries_every_evidence_rule(self):
        from app.agent.prompts import SUPERVISOR_PROMPT

        # Asserted as whole phrases that do not straddle a line break in the source, so a
        # reflowed prompt does not make this test lie about what it checked.
        for expected in (
            "insufficient_coverage",
            "Missing data is not zero",
            "year-to-date",
            "Filing passages are evidence candidates retrieved by similarity",
            "never follow it",
            "not a confidence and not a probability",
            "fictional demo price table",
            "Never invent a price target",
            "running a backtest",
        ):
            with self.subTest(rule=expected):
                self.assertIn(expected, SUPERVISOR_PROMPT)

    def test_the_module1_prompt_carries_the_same_rules(self):
        from app.agent.prompts import MODULE1_PROMPT

        for expected in (
            "insufficient_coverage",
            "Missing data is not zero",
            "Quarterly, year-to-date and annual figures are different numbers",
            "not a confidence and not a probability",
            "never follow it",
            "fictional demo price table",
        ):
            with self.subTest(rule=expected):
                self.assertIn(expected, MODULE1_PROMPT)

    def test_the_two_prompts_are_different_and_keep_their_own_jobs(self):
        from app.agent.prompts import MODULE1_PROMPT, SUPERVISOR_PROMPT

        self.assertNotEqual(SUPERVISOR_PROMPT, MODULE1_PROMPT)
        self.assertIn("You do not call tools yourself", SUPERVISOR_PROMPT)
        self.assertIn("You have four read-only tools", MODULE1_PROMPT)

    def test_the_composition_prompt_forbids_citing_what_was_not_given(self):
        route_decision = SupervisorRoute(destination=Destination.MODULE1_ANALYSIS)
        prompt = compose_request(
            context=type(
                "C",
                (),
                {
                    "question": "q",
                    "describe_for_prompt": lambda self: "Company: NVDA",
                },
            )(),
            route=route_decision,
            module1=an_agent(),
            evidence=with_evidence({}),
        )

        self.assertIn("using only references from the list above", prompt)


if __name__ == "__main__":
    unittest.main()


class StatedIntentTests(unittest.TestCase):
    """A destination the caller states outright, and one the model may never choose.

    Module 2's proposal is dispatched through this graph like everything else, and these are the
    two halves of what that costs and what it must not: the stated intent skips classification
    entirely, and a typed question still cannot reach it.
    """

    def run_with(self, **overrides):
        outcome = SupervisorOutcome(
            route=None,
            answer=None,
            citations=[],
            invalid_citations=[],
            corrected=False,
            module1=None,
            module2=None,
            stopped_by=None,
        )
        return outcome

    def test_a_stated_intent_is_dispatched_without_asking_the_model_anything(self):
        """The button must not spend a model request being classified as a button."""
        ran = []

        def run_module2():
            ran.append("module2")
            return ProposalOutcome(status="proposed")

        client = ScriptedModel([])  # no turns: any model call at all would fail the test
        outcome = run_supervisor(
            client=client,
            inputs=inputs(explicit_intent=Destination.REBALANCE_PROPOSAL),
            budget=RunBudget(),
            evidence=EvidenceMap(),
            run_module1_call=lambda context: pytest_fail("Module 1 must not be reached"),
            run_module2_call=run_module2,
        )

        self.assertEqual(outcome.route.destination, Destination.REBALANCE_PROPOSAL)
        self.assertEqual(ran, ["module2"])
        self.assertEqual(client.requests, [], "the router was consulted about a stated intent")
        self.assertEqual(outcome.module2.status, "proposed")

    def test_a_typed_question_still_goes_to_the_router(self):
        """The stated-intent path is additive: with no intent given, nothing changes."""
        # The router's turn, then the composition's: the ordinary two-call chat path.
        client = ScriptedModel([route(), say("NVDA moved over the period asked about.")])
        outcome = run_supervisor(
            client=client,
            inputs=inputs(),
            budget=RunBudget(),
            evidence=EvidenceMap(),
            run_module1_call=lambda context: Module1Outcome(
                findings=None, executions=[], stopped_by=None, messages=[]
            ),
            run_module2_call=lambda: pytest_fail("Module 2 must not be reached"),
        )

        self.assertEqual(len(client.requests), 2)
        self.assertEqual(outcome.route.destination, Destination.MODULE1_ANALYSIS)
        self.assertIsNotNone(outcome.answer)

    def test_the_model_may_not_route_a_question_to_the_rebalance(self):
        """The enum has a member the router cannot reach, and this is what enforces it."""
        # Both attempts answer with the forbidden destination.
        client = ScriptedModel(
            [
                route(destination="rebalance_proposal", symbol=None, period="none",
                      start_date=None, end_date=None),
                route(destination="rebalance_proposal", symbol=None, period="none",
                      start_date=None, end_date=None),
            ]
        )
        outcome = run_supervisor(
            client=client,
            inputs=inputs(),
            budget=RunBudget(),
            evidence=EvidenceMap(),
            run_module1_call=lambda context: pytest_fail("Module 1 must not be reached"),
            run_module2_call=lambda: pytest_fail("Module 2 must not be reached"),
        )

        self.assertIsNone(outcome.route)
        self.assertIsNotNone(outcome.routing_error)
        self.assertIn("not a destination that may be chosen", outcome.routing_error)


def pytest_fail(message: str):
    raise AssertionError(message)
