"""Explaining a run: the fact sheet, the guard, and every way it can decline.

No network. The model is a scripted double in the shape `tests.agent_doubles` uses -- the thing
under test is what this application does with a reply, not what a provider says today -- and the
run is a literal row in the shape `app.backtests` returns one.

The rule worth stating once: **an explanation that could not be written is not an error.** Every
test here that expects a refusal also expects the run itself to be untouched, because the
figures are complete without the prose.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from unittest import mock

from app.agent import llm
from app.backtest_explanation import (
    REASON_INVENTED_FIGURE,
    REASON_NOT_CONFIGURED,
    REASON_PROVIDER,
    REASON_UNREADABLE,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    explain,
    facts,
)

# A stored row, in the shape `app.backtests.load` returns: the columns as values, the decimals
# as the strings they were written as. Copied from a real run of the crossover fixture.
ROW = {
    "id": "run-1",
    "strategy_id": "moving_average_crossover",
    "strategy_version": "1",
    "symbol": "NVDA",
    "exchange": "NASDAQ",
    "instrument_name": "NVIDIA Corporation",
    "currency": "USD",
    "start_date": "2024-01-05",
    "end_date": "2024-01-12",
    "initial_capital": "1000.00",
    "parameters": {"fast_period": 2, "slow_period": 3},
    "commission_per_share": "0.005",
    "slippage_percent": "0.05",
    "provider": "twelve_data",
    "adjustment_basis": "adjusted",
    "provider_adjust_mode": "splits",
    "data_first_date": "2024-01-01",
    "data_last_date": "2024-01-12",
    "data_sessions": 10,
    "result": {
        "performance": {
            "final_value": "714.43",
            "total_return_percent": "-28.56",
            "max_drawdown_percent": "37.39",
        },
        "benchmark": {
            "final_value": "833.08",
            "total_return_percent": "-16.69",
            "max_drawdown_percent": "37.41",
        },
        "cash_remaining": "714.43",
        "open_position": None,
        "trades": [
            {"side": "buy", "price": "14.0070", "gross_value": "994.50"},
            {"side": "sell", "price": "9.9950", "gross_value": "709.65"},
        ],
        "coverage": {"sessions": 6, "warmup_sessions": 4},
        "assumptions": ["Signals execute at the next session's open."],
        "limitations": ["Prices are split-adjusted and not dividend-adjusted, so every figure "
                        "here is a price return."],
    },
}


class ScriptedModel:
    """A model that returns a fixed reply, or raises. Records what it was asked."""

    model_name = "deepseek-flash"

    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.requests: list[list[dict]] = []

    def complete(self, *, messages, tools=None, response_format=None, max_tokens):
        self.requests.append(list(messages))
        if self.error is not None:
            raise self.error
        return llm.ModelResponse(
            content=self.content,
            tool_calls=(),
            finish_reason="stop",
            message={"role": "assistant", "content": self.content},
        )


class FactSheetTests(unittest.TestCase):
    def test_the_sheet_carries_the_runs_own_figures(self):
        sheet = facts(ROW)

        for figure in ("-28.56", "714.43", "37.39", "-16.69", "833.08", "1000.00"):
            self.assertIn(figure, sheet)
        # The parameters, the window, the costs and where the bars came from.
        self.assertIn("fast_period=2", sheet)
        self.assertIn("slow_period=3", sheet)
        self.assertIn("2024-01-05", sheet)
        self.assertIn("0.005", sheet)
        self.assertIn("splits", sheet)

    def test_the_sheet_is_the_same_every_time(self):
        self.assertEqual(facts(ROW), facts(ROW))

    def test_the_sheet_states_the_assumptions_and_limitations_the_run_recorded(self):
        sheet = facts(ROW)

        self.assertIn("next session's open", sheet)
        self.assertIn("not dividend-adjusted", sheet)

    def test_a_flat_run_says_it_made_no_trades_and_holds_no_position(self):
        row = {
            **ROW,
            "result": {
                **ROW["result"],
                "trades": [],
                "open_position": None,
                "cash_remaining": "1000.00",
            },
        }

        sheet = facts(row)

        self.assertIn("0 executed trade(s)", sheet)
        self.assertIn("Position at the end: none", sheet)


class ExplanationTests(unittest.TestCase):
    def a_reply(self) -> str:
        """Prose using only figures this run contains, which is what the guard requires."""
        return (
            "The strategy was configured with a 2-session fast average against a 3-session "
            "slow one, and it acted twice: one buy and the sell that closed it. "
            "It ended at 714.43 USD, a return of -28.56%, against buy and hold's 833.08 USD "
            "and -16.69%. The maximum drawdown was 37.39%, a little below the benchmark's "
            "37.41%. These are split-adjusted prices, so the figure is a price return."
        )

    def test_a_usable_reply_comes_back_with_its_provenance(self):
        client = ScriptedModel(self.a_reply())

        outcome = explain(ROW, client=client)

        self.assertEqual(outcome.status, STATUS_OK)
        self.assertEqual(outcome.text, self.a_reply())
        self.assertEqual(outcome.model, "deepseek-flash")
        self.assertEqual(outcome.prompt_version, "1")
        self.assertIsNone(outcome.reason)

    def test_the_model_is_given_the_fact_sheet_and_nothing_else(self):
        client = ScriptedModel(self.a_reply())

        explain(ROW, client=client)

        messages = client.requests[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["content"], facts(ROW))

    def test_a_reply_stating_a_figure_the_run_does_not_contain_is_discarded(self):
        """The prompt asks the model not to invent a figure; this is what makes it a rule.

        31.2% is a plausible drawdown for this window and is not this run's -- so the reply is
        refused rather than stored, because prose that reads like a measurement is worse than
        no prose at all.
        """
        client = ScriptedModel("The account fell 31.2% from its peak at one point.")

        outcome = explain(ROW, client=client)

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.reason, REASON_INVENTED_FIGURE)
        self.assertIsNone(outcome.text)

    def test_a_figure_rounded_from_the_runs_own_is_allowed(self):
        # -28.6% is how this run's -28.56% is read, not a new measurement.
        client = ScriptedModel("The strategy returned -28.6% against buy and hold's -16.7%.")

        self.assertEqual(explain(ROW, client=client).status, STATUS_OK)

    def test_a_bare_whole_number_is_not_treated_as_a_metric(self):
        # Years, counts and ordinals are prose about the run. A check that refused them would
        # refuse good explanations, so only percentages and dollar amounts are guarded.
        client = ScriptedModel(
            "Over the 6 sessions of 2024 the strategy acted twice and made its second trade last."
        )

        self.assertEqual(explain(ROW, client=client).status, STATUS_OK)

    def test_a_reply_that_is_empty_is_unusable_rather_than_an_empty_explanation(self):
        client = ScriptedModel("   ")

        outcome = explain(ROW, client=client)

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.reason, REASON_UNREADABLE)

    def test_a_provider_that_will_not_answer_is_unavailable_and_not_a_crash(self):
        client = ScriptedModel(error=llm.ProviderUnavailableError("timed out"))

        outcome = explain(ROW, client=client)

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.reason, REASON_PROVIDER)

    def test_a_rejected_request_is_reported_as_unreadable(self):
        client = ScriptedModel(error=llm.ProviderResponseError("bad request"))

        outcome = explain(ROW, client=client)

        self.assertEqual(outcome.reason, REASON_UNREADABLE)

    def test_no_api_key_is_a_state_and_not_an_error(self):
        """The application runs without a key, so explaining is simply unavailable.

        The client is built inside the request rather than at import, so the only thing that
        happens with no key is this refusal. `build_client` is replaced rather than left to read
        the environment: a test that reached the real builder would make a real call on a machine
        that happens to have a key, which is spending money to check a missing one.
        """
        with mock.patch.object(
            llm,
            "build_client",
            side_effect=llm.ProviderAuthError("no DeepSeek API key is configured"),
        ):
            outcome = explain(ROW)

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.reason, REASON_NOT_CONFIGURED)
        self.assertIn("no DeepSeek API key", outcome.detail or "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
