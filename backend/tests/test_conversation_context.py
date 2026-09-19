"""What a continuing conversation actually does, end to end.

`test_conversations_api.py` checks the coordination with a scripted runner: duplicates, leases,
HTTP statuses. This file checks the other half, and it uses the **real** `run_analysis` with
only the model scripted -- so the Supervisor, the routing prompt, the context enforcement and
the tools are all the real ones.

The test that matters most is the required example. A user asks about NVDA's price and insider
activity without saying over what period; the Supervisor asks which period; the user replies
with nothing but a date range. That reply has to continue the original question. Getting this
wrong is easy and invisible: a date-only message analysed as its own request produces a
confident answer to a question nobody asked.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import json
import unittest
from datetime import date, timedelta
from unittest import mock

from app.agent.context import (
    MAX_CONTEXT_ANSWER_CHARS,
    MAX_CONTEXT_TURNS,
    ConversationContext,
    PendingClarification,
    PriorTurn,
    SettledContext,
)
from app.agent.run import STATUS_CLARIFICATION_NEEDED, STATUS_COMPLETED, run_analysis
from app.analysis_api import post_chat
from app.schemas import ChatRequest
from tests.agent_doubles import ScriptedModel, call, calls, findings, route, say, tool_sessions
from tests.conversation_doubles import conversation_sessions
from tests.test_tools_contracts import ToolTestCase

REFERENCE = date(2026, 9, 17)
WINDOW = (date(2026, 8, 6), date(2026, 9, 17))

# The user's first question: a real analysis, with no period in it.
ORIGINAL_QUESTION = "Compare NVDA's price movement with insider activity."

# What the Supervisor asks back, and what the user replies. The reply carries nothing but
# dates, which is exactly why it cannot be analysed on its own.
CLARIFICATION = "Which period should I look at?"
DATE_REPLY = "August 6 through September 17, 2026."


def a_full_run(*, route_decision, tools=(), answer=None):
    """A script for one whole successful run, given its routing decision.

    The decision may be the `(content, calls)` pair `route()` produces or a bare JSON string,
    because the tests build both: some use the helper, and the ones that need to show exactly
    what the model returned write the JSON out.
    """
    first = route_decision if isinstance(route_decision, tuple) else (route_decision, [])
    script = [first]
    if tools:
        script.append(calls("Checking.", *tools))
    # The agent always gets a turn in which it chooses to stop asking for tools, whether or
    # not it asked for any.
    script.append(say("That is enough."))
    script.append(findings("Something was found.", refs=("E1",) if tools else ()))
    # An answer that cites when there is nothing to cite would be corrected, which would add
    # a turn the caller did not script -- and would hide what the test is actually about.
    script.append(say(answer or ("An answer [E1]." if tools else "An answer.")))
    return script


class ConversationContextTestCase(ToolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.company = self.add_company()
        self.add_price_range(self.company, WINDOW[0], 5, first="100", step="2")
        self.sessions = tool_sessions(self.session)
        self.sessions.__enter__()
        self.addCleanup(lambda: self.sessions.__exit__(None, None, None))
        self.conversations = conversation_sessions(self.session)
        self.conversations.__enter__()
        self.addCleanup(lambda: self.conversations.__exit__(None, None, None))

    def run_it(self, script, *, conversation=None, question="q", **overrides):
        arguments = {
            "reference_date": REFERENCE,
            "run_id": "run-test",
        }
        arguments.update(overrides)
        model = ScriptedModel(script)
        return run_analysis(client=model, question=question, conversation=conversation,
                            **arguments), model

    def start(self) -> str:
        from app.conversations import create_conversation

        return create_conversation()["conversation_id"]

    def ask(self, conversation_id: str, runner, **overrides):
        """One turn through the real route, with the runner the test supplies."""
        overrides.setdefault("request_id", "req-1")
        overrides.setdefault("message", ORIGINAL_QUESTION)
        response = post_chat(
            ChatRequest(conversation_id=conversation_id, **overrides), runner=runner
        )
        return response, json.loads(response.body)


class RequiredExampleTests(ConversationContextTestCase):
    """The worked example from the step, run through the real stack."""

    def test_a_date_only_reply_continues_the_original_question(self):
        conversation_id = self.start()

        # --- turn 1: the question, with no period in it -----------------------------
        #
        # A clarification runs no agent and no tools: routing decides to ask, and the reply
        # node writes the question. Two requests, not five.
        first_runner = _runner(
            [
                route(
                    "clarification_needed",
                    symbol="NVDA",
                    period="none",
                    start_date=None,
                    end_date=None,
                    clarification_question=CLARIFICATION,
                ),
                say(CLARIFICATION),
            ]
        )
        _, first = self.ask(conversation_id, first_runner, message=ORIGINAL_QUESTION)

        self.assertEqual(first["status"], STATUS_CLARIFICATION_NEEDED)
        self.assertEqual(first["answer"], CLARIFICATION)

        # --- turn 2: nothing but dates ----------------------------------------------
        second_runner = _runner(
            a_full_run(
                route_decision=json.dumps(
                    {
                        "destination": "module1_analysis",
                        "reason": "resolving the earlier question with the supplied dates",
                        "symbol": "NVDA",
                        "period": "explicit",
                        "start_date": WINDOW[0].isoformat(),
                        "end_date": WINDOW[1].isoformat(),
                    }
                ),
                tools=(call("c1", "market_insider_analysis"),),
            )
        )
        _, second = self.ask(
            conversation_id,
            second_runner,
            request_id="req-2",
            message=DATE_REPLY,
        )

        self.assertEqual(second["status"], STATUS_COMPLETED)
        self.assertEqual(second["resolved"]["symbol"], "NVDA")
        self.assertEqual(second["resolved"]["start_date"], WINDOW[0].isoformat())
        self.assertEqual(second["resolved"]["end_date"], WINDOW[1].isoformat())

    def test_a_run_that_needed_a_window_it_was_not_given_becomes_a_clarification(self):
        """The route the live model actually took, and why the status is decided in code.

        Asked to compare price with insiders and given no period, the routing step chose
        `module1_analysis` with `period: none` rather than `clarification_needed`. The agent
        then tried the market tool, was refused for having no window, and the answer written
        for the user asked which period to use -- correct prose with the wrong *status*.
        Nothing was recorded as pending, so the reply could not have resumed anything.

        The refusal is raised by this application and carries a code, so the run can act on it
        rather than hope the routing prompt gets it right.
        """
        conversation_id = self.start()
        runner = _runner(
            a_full_run(
                # Routed to analysis, with no period -- the model's own choice.
                route_decision=route(period="none", start_date=None, end_date=None),
                tools=(call("c1", "market_insider_analysis"),),
                answer="I need to know which period you mean.",
            )
        )

        _, first = self.ask(conversation_id, runner, message=ORIGINAL_QUESTION)

        self.assertEqual(first["status"], STATUS_CLARIFICATION_NEEDED)
        self.assertIn("which period", first["answer"].lower())

        # And it is recorded as pending, so a reply continues the original question.
        summary = _conversation_summary(conversation_id)
        self.assertIsNotNone(summary["pending_clarification"])
        self.assertEqual(
            summary["pending_clarification"]["question"], ORIGINAL_QUESTION
        )

    def test_the_refused_call_is_still_recorded_in_the_tool_log(self):
        """The refusal is not hidden by becoming a clarification."""
        conversation_id = self.start()
        runner = _runner(
            a_full_run(
                route_decision=route(period="none", start_date=None, end_date=None),
                tools=(call("c1", "market_insider_analysis"),),
                answer="Which period?",
            )
        )

        _, body = self.ask(conversation_id, runner, message=ORIGINAL_QUESTION)

        execution = body["tool_executions"][0]
        self.assertEqual(execution["rejection_code"], "no_market_window")
        self.assertEqual(body["usage"]["tool_calls"], 0)

    def test_the_second_run_was_told_what_the_reply_was_answering(self):
        """The mechanism, checked directly: the prompt carries the original question."""
        conversation = ConversationContext(
            pending=PendingClarification(
                original_question=ORIGINAL_QUESTION,
                reference_date=REFERENCE,
                asked_for=CLARIFICATION,
            )
        )

        _, model = self.run_it(
            a_full_run(
                route_decision=json.dumps(
                    {
                        "destination": "module1_analysis",
                        "symbol": "NVDA",
                        "period": "explicit",
                        "start_date": WINDOW[0].isoformat(),
                        "end_date": WINDOW[1].isoformat(),
                    }
                ),
                tools=(call("c1", "market_insider_analysis"),),
            ),
            conversation=conversation,
            question=DATE_REPLY,
        )

        prompt = model.requests[0]["messages"][1]["content"]
        self.assertIn(ORIGINAL_QUESTION, prompt)
        self.assertIn(CLARIFICATION, prompt)
        self.assertIn(DATE_REPLY, prompt)
        self.assertIn("Resolve the ORIGINAL question", prompt)

    def test_a_late_reply_is_resolved_against_the_date_it_was_asked(self):
        """Not against today: the window the user asked about must not move."""
        asked_on = REFERENCE - timedelta(days=40)
        conversation = ConversationContext(
            pending=PendingClarification(
                original_question=ORIGINAL_QUESTION, reference_date=asked_on
            )
        )

        result, _ = self.run_it(
            a_full_run(
                route_decision=json.dumps(
                    {
                        "destination": "module1_analysis",
                        "symbol": "NVDA",
                        "period": "last_30_days",
                    }
                ),
                tools=(call("c1", "market_insider_analysis"),),
            ),
            conversation=conversation,
            question="Some time recently.",
            symbol="NVDA",
            # Deliberately not supplying one: the point is that the *pending* date is used
            # when the caller does not set one of its own.
            reference_date=None,
        )

        # "last 30 days" resolved against the original date, not against today.
        self.assertEqual(result.reference_date, asked_on)


class FollowUpTests(ConversationContextTestCase):
    def test_a_follow_up_runs_against_the_settled_company_and_cutoff(self):
        conversation = ConversationContext(
            settled=SettledContext(
                symbol="NVDA", start_date=WINDOW[0], end_date=WINDOW[1], as_of=WINDOW[1]
            ),
            recent=(
                PriorTurn(
                    user_message=ORIGINAL_QUESTION,
                    answer="NVDA rose slightly.",
                    status=STATUS_COMPLETED,
                ),
            ),
        )

        result, model = self.run_it(
            a_full_run(
                route_decision=json.dumps(
                    {
                        "destination": "module1_analysis",
                        "symbol": "NVDA",
                        "period": "explicit",
                        "start_date": WINDOW[0].isoformat(),
                        "end_date": WINDOW[1].isoformat(),
                        "as_of": WINDOW[1].isoformat(),
                    }
                ),
                tools=(call("c1", "market_insider_analysis"),),
            ),
            conversation=conversation,
            question="What filing risks help explain that?",
        )

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(result.resolved["symbol"], "NVDA")
        self.assertEqual(result.resolved["as_of"], WINDOW[1].isoformat())
        # The tool ran against the settled company and window, not against nothing.
        arguments = result.tool_executions[0]["arguments"]
        self.assertEqual(arguments["symbol"], "NVDA")
        self.assertEqual(arguments["start_date"], WINDOW[0])
        self.assertEqual(arguments["end_date"], WINDOW[1])

    def test_the_prompt_says_prior_prose_is_not_evidence(self):
        conversation = ConversationContext(
            recent=(
                PriorTurn(
                    user_message=ORIGINAL_QUESTION,
                    answer="NVDA rose 400% and insiders bought heavily.",
                    status=STATUS_COMPLETED,
                ),
            )
        )

        _, model = self.run_it(
            a_full_run(route_decision=route()),
            conversation=conversation,
            question="What about the filings?",
        )

        prompt = model.requests[0]["messages"][1]["content"]
        self.assertIn("not evidence", prompt)
        self.assertIn("this run's citations must come from this run's tool results", prompt)

    def test_a_changed_company_starts_from_a_fresh_evidence_map(self):
        """Nothing from the previous company may be cited."""
        conversation = ConversationContext(
            settled=SettledContext(symbol="NVDA", as_of=WINDOW[1]),
        )

        result, _ = self.run_it(
            a_full_run(
                route_decision=json.dumps(
                    {
                        "destination": "module1_analysis",
                        "symbol": "NVDA",
                        "period": "none",
                    }
                ),
                tools=(),
            ),
            conversation=conversation,
            question="Actually, what about NVDA's filings?",
        )

        # The run's evidence starts empty; `E1` belongs to this run and no other.
        self.assertEqual([item["reference"] for item in result.evidence], [])


class BoundingTests(ConversationContextTestCase):
    def test_a_long_conversation_sends_only_the_last_few_turns(self):
        from app.conversations import _load_context
        from app.models import Conversation

        conversation_id = self.start()
        runner = _runner(a_full_run(route_decision=route(), tools=()))
        for index in range(12):
            self.ask(
                conversation_id, runner, request_id=f"req-{index}", message=f"Question {index}."
            )

        stored = self.session.get(Conversation, conversation_id)
        context = _load_context(self.session, stored, before_sequence=None)

        self.assertEqual(len(context.recent), MAX_CONTEXT_TURNS)
        self.assertEqual(context.recent[-1].user_message, "Question 11.")

    def test_a_long_answer_is_clipped_in_the_context(self):
        conversation = ConversationContext(
            recent=(
                PriorTurn(
                    user_message="An earlier question.",
                    answer="x" * 5000,
                    status=STATUS_COMPLETED,
                ),
            )
        )

        described = conversation.describe_for_prompt()

        self.assertIn("shortened", described)
        self.assertLess(len(described), 5000)

    def test_an_empty_conversation_adds_nothing_to_the_prompt(self):
        self.assertEqual(ConversationContext().describe_for_prompt(), "")

    def test_the_bound_is_stated_in_the_prompt_when_recent_turns_are_included(self):
        """A model composing from a partial transcript has to know it is partial."""
        conversation = ConversationContext(
            recent=(PriorTurn(user_message="q", answer="a", status=STATUS_COMPLETED),)
        )

        described = conversation.describe_for_prompt()

        self.assertIn("last few exchanges", described)


class CorrectionTests(ConversationContextTestCase):
    def test_an_explicit_company_argument_overrides_the_settled_one(self):
        conversation = ConversationContext(
            settled=SettledContext(symbol="AAPL", as_of=WINDOW[1]),
        )

        result, _ = self.run_it(
            a_full_run(
                route_decision=json.dumps(
                    {
                        "destination": "module1_analysis",
                        "symbol": "NVDA",
                        "period": "none",
                    }
                ),
                tools=(),
            ),
            conversation=conversation,
            question="What about NVDA?",
            symbol="NVDA",
        )

        self.assertEqual(result.resolved["symbol"], "NVDA")

    def test_the_argument_beats_a_settled_context_that_disagrees(self):
        """The request's own arguments are authoritative; the context is only a default."""
        conversation = ConversationContext(
            settled=SettledContext(symbol="AAPL", as_of=WINDOW[1]),
        )

        result, _ = self.run_it(
            a_full_run(
                route_decision=json.dumps(
                    {
                        "destination": "module1_analysis",
                        "symbol": "NVDA",
                        "period": "none",
                    }
                ),
                tools=(),
            ),
            conversation=conversation,
            question="And NVDA?",
            symbol="NVDA",
        )

        self.assertNotEqual(result.status, STATUS_CLARIFICATION_NEEDED)
        self.assertEqual(result.resolved["symbol"], "NVDA")


class IsolationFromCliTests(ConversationContextTestCase):
    def test_the_cli_path_still_works_with_no_conversation(self):
        """Omitting history leaves the run exactly as it was before conversations existed."""
        result, model = self.run_it(
            a_full_run(
                route_decision=route(),
                tools=(call("c1", "market_insider_analysis"),),
            ),
            question="How did NVDA move?",
            symbol="NVDA",
            start_date=WINDOW[0],
            end_date=WINDOW[1],
        )

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(result.run_id, "run-test")
        prompt = model.requests[0]["messages"][1]["content"]
        self.assertNotIn("continuing conversation", prompt)


# --- helpers ------------------------------------------------------------------------------


def _conversation_summary(conversation_id: str) -> dict:
    from app.conversations import get_conversation

    return get_conversation(conversation_id)


def _runner(script):
    """The real `run_analysis` with a scripted model, in the shape `post_chat` wants.

    A *fresh* model per call, replaying the same script. A conversation asks several questions,
    and one shared model would run out of turns after the first.
    """
    turns = list(script)

    def run(**kwargs):
        return run_analysis(client=ScriptedModel(list(turns)), **kwargs)

    return run


if __name__ == "__main__":
    unittest.main()
