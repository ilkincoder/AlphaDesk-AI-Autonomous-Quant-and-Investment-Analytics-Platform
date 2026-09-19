"""One whole run, and the command that starts it.

These are the only tests that drive `run_analysis` -- the function a future `/analysis/chat`
endpoint will call. They cover the things a caller branches on: which status came back, what
is in the result when the provider dies half way, that a missing key is a clear configuration
error rather than a crash, and that the exit code tells an answer from a failure.

The model is scripted and the database is the real test one, so a whole run is exercised
without a network and without spending anything.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import contextlib
import io
import json
import unittest
from datetime import date
from unittest import mock

from app.agent.context import KnownSymbols
from app.agent.run import (
    STATUS_BUDGET_EXHAUSTED,
    STATUS_CLARIFICATION_NEEDED,
    STATUS_COMPLETED,
    STATUS_CONFIGURATION_ERROR,
    STATUS_INVALID_CITATIONS,
    STATUS_PROVIDER_FAILED,
    STATUS_UNSUPPORTED_CAPABILITY,
    run_analysis,
)
from app.run_analysis import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from tests.agent_doubles import (
    AUTH_ERROR,
    RESPONSE_ERROR,
    UNAVAILABLE_ERROR,
    FailOnceModel,
    FailingModel,
    ScriptedModel,
    call,
    calls,
    findings,
    route,
    say,
    tool_sessions,
)
from tests.test_tools_contracts import ToolTestCase

REFERENCE = date(2026, 9, 17)

# A whole successful run, minus the routing decision the individual tests vary.
ANALYSIS_SCRIPT = [
    route(),
    calls("Checking.", call("c1", "market_insider_analysis")),
    say("That is enough."),
    findings(
        "The price rose over the window.",
        refs=("E1",),
        limitations=["insufficient_coverage"],
        next_steps=["Watch the next 10-Q."],
    ),
    say("The price rose slightly [E1]. The sample is thin."),
]


class RunAnalysisTestCase(ToolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.company = self.add_company()
        self.add_price_range(self.company, date(2026, 8, 6), 5, first="100", step="2")
        self.sessions = tool_sessions(self.session)
        self.sessions.__enter__()
        self.addCleanup(lambda: self.sessions.__exit__(None, None, None))

    def run_it(self, script, **overrides):
        arguments = {
            "question": "Compare the price move with reported insider activity.",
            "reference_date": REFERENCE,
            "symbol": "NVDA",
            "start_date": date(2026, 8, 6),
            "end_date": date(2026, 9, 17),
            "known_symbols": KnownSymbols(companies=("AAPL", "NVDA"), holdings=("AAPL", "MSFT", "NVDA")),
            "run_id": "run-test",
        }
        arguments.update(overrides)
        model = arguments.pop("client", None) or ScriptedModel(script)
        return run_analysis(client=model, **arguments), model


class CompletedRunTests(RunAnalysisTestCase):
    def test_a_successful_run_reports_what_it_used_and_what_it_found(self):
        result, _ = self.run_it(ANALYSIS_SCRIPT)

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(result.answer, "The price rose slightly [E1]. The sample is thin.")
        self.assertEqual(result.findings, ["The price rose over the window."])
        self.assertEqual(result.next_steps, ["Watch the next 10-Q."])
        # The agent's own limitation, and the tool's warnings beside it. Both, because the
        # tool's coverage caveat must not depend on the model having remembered to repeat it.
        self.assertIn("insufficient_coverage", result.limitations)
        self.assertTrue(
            any("Retrospective analysis" in item for item in result.limitations)
        )

    def test_a_tool_warning_reaches_the_answer_even_when_the_agent_reports_none(self):
        """The finding step is the model's; the coverage caveat is the application's."""
        script = list(ANALYSIS_SCRIPT)
        script[3] = findings("The price rose over the window.", refs=("E1",))

        result, model = self.run_it(script)

        self.assertEqual(result.limitations and True, True)
        self.assertTrue(
            any("Retrospective analysis" in item for item in result.limitations)
        )
        # And it is in the composition prompt, not only in the result.
        prompt = model.requests[-1]["messages"][-1]["content"]
        self.assertIn("Retrospective analysis", prompt)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.run_id, "run-test")

    def test_the_resolved_request_is_reported(self):
        result, _ = self.run_it(ANALYSIS_SCRIPT)

        self.assertEqual(result.resolved["symbol"], "NVDA")
        self.assertEqual(result.resolved["start_date"], "2026-08-06")
        self.assertEqual(result.resolved["end_date"], "2026-09-17")
        self.assertEqual(result.resolved["as_of"], "2026-09-17")
        self.assertEqual(result.reference_date, REFERENCE)

    def test_the_tool_log_records_what_ran_and_how_it_went(self):
        result, _ = self.run_it(ANALYSIS_SCRIPT)

        self.assertEqual(len(result.tool_executions), 1)
        execution = result.tool_executions[0]
        self.assertEqual(execution["tool"], "market_insider_analysis")
        self.assertEqual(execution["status"], "ok")
        self.assertEqual(execution["evidence_refs"], ["E1"])

    def test_citations_are_resolved_from_the_evidence_map(self):
        result, _ = self.run_it(ANALYSIS_SCRIPT)

        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.citations[0]["reference"], "E1")
        self.assertEqual(result.citations[0]["tool"], "market_insider_analysis")

    def test_the_usage_block_reports_the_limits_and_the_spend(self):
        result, _ = self.run_it(ANALYSIS_SCRIPT)

        usage = result.usage
        self.assertEqual(usage["model_requests"], 5)
        self.assertEqual(usage["tool_calls"], 1)
        self.assertGreater(usage["total_tokens"], 0)
        self.assertEqual(usage["limits"]["max_model_requests"], 12)
        self.assertIsNone(usage["stopped_by"])

    def test_the_model_name_is_recorded(self):
        result, _ = self.run_it(ANALYSIS_SCRIPT)

        self.assertEqual(result.model, "scripted/test-model")

    def test_the_result_survives_json_round_trip(self):
        result, _ = self.run_it(ANALYSIS_SCRIPT)

        document = json.loads(json.dumps(result.as_json()))
        self.assertEqual(document["status"], STATUS_COMPLETED)
        self.assertEqual(document["citations"][0]["reference"], "E1")


class ClarificationAndScopeTests(RunAnalysisTestCase):
    def test_a_request_needing_clarification_is_a_completed_answer_of_that_kind(self):
        result, _ = self.run_it(
            [
                route(
                    "clarification_needed",
                    symbol=None,
                    period="none",
                    start_date=None,
                    end_date=None,
                    clarification_question="Which company, and over what period?",
                ),
                say("Which company, and over what period?"),
            ]
        )

        self.assertEqual(result.status, STATUS_CLARIFICATION_NEEDED)
        self.assertIn("Which company", result.answer)
        self.assertEqual(result.tool_executions, [])

    def test_an_out_of_scope_request_is_explained(self):
        result, _ = self.run_it(
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

        self.assertEqual(result.status, STATUS_UNSUPPORTED_CAPABILITY)
        self.assertIn("cannot place orders", result.answer)

    def test_a_mixed_request_carries_what_was_not_done(self):
        script = list(ANALYSIS_SCRIPT)
        script[0] = route(scope_note="the trade instruction was not performed")

        result, model = self.run_it(script)

        self.assertEqual(result.status, STATUS_COMPLETED)
        prompt = model.requests[-1]["messages"][-1]["content"]
        self.assertIn("the trade instruction was not performed", prompt)


class CitationFailureTests(RunAnalysisTestCase):
    def test_an_answer_citing_evidence_that_does_not_exist_is_withheld(self):
        script = list(ANALYSIS_SCRIPT)
        script[4] = say("The price rose [E9].")
        # The correction turn, in which the model invents a different reference instead.
        script.append(say("It rose, as [E9] and [E10] show."))

        result, model = self.run_it(script)

        self.assertEqual(result.status, STATUS_INVALID_CITATIONS)
        self.assertIsNone(result.answer)
        self.assertTrue(any("E9" in warning for warning in result.warnings))
        # The evidence and the tool log survive, so the run is diagnosable.
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(len(result.tool_executions), 1)


class ProviderFailureTests(RunAnalysisTestCase):
    def test_an_unavailable_provider_is_reported_as_a_failure_with_no_answer(self):
        model = FailingModel(UNAVAILABLE_ERROR)
        result, _ = self.run_it([], client=model, known_symbols=KnownSymbols(companies=("NVDA",)))

        self.assertEqual(result.status, STATUS_PROVIDER_FAILED)
        self.assertIsNone(result.answer)
        self.assertTrue(any("could not be used" in w for w in result.warnings))

    def test_an_authentication_failure_is_a_configuration_error(self):
        """A wrong key is not a provider outage, and repeating it changes nothing."""
        model = FailingModel(AUTH_ERROR)
        result, _ = self.run_it([], client=model, known_symbols=KnownSymbols(companies=("NVDA",)))

        self.assertEqual(result.status, STATUS_CONFIGURATION_ERROR)
        self.assertEqual(model.calls, 1)

    def test_a_rejected_request_is_a_provider_failure_rather_than_a_configuration_one(self):
        model = FailingModel(RESPONSE_ERROR)
        result, _ = self.run_it([], client=model, known_symbols=KnownSymbols(companies=("NVDA",)))

        self.assertEqual(result.status, STATUS_PROVIDER_FAILED)
        self.assertEqual(model.calls, 1)

    def test_a_transient_failure_is_the_client_s_to_retry_not_the_run_s(self):
        """The run classifies; the client retries. See `test_agent_llm.py` for the retrying.

        A double that raises `ProviderUnavailableError` has already exhausted its own
        retries, so what the run sees is a provider that stayed down.
        """
        model = FailOnceModel(ANALYSIS_SCRIPT, failures=1)
        result, _ = self.run_it([], client=model, known_symbols=KnownSymbols(companies=("NVDA",)))

        self.assertEqual(result.status, STATUS_PROVIDER_FAILED)
        self.assertEqual(model.failed, 1)


class BudgetTests(RunAnalysisTestCase):
    def test_running_out_of_model_requests_stops_the_run_and_says_which_limit(self):
        from app.agent.budget import RunBudget

        result, _ = self.run_it(
            [route(), say("Checking.")], budget=RunBudget(max_model_requests=2)
        )

        self.assertEqual(result.status, STATUS_BUDGET_EXHAUSTED)
        self.assertEqual(result.usage["stopped_by"], "model_requests")
        self.assertTrue(any("stopped early" in w.lower() for w in result.warnings))


class NotConfiguredTests(ToolTestCase):
    def test_a_missing_key_is_a_clear_configuration_error_not_a_crash(self):
        with mock.patch("app.agent.run._settings") as settings:
            settings.return_value = mock.Mock(
                deepseek_api_key=None,
                deepseek_model="deepseek-flash",
                deepseek_base_url="https://api.deepseek.com",
                deepseek_timeout_seconds=60.0,
            )
            result = run_analysis(question="Anything?", reference_date=REFERENCE)

        self.assertEqual(result.status, STATUS_CONFIGURATION_ERROR)
        self.assertTrue(any("DEEPSEEK_API_KEY" in w for w in result.warnings))

    def test_the_api_still_starts_with_no_key(self):
        """Nothing in the agent package may be imported by the API, or reach for a key."""
        import app.main  # noqa: F401 - the import is the test

        from pathlib import Path

        source = Path(app.main.__file__).read_text()
        self.assertNotIn("run_analysis", source)
        self.assertNotIn("agent", source.replace("management", ""))


class ReadsOnlyTests(RunAnalysisTestCase):
    def test_a_run_does_not_change_a_row(self):
        before = self.counts()

        self.run_it(ANALYSIS_SCRIPT)

        self.assert_wrote_nothing(before)


class CommandTests(ToolTestCase):
    """The CLI: exit codes, output modes, and validation before anything runs."""

    def invoke(self, argv):
        """Run the CLI with the run function replaced, so nothing reaches the provider."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("app.run_analysis.run_analysis") as runner:
            runner.return_value = self.fake_result()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), runner

    def fake_result(self):
        from app.agent.run import RunResult

        return RunResult(
            run_id="run-test",
            status=STATUS_COMPLETED,
            question="q",
            reference_date=REFERENCE,
            model="deepseek-flash",
            resolved={"symbol": "NVDA", "start_date": "2026-08-06",
                      "end_date": "2026-09-17", "as_of": "2026-09-17",
                      "period": "explicit"},
            destination="module1_analysis",
            route_reason="because",
            answer="The price rose [E1].",
            citations=[{"reference": "E1", "tool": "market_insider_analysis",
                        "label": "market result", "filing": None}],
            limitations=["insufficient_coverage"],
            tool_executions=[{"tool": "market_insider_analysis", "status": "ok",
                              "arguments": {}, "reason": None, "evidence_refs": ["E1"],
                              "reused_previous_result": False, "rejected": None}],
            usage={"model_requests": 5, "tool_calls": 1, "prompt_tokens": 500,
                   "completion_tokens": 100, "total_tokens": 600, "elapsed_seconds": 1.5,
                   "transient_retries": 0, "stopped_by": None, "stop_detail": None,
                   "limits": {"max_model_requests": 12, "max_tool_calls": 10}},
        )

    def test_the_readable_output_shows_the_answer_and_what_it_rests_on(self):
        code, out, _, _ = self.invoke(["--question", "q"])

        self.assertEqual(code, EXIT_OK)
        self.assertIn("The price rose [E1].", out)
        self.assertIn("insufficient_coverage", out)
        self.assertIn("[E1]", out)
        self.assertIn("model requests 5/12", out)

    def test_json_mode_prints_the_structured_result(self):
        code, out, _, _ = self.invoke(["--question", "q", "--json"])

        self.assertEqual(code, EXIT_OK)
        document = json.loads(out)
        self.assertEqual(document["status"], STATUS_COMPLETED)

    def test_a_half_specified_window_is_refused_before_anything_runs(self):
        code, _, err, runner = self.invoke(
            ["--question", "q", "--start-date", "2026-08-06"]
        )

        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("must be given together", err)
        runner.assert_not_called()

    def test_a_missing_question_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self.invoke([])

        self.assertEqual(caught.exception.code, 2)

    def test_an_unparseable_date_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self.invoke(["--question", "q", "--start-date", "last Tuesday"])

        self.assertEqual(caught.exception.code, 2)

    def test_the_arguments_reach_the_run_function(self):
        _, _, _, runner = self.invoke(
            [
                "--question", "q",
                "--symbol", "NVDA",
                "--start-date", "2026-08-06",
                "--end-date", "2026-09-17",
                "--as-of", "2026-09-17",
                "--reference-date", "2026-09-17",
            ]
        )

        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["symbol"], "NVDA")
        self.assertEqual(kwargs["start_date"], date(2026, 8, 6))
        self.assertEqual(kwargs["end_date"], date(2026, 9, 17))
        self.assertEqual(kwargs["as_of"], date(2026, 9, 17))
        self.assertEqual(kwargs["reference_date"], date(2026, 9, 17))

    def test_a_failed_run_exits_nonzero(self):
        from app.agent.run import RunResult

        for status in (STATUS_PROVIDER_FAILED, STATUS_CONFIGURATION_ERROR,
                       STATUS_BUDGET_EXHAUSTED, STATUS_INVALID_CITATIONS):
            with self.subTest(status=status):
                stdout = io.StringIO()
                with mock.patch("app.run_analysis.run_analysis") as runner:
                    runner.return_value = RunResult(
                        run_id="r", status=status, question="q",
                        reference_date=REFERENCE, warnings=["something went wrong"],
                    )
                    with contextlib.redirect_stdout(stdout):
                        code = main(["--question", "q"])
                self.assertEqual(code, EXIT_FAILED)

    def test_an_answer_like_status_exits_zero(self):
        from app.agent.run import RunResult

        for status in (STATUS_CLARIFICATION_NEEDED, STATUS_UNSUPPORTED_CAPABILITY):
            with self.subTest(status=status):
                stdout = io.StringIO()
                with mock.patch("app.run_analysis.run_analysis") as runner:
                    runner.return_value = RunResult(
                        run_id="r", status=status, question="q",
                        reference_date=REFERENCE, answer="Which company?",
                    )
                    with contextlib.redirect_stdout(stdout):
                        code = main(["--question", "q"])
                self.assertEqual(code, EXIT_OK)
                self.assertIn("Which company?", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
