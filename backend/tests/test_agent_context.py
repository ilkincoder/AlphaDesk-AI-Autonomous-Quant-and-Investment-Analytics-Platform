"""Settling a run: the company, the window, and what was knowable.

Everything here is pure date and string logic, so it needs no database and no model. It is
tested on its own because it is the layer that decides *what question is being answered*, and
a mistake here is invisible in the output -- a price comparison over the wrong window looks
exactly like one over the right window.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date

from app.agent.context import (
    KnownSymbols,
    PERIOD_EXPLICIT,
    PERIOD_LAST_7_DAYS,
    PERIOD_LAST_30_DAYS,
    PERIOD_LAST_90_DAYS,
    PERIOD_LAST_QUARTER,
    PERIOD_NONE,
    PERIOD_TOKENS,
    PERIOD_YEAR_TO_DATE,
    ExplicitArguments,
    resolve_period,
    resolve_request,
)

REFERENCE = date(2026, 9, 17)


class PeriodConventionTests(unittest.TestCase):
    """The documented conventions, asserted as documentation."""

    def test_a_trailing_window_ends_on_the_reference_date_and_includes_it(self):
        for token, days in (
            (PERIOD_LAST_7_DAYS, 7),
            (PERIOD_LAST_30_DAYS, 30),
            (PERIOD_LAST_90_DAYS, 90),
        ):
            with self.subTest(token=token):
                start, end = resolve_period(token, REFERENCE)
                self.assertEqual(end, REFERENCE)
                # Inclusive of both ends, so "7 days" is 7 dates and not 8.
                self.assertEqual((end - start).days, days - 1)

    def test_year_to_date_starts_on_the_first_of_january(self):
        self.assertEqual(
            resolve_period(PERIOD_YEAR_TO_DATE, REFERENCE), (date(2026, 1, 1), REFERENCE)
        )

    def test_the_last_quarter_is_the_previous_complete_calendar_quarter(self):
        # 17 September is in Q3, so the previous complete quarter is April to June.
        self.assertEqual(
            resolve_period(PERIOD_LAST_QUARTER, REFERENCE),
            (date(2026, 4, 1), date(2026, 6, 30)),
        )

    def test_the_last_quarter_crosses_a_year_boundary(self):
        self.assertEqual(
            resolve_period(PERIOD_LAST_QUARTER, date(2026, 2, 10)),
            (date(2025, 10, 1), date(2025, 12, 31)),
        )

    def test_no_period_means_no_window(self):
        self.assertIsNone(resolve_period(PERIOD_NONE, REFERENCE))

    def test_a_token_outside_the_closed_set_is_a_programming_error(self):
        """The routing model is validated first; this is the backstop."""
        with self.assertRaises(ValueError):
            resolve_period("last_fortnight", REFERENCE)

    def test_every_relative_token_resolves_to_an_ordered_window(self):
        """`explicit` and `none` are excluded: neither is a relative period.

        `none` returns no window and `explicit` means the dates were stated outright, so
        neither has arithmetic for this function to do.
        """
        for token in PERIOD_TOKENS:
            if token in (PERIOD_NONE, PERIOD_EXPLICIT):
                continue
            with self.subTest(token=token):
                start, end = resolve_period(token, REFERENCE)
                self.assertLessEqual(start, end)


class ResolutionTests(unittest.TestCase):
    def resolve(self, **overrides):
        arguments = {
            "reference_date": REFERENCE,
            "explicit": ExplicitArguments(),
            "symbol": "NVDA",
            "period": PERIOD_EXPLICIT,
            "start_date": date(2026, 8, 6),
            "end_date": date(2026, 9, 17),
        }
        arguments.update(overrides)
        return resolve_request(**arguments)

    def test_a_settled_request_carries_the_company_and_window(self):
        outcome = self.resolve()

        self.assertTrue(outcome.settled)
        self.assertEqual(outcome.resolved.symbol, "NVDA")
        self.assertEqual(outcome.resolved.start_date, date(2026, 8, 6))
        self.assertEqual(outcome.resolved.end_date, date(2026, 9, 17))

    def test_the_information_cutoff_defaults_to_the_end_of_the_window(self):
        self.assertEqual(self.resolve().resolved.as_of, date(2026, 9, 17))

    def test_a_question_needing_no_window_still_gets_a_cutoff(self):
        """A filing question has no market window, but it still has to say what was knowable."""
        outcome = self.resolve(period=PERIOD_NONE, start_date=None, end_date=None)

        self.assertTrue(outcome.settled)
        self.assertFalse(outcome.resolved.has_window)
        self.assertEqual(outcome.resolved.as_of, REFERENCE)

    def test_no_company_is_a_clarification_not_a_guess(self):
        outcome = self.resolve(symbol=None)

        self.assertFalse(outcome.settled)
        self.assertIn("which company", outcome.conflict)


class ExplicitArgumentTests(unittest.TestCase):
    """Explicit arguments win, and a question that disagrees is a question for the user."""

    def test_the_explicit_symbol_is_used_when_the_question_names_none(self):
        outcome = resolve_request(
            reference_date=REFERENCE,
            explicit=ExplicitArguments(symbol="aapl"),
            symbol=None,
            period=PERIOD_NONE,
        )

        self.assertEqual(outcome.resolved.symbol, "AAPL")

    def test_a_question_naming_a_different_company_asks_which_one(self):
        outcome = resolve_request(
            reference_date=REFERENCE,
            explicit=ExplicitArguments(symbol="AAPL"),
            symbol="NVDA",
            period=PERIOD_NONE,
        )

        self.assertFalse(outcome.settled)
        self.assertIn("NVDA", outcome.conflict)
        self.assertIn("AAPL", outcome.conflict)
        self.assertIn("One company per run", outcome.conflict)

    def test_an_explicit_window_is_used_when_the_question_names_no_period(self):
        outcome = resolve_request(
            reference_date=REFERENCE,
            explicit=ExplicitArguments(
                start_date=date(2026, 8, 6), end_date=date(2026, 9, 17)
            ),
            symbol="NVDA",
            period=PERIOD_NONE,
        )

        self.assertTrue(outcome.settled)
        self.assertEqual(outcome.resolved.start_date, date(2026, 8, 6))

    def test_a_question_implying_a_different_window_asks_which_one(self):
        outcome = resolve_request(
            reference_date=REFERENCE,
            explicit=ExplicitArguments(
                start_date=date(2026, 8, 6), end_date=date(2026, 9, 17)
            ),
            symbol="NVDA",
            period=PERIOD_LAST_30_DAYS,
        )

        self.assertFalse(outcome.settled)
        self.assertIn("2026-08-19", outcome.conflict)

    def test_an_explicit_window_matching_the_question_is_settled(self):
        outcome = resolve_request(
            reference_date=REFERENCE,
            explicit=ExplicitArguments(
                start_date=date(2026, 8, 6), end_date=date(2026, 9, 17)
            ),
            symbol="NVDA",
            period=PERIOD_EXPLICIT,
            start_date=date(2026, 8, 6),
            end_date=date(2026, 9, 17),
        )

        self.assertTrue(outcome.settled)

    def test_a_question_asking_for_a_later_cutoff_than_the_run_allows_is_refused(self):
        """The explicit cutoff is a ceiling on what counts as knowable, not a default."""
        outcome = resolve_request(
            reference_date=REFERENCE,
            explicit=ExplicitArguments(as_of=date(2026, 8, 1)),
            symbol="NVDA",
            period=PERIOD_NONE,
            as_of=date(2026, 9, 17),
        )

        self.assertFalse(outcome.settled)
        self.assertIn("after the cutoff this run was started with", outcome.conflict)

    def test_a_half_specified_window_is_refused(self):
        for arguments in (
            ExplicitArguments(start_date=date(2026, 8, 6)),
            ExplicitArguments(end_date=date(2026, 9, 17)),
        ):
            with self.subTest(arguments=arguments):
                outcome = resolve_request(
                    reference_date=REFERENCE,
                    explicit=arguments,
                    symbol="NVDA",
                    period=PERIOD_NONE,
                )
                self.assertFalse(outcome.settled)

    def test_a_backwards_window_is_refused(self):
        outcome = resolve_request(
            reference_date=REFERENCE,
            explicit=ExplicitArguments(),
            symbol="NVDA",
            period=PERIOD_EXPLICIT,
            start_date=date(2026, 9, 17),
            end_date=date(2026, 8, 6),
        )

        self.assertFalse(outcome.settled)
        self.assertIn("starts after it ends", outcome.conflict)

    def test_an_explicit_period_without_dates_is_refused(self):
        outcome = resolve_request(
            reference_date=REFERENCE,
            explicit=ExplicitArguments(),
            symbol="NVDA",
            period=PERIOD_EXPLICIT,
        )

        self.assertFalse(outcome.settled)
        self.assertIn("without both a start and an end", outcome.conflict)


class PromptDescriptionTests(unittest.TestCase):
    def test_the_resolved_request_is_described_for_the_prompts(self):
        context = resolve_request(
            reference_date=REFERENCE,
            explicit=ExplicitArguments(),
            symbol="NVDA",
            period=PERIOD_EXPLICIT,
            start_date=date(2026, 8, 6),
            end_date=date(2026, 9, 17),
        ).resolved

        from app.agent.context import RunContext

        described = RunContext(
            question="q",
            reference_date=REFERENCE,
            resolved=context,
            explicit=ExplicitArguments(),
            known=KnownSymbols(companies=("AAPL", "NVDA"), holdings=("AAPL", "MSFT", "NVDA")),
        ).describe_for_prompt()

        self.assertIn("Company: NVDA", described)
        self.assertIn("2026-08-06 to 2026-09-17", described)
        self.assertIn("Companies with ingested data", described)
        self.assertIn("AAPL, NVDA", described)
        self.assertIn("Symbols held in the demo portfolio", described)

    def test_a_question_with_no_window_says_so_rather_than_showing_blank_dates(self):
        from app.agent.context import RequestInputs

        inputs = RequestInputs(
            question="q", reference_date=REFERENCE, explicit=ExplicitArguments()
        )
        self.assertIn("Reference date", inputs.describe_for_prompt())


if __name__ == "__main__":
    unittest.main()
