"""The backtest engine, on fixtures small enough to check by hand.

Every fixture here is a handful of sessions whose moving averages, crossovers, fills, cash
balances and returns are worked out in the test itself or written down as a literal. That is
the point of the file: a performance figure is only worth anything if someone can say where
it came from, and an engine tested against numbers it produced is tested against nothing.

The two rules most easily got wrong are checked directly rather than inferred from a result:
a signal executes on the *next* session, and the test starts flat even when the fast average
is already above the slow one.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import date, timedelta
from decimal import Decimal

from app.backtest import (
    BacktestConfig,
    CostModel,
    CostModelError,
    InvalidConfigurationError,
    MOVING_AVERAGE_CROSSOVER,
    RSI_REVERSION,
    UnusableDataError,
    run_backtest,
    validate_configuration,
    warmup_lookback_days,
)
from app.twelvedata import DailyBar


def sessions(start: date, count: int) -> list[date]:
    """`count` weekday dates from `start`, weekends skipped.

    Weekends only. Exchange holidays are not modelled, and they do not need to be: the engine
    reads whatever dates it is given and never consults a calendar. What the fixture has to
    provide is an ordered series, and consecutive weekdays are the shape a real one has.
    """
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def bars_from(
    dates: list[date],
    closes: list[str],
    *,
    opens: list[str] | None = None,
) -> list[DailyBar]:
    """Bars from closing prices, with opens defaulting to the same price.

    `high` and `low` are set to the extremes of the two prices so every bar is coherent. The
    engine never reads them, but a fixture holding an impossible bar would be hiding a bug in
    whatever produced it rather than in the engine.
    """
    open_prices = opens if opens is not None else closes
    bars = []
    for day, open_price, close_price in zip(dates, open_prices, closes, strict=True):
        high = max(open_price, close_price, key=Decimal)
        low = min(open_price, close_price, key=Decimal)
        bars.append(
            DailyBar(
                trading_date=day,
                open=Decimal(open_price),
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close_price),
                volume=1_000_000,
            )
        )
    return bars


# The crossover fixture, and it is worked out here so every assertion below can be checked
# against it. fast = SMA(2), slow = SMA(3).
#
#   index   0    1    2    3    4    5    6    7    8    9
#   close  10   10   10   10   12   14   16   14   12   10
#   fast    -   10   10   10   11   13   15   15   13   11
#   slow    -    -   10   10 10.67   12   14 14.67   14   12
#   diff    -    -    0    0  .33    1    1  .33   -1   -1
#
# The only crossings are at index 4 (0 -> +0.33, a buy) and at index 8 (+0.33 -> -1, a sell).
# The buy executes at index 5's open and the sell at index 9's open.
CROSSOVER_CLOSES = ["10", "10", "10", "10", "12", "14", "16", "14", "12", "10"]
CROSSOVER_OPENS = ["10", "10", "10", "10", "12", "14", "16", "14", "12", "10"]

DATES = sessions(date(2024, 1, 1), 10)  # 2024-01-01 .. 2024-01-12, weekdays only


def crossover_config(**overrides) -> BacktestConfig:
    """The crossover fixture's window: indices 4 to 9, with exactly the warm-up it needs.

    Four sessions of warm-up for a three-session slow average, which is `slow + 1`: the extra
    one exists so the last warm-up session can be compared with the one before it.

    A period may be named directly -- `crossover_config(fast_period=3)` -- because that is how
    the refusal tests read, or the whole set replaced through `parameters=`.
    """
    parameters = {"fast_period": 2, "slow_period": 3}
    parameters.update(
        {name: overrides.pop(name) for name in list(overrides) if name in parameters}
    )
    settings = {
        "strategy": MOVING_AVERAGE_CROSSOVER,
        "symbol": "TEST",
        "start_date": DATES[4],
        "end_date": DATES[9],
        "initial_capital": Decimal("1000.00"),
        "parameters": parameters,
        # Costs off, so the arithmetic under test is the strategy's rather than the cost
        # model's. The cost model has fixtures of its own below.
        "costs": CostModel(
            commission_per_share=Decimal("0"), slippage_percent=Decimal("0")
        ),
    }
    settings.update(overrides)
    return BacktestConfig(**settings)


def run_crossover(**overrides):
    return run_backtest(bars_from(DATES, CROSSOVER_CLOSES, opens=CROSSOVER_OPENS),
                        crossover_config(**overrides))


class WarmupTests(unittest.TestCase):
    def test_warmup_is_one_more_session_than_the_slow_period(self):
        # The extra session is not slack. Evaluating a crossing at the last warm-up session --
        # whose execution is the test's first session -- needs the slow average at the session
        # before it as well.
        self.assertEqual(
            MOVING_AVERAGE_CROSSOVER.warmup_sessions({"fast_period": 20, "slow_period": 50}), 51
        )

    def test_rsi_warmup_is_one_more_session_than_its_period(self):
        # `period` changes give the first average gain and loss, and the extra session is the
        # one a crossing needs to be a crossing rather than a level.
        self.assertEqual(
            RSI_REVERSION.warmup_sessions(
                {"period": 14, "entry_threshold": 30, "exit_threshold": 70}
            ),
            15,
        )

    def test_the_lookback_covers_weekends_and_holidays(self):
        # 51 sessions need 72 calendar days at five-in-seven, plus an allowance for the
        # exchange holidays that estimate cannot see. The exact number is not the point; that
        # it comfortably exceeds the minimum is.
        self.assertGreater(warmup_lookback_days(50), 51)
        self.assertGreaterEqual(warmup_lookback_days(5), 1)


class TimingTests(unittest.TestCase):
    def test_a_signal_executes_on_the_next_session_not_the_one_that_made_it(self):
        outcome = run_crossover()

        self.assertEqual(len(outcome.trades), 2)
        buy, sell = outcome.trades
        # The crossing was at index 4 and the buy at index 5.
        self.assertEqual(buy.signalled_on, DATES[4])
        self.assertEqual(buy.executed_on, DATES[5])
        # The crossing was at index 8 and the sell at index 9.
        self.assertEqual(sell.signalled_on, DATES[8])
        self.assertEqual(sell.executed_on, DATES[9])

    def test_a_trade_is_priced_at_the_execution_session_open_not_its_close(self):
        outcome = run_crossover()

        buy, sell = outcome.trades
        # Index 5 opened at 14 and closed at 14; index 9 opened at 10 and closed at 10. The
        # opens and closes are equal in this fixture on purpose, so the assertion that matters
        # is against the *open* of the execution session rather than the close of the signal
        # session -- which is a different number in the fixture below.
        self.assertEqual(buy.price, Decimal("14"))
        self.assertEqual(sell.price, Decimal("10"))

    def test_the_execution_price_follows_the_open_even_when_it_differs_from_the_close(self):
        # Index 4 closes at 12 and raises the buy; index 5 opens at 20 having closed at 14.
        # Filling at 14 (the signal session's close) would be deciding with a price the
        # strategy could not have traded at.
        outcome = run_backtest(
            bars_from(
                DATES,
                CROSSOVER_CLOSES,
                opens=["10", "10", "10", "10", "12", "20", "16", "14", "12", "10"],
            ),
            crossover_config(),
        )

        self.assertEqual(outcome.trades[0].executed_on, DATES[5])
        self.assertEqual(outcome.trades[0].price, Decimal("20"))

    def test_a_signal_on_the_final_session_is_discarded(self):
        # The sell crosses at index 8, and the window ends there: there is no session left
        # inside the test to execute it in, so the position stays open rather than being
        # closed on a bar that produced the signal.
        outcome = run_crossover(end_date=DATES[8])

        self.assertEqual(len(outcome.trades), 1)
        self.assertEqual(outcome.trades[0].side, "buy")
        self.assertIsNotNone(outcome.open_position)

    def test_a_position_open_at_the_end_is_marked_to_the_last_close_not_sold(self):
        outcome = run_crossover(end_date=DATES[8])

        position = outcome.open_position
        assert position is not None
        self.assertEqual(position.quantity, 71)
        self.assertEqual(position.entry_date, DATES[5])
        self.assertEqual(position.entry_price, Decimal("14"))
        self.assertEqual(position.mark_date, DATES[8])
        self.assertEqual(position.mark_price, Decimal("12"))
        self.assertEqual(position.market_value, Decimal("852.00"))
        # 71 shares bought at 14 out of 1000 leaves 6 in cash, and 6 + 71 * 12 is the value.
        self.assertEqual(outcome.cash_remaining, Decimal("6.00"))
        self.assertEqual(outcome.performance.final_value, Decimal("858.00"))


class StartFlatTests(unittest.TestCase):
    """The rule that is easiest to get wrong: a test does not inherit a position."""

    # A series that rises all the way, so the fast average is above the slow one throughout
    # after a crossing at index 4. Making the window start at index 6 puts that crossing two
    # sessions before the test opens.
    RISING = ["10", "10", "10", "10", "20", "30", "40", "50", "60", "70"]

    def test_a_fast_average_already_above_the_slow_one_opens_no_position(self):
        outcome = run_backtest(
            bars_from(DATES, self.RISING),
            crossover_config(start_date=DATES[6], end_date=DATES[9]),
        )

        self.assertEqual(outcome.trades, ())
        self.assertIsNone(outcome.open_position)
        self.assertEqual(outcome.performance.final_value, Decimal("1000.00"))
        self.assertEqual(outcome.performance.total_return_percent, Decimal("0.00"))

    def test_a_crossing_on_the_last_warmup_session_does_open_one(self):
        # The same series, with the window starting one session earlier. The crossing at index
        # 4 is then the last warm-up session, and its execution -- index 5's open, 30 -- falls
        # inside the test. This is a crossing, not an average that was already above, and the
        # two are different facts.
        outcome = run_backtest(
            bars_from(DATES, self.RISING),
            crossover_config(start_date=DATES[5], end_date=DATES[9]),
        )

        self.assertEqual(len(outcome.trades), 1)
        self.assertEqual(outcome.trades[0].side, "buy")
        self.assertEqual(outcome.trades[0].signalled_on, DATES[4])
        self.assertEqual(outcome.trades[0].executed_on, DATES[5])


class IsolatedHistoryTests(unittest.TestCase):
    def test_later_sessions_do_not_change_earlier_decisions(self):
        window = bars_from(DATES, CROSSOVER_CLOSES, opens=CROSSOVER_OPENS)
        extended = window + bars_from(
            sessions(date(2024, 1, 15), 4), ["10", "9", "8", "7"]
        )

        short = run_backtest(window, crossover_config())
        long = run_backtest(extended, crossover_config(end_date=DATES[9]))

        # The same window, and every figure in it is the same. A decision taken on 2024-01-05
        # cannot be a function of a price set on 2024-01-17.
        self.assertEqual(short.trades, long.trades)
        self.assertEqual(short.equity, long.equity)
        self.assertEqual(short.performance, long.performance)

    def test_extra_warmup_does_not_change_the_reported_interval(self):
        # Ten sessions of history before the same window, instead of four. The averages inside
        # the window only ever look back `period` sessions, so nothing about the result may
        # move -- and the extra history changes nothing that is reported.
        older = bars_from(sessions(date(2023, 12, 4), 10), ["5"] * 10)
        window = bars_from(DATES, CROSSOVER_CLOSES, opens=CROSSOVER_OPENS)

        plain = run_backtest(window, crossover_config())
        padded = run_backtest(older + window, crossover_config())

        self.assertEqual(plain.equity, padded.equity)
        self.assertEqual(plain.trades, padded.trades)
        self.assertEqual(plain.performance, padded.performance)
        # The warm-up is longer, and none of it is reported on.
        self.assertEqual(plain.warmup_sessions, 4)
        self.assertEqual(padded.warmup_sessions, 14)
        self.assertEqual(padded.first_session, DATES[4])

    def test_the_curve_begins_at_the_initial_capital_and_not_before_the_start_date(self):
        outcome = run_crossover()

        first = outcome.equity[0]
        self.assertEqual(first.on, DATES[4])
        self.assertEqual(first.equity, Decimal("1000.00"))
        self.assertEqual(outcome.first_session, DATES[4])
        # One point for the opening balance plus one per session in the window.
        self.assertEqual(len(outcome.equity), outcome.sessions + 1)
        self.assertEqual(outcome.sessions, 6)


class SizingAndCostTests(unittest.TestCase):
    def test_whole_shares_are_bought_and_the_remainder_is_kept(self):
        # No costs, so the largest whole number of shares at 14 that 1000 USD buys is 71
        # (72 would be 1008) and 6 USD is left over.
        outcome = run_crossover()

        buy = outcome.trades[0]
        self.assertEqual(buy.quantity, 71)
        self.assertEqual(buy.gross_value, Decimal("994.00"))
        self.assertEqual(buy.cash_after, Decimal("6.00"))
        self.assertEqual(buy.commission, Decimal("0"))

    def test_commission_is_charged_on_both_sides_and_lands_in_the_cash_balance(self):
        # 100 USD at 14 with 0.01 USD per share: the commission is part of what has to fit, so
        # the affordable quantity is floor(100 / 14.01) = 7 rather than floor(100 / 14) = 7.
        # They agree here; the cash balance is what proves the fee was charged.
        outcome = run_crossover(
            initial_capital=Decimal("100.00"),
            costs=CostModel(
                commission_per_share=Decimal("0.01"), slippage_percent=Decimal("0")
            ),
        )

        buy, sell = outcome.trades
        self.assertEqual(buy.quantity, 7)
        self.assertEqual(buy.gross_value, Decimal("98.00"))
        self.assertEqual(buy.commission, Decimal("0.07"))
        self.assertEqual(buy.cash_after, Decimal("1.93"))

        self.assertEqual(sell.gross_value, Decimal("70.00"))
        self.assertEqual(sell.commission, Decimal("0.07"))
        self.assertEqual(sell.cash_after, Decimal("71.86"))
        self.assertEqual(outcome.cash_remaining, Decimal("71.86"))

    def test_the_commission_is_what_limits_the_quantity_not_the_price_alone(self):
        # One share costs 14 and 0.99 in commission, so 14.50 USD buys none of it even though
        # 14.50 is more than the share price.
        outcome = run_crossover(
            initial_capital=Decimal("14.50"),
            costs=CostModel(
                commission_per_share=Decimal("0.99"), slippage_percent=Decimal("0")
            ),
        )

        self.assertEqual(outcome.trades, ())
        self.assertEqual(outcome.performance.final_value, Decimal("14.50"))

    def test_slippage_moves_the_buy_up_and_the_sell_down(self):
        outcome = run_crossover(
            costs=CostModel(
                commission_per_share=Decimal("0"), slippage_percent=Decimal("1")
            ),
        )

        buy, sell = outcome.trades
        # 14 * 1.01 = 14.14 and 10 * 0.99 = 9.90.
        self.assertEqual(buy.price, Decimal("14.1400"))
        self.assertEqual(sell.price, Decimal("9.9000"))
        # floor(1000 / 14.14) = 70, and the remainder stays in cash.
        self.assertEqual(buy.quantity, 70)
        self.assertEqual(buy.cash_after, Decimal("10.20"))

    def test_cash_never_goes_negative_across_a_range_of_capitals(self):
        # The rounded total can sit a fraction above the exact quotient, so the quantity is
        # stepped down until the arithmetic fits. These capitals are chosen to sit close to a
        # share boundary, which is where that gap shows up.
        for capital in ("14.01", "28.02", "42.03", "140.10", "999.99", "1000.00"):
            with self.subTest(capital=capital):
                outcome = run_crossover(
                    initial_capital=Decimal(capital),
                    costs=CostModel(
                        commission_per_share=Decimal("0.005"),
                        slippage_percent=Decimal("0.05"),
                    ),
                )
                for trade in outcome.trades:
                    self.assertGreaterEqual(trade.cash_after, Decimal("0"))
                self.assertGreaterEqual(outcome.cash_remaining, Decimal("0"))
                # And the ledger and the account agree about the balance it ended on.
                if outcome.trades:
                    self.assertEqual(
                        outcome.cash_remaining, outcome.trades[-1].cash_after
                    )
                else:
                    self.assertEqual(outcome.cash_remaining, Decimal(capital))

    def test_a_commission_larger_than_the_execution_price_is_refused_rather_than_paid(self):
        # A one-dollar commission on a fifty-cent share is a sale that returns less than
        # nothing. Producing it would put a negative cash balance on screen and call it a
        # result, so the run stops and says what to change.
        dates = sessions(date(2024, 1, 1), 10)
        closes = ["0.50", "0.50", "0.50", "0.50", "0.60", "0.70", "0.80", "0.70", "0.60",
                  "0.50"]

        with self.assertRaises(CostModelError) as caught:
            run_backtest(
                bars_from(dates, closes),
                crossover_config(
                    costs=CostModel(
                        commission_per_share=Decimal("1.00"), slippage_percent=Decimal("0")
                    ),
                ),
            )

        self.assertIn("commission", str(caught.exception))


class MetricTests(unittest.TestCase):
    def test_return_and_final_value_follow_the_ledger(self):
        outcome = run_crossover()

        # Bought 71 at 14 out of 1000, sold 71 at 10: 6 + 710 = 716.
        self.assertEqual(outcome.performance.final_value, Decimal("716.00"))
        self.assertEqual(outcome.performance.total_return_percent, Decimal("-28.40"))

    def test_maximum_drawdown_is_the_largest_peak_to_trough_fall(self):
        outcome = run_crossover()

        # Equity: 1000 (open), 1000, 1071, 1142, 1000, 858, 716. The peak is 1142 and the
        # trough after it is 716, so the worst fall is 426 / 1142 = 37.30%.
        self.assertEqual(outcome.performance.max_drawdown_percent, Decimal("37.30"))

    def test_a_curve_that_only_rises_has_no_drawdown(self):
        dates = sessions(date(2024, 1, 1), 10)
        # A step up and then flat, so the fast average crosses once and the price never falls
        # below the entry.
        closes = ["10", "10", "10", "10", "12", "12", "12", "12", "12", "12"]
        outcome = run_backtest(
            bars_from(dates, closes),
            crossover_config(start_date=dates[4], end_date=dates[9]),
        )

        # Bought at 12, marked at 12 throughout: never below the peak.
        self.assertIsNotNone(outcome.open_position)
        self.assertEqual(outcome.performance.max_drawdown_percent, Decimal("0.00"))

    def test_the_drawdown_measures_from_the_initial_capital_and_not_from_the_first_session(self):
        # The window opens on the session the buy executes in, and it opens at 20 while the
        # session closes at 14 -- so the very first reported session is already below the
        # money that was put in. A peak that started at that session rather than at the
        # opening balance would measure the fall from 800 and report 37.50%.
        outcome = run_backtest(
            bars_from(
                DATES,
                CROSSOVER_CLOSES,
                opens=["10", "10", "10", "10", "12", "20", "16", "14", "12", "10"],
            ),
            crossover_config(start_date=DATES[5], end_date=DATES[9]),
        )

        # 50 shares at 20 spends the whole 1000, and the last session closes at 10.
        self.assertEqual(outcome.equity[0].equity, Decimal("1000.00"))
        self.assertEqual(outcome.equity[1].equity, Decimal("700.00"))
        self.assertEqual(outcome.performance.final_value, Decimal("500.00"))
        self.assertEqual(outcome.performance.max_drawdown_percent, Decimal("50.00"))

    def test_the_same_inputs_produce_the_same_result_twice(self):
        first = run_crossover()
        second = run_crossover()

        self.assertEqual(first, second)


class BenchmarkTests(unittest.TestCase):
    def test_the_benchmark_buys_at_the_first_session_open_and_holds(self):
        outcome = run_crossover()

        # Index 4 opens at 12, so 1000 USD buys 83 shares and leaves 4.
        self.assertEqual(len(outcome.benchmark_equity), outcome.sessions + 1)
        position = outcome.benchmark_equity
        self.assertEqual(position[0].equity, Decimal("1000.00"))
        # 4 + 83 * 12.
        self.assertEqual(position[1].equity, Decimal("1000.00"))
        # Marked to each close: 14, 16, 14, 12, 10 -- so 4 + 83 * 14 after the second.
        self.assertEqual(position[2].equity, Decimal("1166.00"))
        self.assertEqual(position[3].equity, Decimal("1332.00"))
        self.assertEqual(position[-1].equity, Decimal("834.00"))

    def test_the_benchmark_uses_the_same_cost_model(self):
        free = run_crossover()
        costly = run_crossover(
            costs=CostModel(
                commission_per_share=Decimal("0.99"), slippage_percent=Decimal("1")
            )
        )

        # The benchmark's own entry is moved by its own slippage and charged its own
        # commission, which is the whole claim being tested.
        self.assertEqual(free.benchmark.final_value, Decimal("834.00"))
        # 1000 / (12 * 1.01 + 0.99) = 76.27, so 76 shares; 76 * 12.12 + 76 * 0.99 = 996.36
        # leaves 3.64, and 3.64 + 76 * 10 = 763.64.
        self.assertEqual(costly.benchmark.final_value, Decimal("763.64"))

    def test_a_benchmark_that_cannot_afford_one_share_holds_cash(self):
        outcome = run_crossover(initial_capital=Decimal("5.00"))

        self.assertEqual(outcome.benchmark.final_value, Decimal("5.00"))
        self.assertEqual(outcome.benchmark.total_return_percent, Decimal("0.00"))
        self.assertTrue(
            any("could not buy a single share" in line for line in outcome.assumptions)
        )

    def test_the_benchmark_curve_starts_from_the_same_capital_and_window(self):
        outcome = run_crossover()

        self.assertEqual(outcome.benchmark_equity[0].on, outcome.equity[0].on)
        self.assertEqual(outcome.benchmark_equity[0].equity, Decimal("1000.00"))
        self.assertEqual(
            outcome.benchmark_equity[-1].on, outcome.equity[-1].on
        )


class UnusableDataTests(unittest.TestCase):
    def test_a_window_with_no_sessions_is_refused_with_the_dates_in_the_message(self):
        with self.assertRaises(UnusableDataError) as caught:
            run_backtest(
                bars_from(DATES, CROSSOVER_CLOSES),
                crossover_config(
                    # The weekend between the 2024-01-05 and 2024-01-08 sessions.
                    start_date=date(2024, 1, 6),
                    end_date=date(2024, 1, 7),
                ),
            )

        message = str(caught.exception)
        self.assertIn("2024-01-06", message)
        self.assertIn("TEST", message)

    def test_too_little_history_before_the_window_is_refused_and_says_how_much_is_needed(self):
        # A three-session slow average needs four sessions before the window. Starting at
        # index 3 gives three.
        with self.assertRaises(UnusableDataError) as caught:
            run_backtest(
                bars_from(DATES, CROSSOVER_CLOSES),
                crossover_config(start_date=DATES[3], end_date=DATES[9]),
            )

        message = str(caught.exception)
        self.assertIn("4 sessions of history", message)
        self.assertIn("Start the test later", message)

    def test_the_boundary_case_of_exactly_enough_history_is_accepted(self):
        outcome = run_backtest(
            bars_from(DATES, CROSSOVER_CLOSES),
            crossover_config(start_date=DATES[4], end_date=DATES[9]),
        )

        self.assertEqual(outcome.warmup_sessions, 4)


class ConfigurationTests(unittest.TestCase):
    def check(self, message: str, **overrides) -> None:
        with self.assertRaises(InvalidConfigurationError) as caught:
            validate_configuration(crossover_config(**overrides))
        self.assertIn(message, str(caught.exception))

    def test_a_fast_period_at_or_above_the_slow_one_is_refused(self):
        self.check("must be shorter than the slow period", fast_period=3, slow_period=3)
        self.check("must be shorter than the slow period", fast_period=10, slow_period=5)

    def test_a_period_below_the_minimum_is_refused(self):
        self.check("between 2 and 400", fast_period=1)
        self.check("between 2 and 400", slow_period=1, fast_period=1)

    def test_a_period_above_the_maximum_is_refused(self):
        self.check("between 2 and 400", slow_period=401)

    def test_dates_must_be_ordered_and_the_window_must_not_be_empty(self):
        self.check("must be before the end date", start_date=DATES[5], end_date=DATES[5])
        self.check("must be before the end date", start_date=DATES[6], end_date=DATES[5])

    def test_a_start_date_older_than_the_available_history_is_refused(self):
        self.check("1980-01-01", start_date=date(1970, 1, 1))

    def test_an_end_date_in_the_future_is_refused(self):
        with self.assertRaises(InvalidConfigurationError) as caught:
            validate_configuration(
                crossover_config(end_date=date(2030, 1, 1)), today=date(2026, 10, 10)
            )
        self.assertIn("in the future", str(caught.exception))

    def test_the_initial_capital_must_be_positive_and_bounded(self):
        self.check("greater than zero", initial_capital=Decimal("0"))
        self.check("greater than zero", initial_capital=Decimal("-100"))
        self.check("at most", initial_capital=Decimal("100000000.01"))

    def test_the_commission_and_slippage_ranges_are_enforced(self):
        self.check(
            "commission must be between",
            costs=CostModel(commission_per_share=Decimal("1.01")),
        )
        self.check(
            "commission must be between",
            costs=CostModel(commission_per_share=Decimal("-0.01")),
        )
        self.check(
            "slippage must be between",
            costs=CostModel(slippage_percent=Decimal("5.01")),
        )
        self.check(
            "slippage must be between",
            costs=CostModel(slippage_percent=Decimal("-0.01")),
        )

    def test_the_engine_refuses_a_configuration_it_was_handed_directly(self):
        # The route validates first, so this is the second line: a caller that skipped the
        # route cannot get a run out of the engine either.
        with self.assertRaises(InvalidConfigurationError):
            run_backtest(bars_from(DATES, CROSSOVER_CLOSES), crossover_config(fast_period=9,
                                                                            slow_period=3))


class ProseTests(unittest.TestCase):
    def test_the_result_states_the_price_return_limitation(self):
        outcome = run_crossover()

        joined = " ".join(outcome.limitations)
        self.assertIn("price return", joined)
        self.assertIn("dividend", joined)

    def test_the_result_states_the_cost_model_it_actually_used(self):
        outcome = run_crossover(
            costs=CostModel(
                commission_per_share=Decimal("0.25"), slippage_percent=Decimal("1.5")
            )
        )

        joined = " ".join(outcome.assumptions)
        self.assertIn("0.25 USD per share", joined)
        self.assertIn("1.5%", joined)

    def test_the_strategy_carries_its_own_rules_and_version(self):
        strategy = MOVING_AVERAGE_CROSSOVER

        self.assertEqual(strategy.version, "1")
        self.assertEqual(
            {param.name: param.default for param in strategy.parameters},
            {"fast_period": 20, "slow_period": 50},
        )
        self.assertIn("next session's open", " ".join(strategy.execution))


# --- the RSI strategy ------------------------------------------------------------------

# A two-session indicator, so a rise and a fall both fit inside a series short enough that
# every reading can be written down. Falls from 13 to 9, then rises back to 16.
RSI_CLOSES = ["10", "11", "12", "13", "12", "11", "10", "9", "10", "11", "12", "13", "14", "15", "16"]
RSI_DATES = sessions(date(2024, 3, 1), len(RSI_CLOSES))


def rsi_config(**overrides) -> BacktestConfig:
    """The RSI fixture's window: indices 5 to 14, with more warm-up than the indicator needs.

    `period + 1` is three sessions; starting at index 5 leaves five, which is slack on purpose.
    The signals under test are the two crossings in the series, not the boundary of the warm-up.
    """
    parameters = {"period": 2, "entry_threshold": 30, "exit_threshold": 70}
    parameters.update(
        {name: overrides.pop(name) for name in list(overrides) if name in parameters}
    )
    settings = {
        "strategy": RSI_REVERSION,
        "symbol": "TEST",
        "start_date": RSI_DATES[5],
        "end_date": RSI_DATES[14],
        "initial_capital": Decimal("1000.00"),
        "parameters": parameters,
        "costs": CostModel(
            commission_per_share=Decimal("0"), slippage_percent=Decimal("0")
        ),
    }
    settings.update(overrides)
    return BacktestConfig(**settings)


def run_rsi(**overrides):
    return run_backtest(bars_from(RSI_DATES, RSI_CLOSES), rsi_config(**overrides))


class RsiIndicatorTests(unittest.TestCase):
    """Wilder's own arithmetic, on a series whose readings can be checked by hand."""

    def readings(self, closes: list[str], period: int = 2):
        from app.backtest import _wilder_rsi

        return _wilder_rsi([Decimal(close) for close in closes], period)

    def test_no_reading_exists_before_period_changes_have_been_seen(self):
        """Initialisation. `period` changes need `period + 1` closes, so the first reading sits
        at that index -- and everything before it is `None` rather than a shorter average."""
        values = self.readings(RSI_CLOSES)

        self.assertEqual(values[0], None)
        self.assertEqual(values[1], None)
        self.assertIsNotNone(values[2])

    def test_the_first_reading_is_the_mean_of_the_first_period_changes(self):
        # Changes are +1, +1: an average gain of 1 and an average loss of 0, so nothing fell
        # and the index is 100.
        self.assertEqual(self.readings(["10", "11", "12"])[2], Decimal("100"))

    def test_each_later_reading_smooths_the_one_before_it(self):
        """Every reading after the first is Wilder's smoothed average, not a fresh window.

        Worked through for the fixture, period 2:

            idx 4  change -1   gain 0.5    loss 0.5    RS 1        -> 50
            idx 5  change -1   gain 0.25   loss 0.75   RS 1/3      -> 25
            idx 6  change -1   gain 0.125  loss 0.875  RS 1/7      -> 12.5
            idx 7  change -1   gain 0.0625 loss 0.9375 RS 1/15     -> 6.25
        """
        values = self.readings(RSI_CLOSES)

        self.assertEqual(values[4], Decimal("50"))
        self.assertEqual(values[5], Decimal("25"))
        self.assertEqual(values[6], Decimal("12.5"))
        self.assertEqual(values[7], Decimal("6.25"))

    def test_a_series_with_no_losses_reads_100(self):
        values = self.readings(["10", "11", "12", "13"])

        self.assertEqual(values[2], Decimal("100"))
        self.assertEqual(values[3], Decimal("100"))

    def test_a_series_with_no_gains_reads_0(self):
        values = self.readings(["13", "12", "11", "10"])

        self.assertEqual(values[2], Decimal("0"))
        self.assertEqual(values[3], Decimal("0"))

    def test_a_flat_series_reads_50_and_not_100(self):
        """A series that did not move has no gain and no loss, so the ratio is undefined.

        Reading it as 100 -- which the "no losses" rule alone would give -- calls a flat line
        extremely overbought, a claim about momentum where there is none. 50 is the honest
        reading and it is what keeps a flat series from signalling.
        """
        values = self.readings(["10", "10", "10", "10"])

        self.assertEqual(values[2], Decimal("50"))
        self.assertEqual(values[3], Decimal("50"))

    def test_a_series_shorter_than_the_indicator_has_no_readings_at_all(self):
        self.assertEqual(self.readings(["10", "11"]), [None, None])


class RsiSignalTests(unittest.TestCase):
    def test_a_fall_through_the_entry_threshold_is_a_buy_at_the_next_sessions_open(self):
        """The first crossing in the fixture: the RSI falls from 50 to 25 through 30.

        Signalled on index 5's close and executed on index 6's open, which is the whole
        "next session" rule applied to a threshold rather than to an average.
        """
        outcome = run_rsi()

        first = outcome.trades[0]
        self.assertEqual(first.side, "buy")
        self.assertEqual(first.signalled_on, RSI_DATES[5])
        self.assertEqual(first.executed_on, RSI_DATES[6])

    def test_a_rise_through_the_exit_threshold_is_a_sell_at_the_next_sessions_open(self):
        # The RSI rises from 53.125 to 76.5625 through 70 on index 9, and the position opened
        # by the buy is still held, so this is the exit.
        outcome = run_rsi()

        sale = outcome.trades[1]
        self.assertEqual(sale.side, "sell")
        self.assertEqual(sale.signalled_on, RSI_DATES[9])
        self.assertEqual(sale.executed_on, RSI_DATES[10])

    def test_two_crossings_are_one_completed_round_trip(self):
        outcome = run_rsi()

        self.assertEqual([trade.side for trade in outcome.trades], ["buy", "sell"])
        self.assertIsNone(outcome.open_position)

    def test_a_level_already_reached_is_not_a_crossing(self):
        """The test starts flat even though the RSI was already below the entry threshold.

        Signs the same series but opens the window at index 7, where the RSI is 6.25 -- deep
        below 30 and going nowhere but up. No crossing down through the threshold happens
        inside the window, so nothing is bought.
        """
        outcome = run_rsi(start_date=RSI_DATES[7])

        self.assertEqual(outcome.trades, ())

    def test_being_below_the_threshold_for_several_sessions_buys_only_once(self):
        """A crossing is the *first* session of the condition, not every session inside it.

        Buying on the level instead of the crossing would buy again on every session of a
        decline, which is the behaviour the crossing rule exists to prevent.
        """
        outcome = run_rsi()

        self.assertEqual([trade.side for trade in outcome.trades].count("buy"), 1)

    def test_a_series_that_never_moves_produces_no_trades(self):
        # Readings of 50 sit between both thresholds and never cross either.
        flat = ["10"] * len(RSI_CLOSES)
        outcome = run_backtest(bars_from(RSI_DATES, flat), rsi_config())

        self.assertEqual(outcome.trades, ())
        self.assertEqual(outcome.performance.total_return_percent, Decimal("0.00"))

    def test_an_rsi_window_with_too_little_history_is_refused(self):
        # `period + 1` is three sessions before the window; index 1 gives one.
        with self.assertRaises(UnusableDataError) as caught:
            run_rsi(start_date=RSI_DATES[1])

        message = str(caught.exception)
        self.assertIn("RSI Reversion", message)
        self.assertIn("needs 3 sessions of history", message)


class RsiConfigurationTests(unittest.TestCase):
    def check(self, message: str, **overrides) -> None:
        with self.assertRaises(InvalidConfigurationError) as caught:
            validate_configuration(rsi_config(**overrides))
        self.assertIn(message, str(caught.exception))

    def test_an_entry_threshold_at_or_above_the_exit_is_refused(self):
        self.check("must be below the exit threshold", entry_threshold=70, exit_threshold=70)
        self.check("must be below the exit threshold", entry_threshold=80, exit_threshold=70)

    def test_a_threshold_outside_the_scale_is_refused(self):
        self.check("between 1 and 99", entry_threshold=0)
        self.check("between 1 and 99", exit_threshold=100)

    def test_a_period_outside_the_supported_range_is_refused(self):
        self.check("between 2 and 400", period=1)
        self.check("between 2 and 400", period=401)

    def test_a_parameter_the_strategy_does_not_declare_is_refused(self):
        """A crossover period sent to an RSI run is refused rather than quietly ignored."""
        self.check(
            "has no parameter called fast_period",
            parameters={"period": 2, "entry_threshold": 30, "exit_threshold": 70,
                        "fast_period": 5},
        )

    def test_a_missing_parameter_is_refused_rather_than_defaulted(self):
        self.check(
            "is required",
            parameters={"period": 2, "entry_threshold": 30},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
