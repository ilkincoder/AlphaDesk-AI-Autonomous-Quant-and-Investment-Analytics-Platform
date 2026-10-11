"""The backtest routes: resolution, refusals, idempotency and what gets stored.

No network and no database. The provider calls are patched out and the store is a dictionary,
which is what lets the whole HTTP-facing behaviour be exercised without a key or a running
Postgres -- and, more importantly, lets a test say what the provider *said* rather than
depending on what it happens to say today.

The arithmetic is not tested here; `test_backtest_engine.py` does that on hand-checkable
fixtures. What is tested here is everything between a request body and a stored row.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError

from app import backtest_api, backtest_explanation
from app.backtest_api import (
    get_instruments,
    get_latest_run,
    get_run,
    get_strategies,
    post_explanation,
    post_run,
)
from app.schemas import BacktestRunRequest
from app.twelvedata import (
    DailyBar,
    DailyBarRange,
    InstrumentMatch,
    ProviderUnavailableError,
    RateLimitError,
    SymbolNotFoundError,
)
from tests.test_backtest_engine import (
    CROSSOVER_CLOSES,
    CROSSOVER_OPENS,
    DATES,
    bars_from,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

NVDA = InstrumentMatch(
    symbol="NVDA",
    name="NVIDIA Corporation",
    exchange="NASDAQ",
    instrument_type="Common Stock",
    currency="USD",
    country="United States",
)


def series_from(bars: list[DailyBar]) -> DailyBarRange:
    """The series the provider would have returned for these bars."""
    return DailyBarRange(
        symbol="NVDA",
        exchange="NASDAQ",
        currency="USD",
        exchange_timezone="America/New_York",
        provider="twelve_data",
        interval="1day",
        adjustment_basis="adjusted",
        provider_adjust_mode="splits",
        retrieved_at=NOW,
        requested_start=bars[0].trading_date,
        requested_end=bars[-1].trading_date,
        bars=tuple(bars),
    )


# The crossover fixture: ten sessions, four of warm-up and six of test window, with one
# completed round trip in it. Long enough to satisfy a three-session slow average and short
# enough that every figure in the response can be checked against `test_backtest_engine`.
FIXTURE_BARS = bars_from(DATES, CROSSOVER_CLOSES, opens=CROSSOVER_OPENS)


class FakeStore:
    """`app.backtests`, as a dictionary.

    Records what it was handed so a test can inspect the row and the snapshot that were stored,
    which is the only way to check the two claims persistence makes: the run is reproducible,
    and a repeated request is not paid for twice.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.by_request: dict[str, dict] = {}
        self.order: list[str] = []
        self.recorded_snapshots: list[dict] = []

    def find(self, request_id: str) -> dict | None:
        return self.by_request.get(request_id)

    def load(self, run_id: str) -> dict | None:
        return self.rows.get(run_id)

    def latest(self) -> dict | None:
        """The row recorded last, which is what the real store's ordering arrives at.

        Every row this fake writes carries the same `created_at`, so ordering by the clock would
        be a tie. The *order they were recorded in* is what stands in for it here -- and the
        store's own ordering is exercised against PostgreSQL rather than in this file.
        """
        if not self.order:
            return None
        return self.rows[self.order[-1]]

    def record(self, *, request_id: str, run: dict, snapshot: dict) -> dict:
        run_id = f"run{len(self.order) + 1:02d}"
        row = {
            "id": run_id,
            "request_id": request_id,
            "created_at": NOW,
            "input_snapshot": snapshot,
            # Null until an explanation is asked for, exactly as the column is.
            "explanation": None,
            "explanation_model": None,
            "explanation_prompt_version": None,
            **run,
        }
        self.rows[run_id] = row
        self.by_request[request_id] = row
        self.order.append(run_id)
        self.recorded_snapshots.append(snapshot)
        return row

    def save_explanation(
        self, run_id: str, *, text: str, model: str | None, prompt_version: str | None
    ) -> dict | None:
        row = self.rows.get(run_id)
        if row is None:
            return None
        row["explanation"] = text
        row["explanation_model"] = model
        row["explanation_prompt_version"] = prompt_version
        return row


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.search = mock.patch.object(
            backtest_api, "search_instruments", return_value=(NVDA,)
        ).start()
        self.fetch = mock.patch.object(
            backtest_api, "fetch_daily_bars_range", return_value=series_from(FIXTURE_BARS)
        ).start()
        mock.patch.object(backtest_api, "backtests", self.store).start()
        self.addCleanup(mock.patch.stopall)

    def request(self, **overrides) -> BacktestRunRequest:
        body = {
            "request_id": "req-1",
            "strategy_id": "moving_average_crossover",
            "symbol": "NVDA",
            "exchange": "NASDAQ",
            "start_date": DATES[4],
            "end_date": DATES[9],
            "initial_capital": Decimal("1000.00"),
            "parameters": {"fast_period": 2, "slow_period": 3},
        }
        body.update(overrides)
        return BacktestRunRequest(**body)

    def assertRefused(self, status: int, detail_contains: str, **overrides) -> str:
        with self.assertRaises(HTTPException) as caught:
            post_run(self.request(**overrides))
        self.assertEqual(caught.exception.status_code, status)
        self.assertIn(detail_contains, caught.exception.detail)
        return caught.exception.detail


class StrategyCatalogueTests(unittest.TestCase):
    def test_every_strategy_comes_with_its_rules_its_parameters_and_the_bounds(self):
        body = get_strategies()

        self.assertEqual(
            [strategy.id for strategy in body.strategies],
            ["moving_average_crossover", "rsi_reversion"],
        )
        strategy = body.strategies[0]
        self.assertEqual(strategy.id, "moving_average_crossover")
        self.assertEqual(strategy.version, "1")
        self.assertEqual(
            {param.name: param.default for param in strategy.parameters},
            {"fast_period": 20, "slow_period": 50},
        )
        # The form refuses an out-of-range entry with these instead of guessing, and the
        # server re-checks every one of them. The bounds travel per parameter, not per
        # strategy, because RSI's thresholds and its period do not share one range.
        self.assertEqual(
            {param.name: (param.minimum, param.maximum) for param in strategy.parameters},
            {"fast_period": (2, 400), "slow_period": (2, 400)},
        )
        self.assertEqual(strategy.max_commission_per_share, Decimal("1.00"))
        self.assertEqual(strategy.max_slippage_percent, Decimal("5.00"))
        self.assertTrue(any("simple moving averages" in rule for rule in strategy.rules))
        self.assertTrue(any("next session's open" in line for line in strategy.execution))

    def test_the_rsi_strategy_is_offered_with_wilders_defaults(self):
        body = get_strategies()

        rsi = body.strategies[1]
        self.assertEqual(rsi.name, "RSI Reversion")
        self.assertEqual(
            {param.name: param.default for param in rsi.parameters},
            {"period": 14, "entry_threshold": 30, "exit_threshold": 70},
        )
        self.assertTrue(any("Wilder" in rule for rule in rsi.rules))


class InstrumentSearchTests(ApiTestCase):
    def test_an_empty_query_returns_no_matches_without_asking_the_provider(self):
        body = get_instruments(q="   ")

        self.assertEqual(body.matches, [])
        self.assertEqual(body.query, "")
        self.search.assert_not_called()

    def test_matches_come_back_with_the_ticker_the_name_and_the_exchange(self):
        body = get_instruments(q="nvidia")

        self.assertEqual(body.query, "nvidia")
        self.assertEqual(len(body.matches), 1)
        match = body.matches[0]
        self.assertEqual(match.symbol, "NVDA")
        self.assertEqual(match.name, "NVIDIA Corporation")
        self.assertEqual(match.exchange, "NASDAQ")
        self.assertEqual(match.instrument_type, "Common Stock")

    def test_nothing_matched_is_an_empty_list_and_not_an_error(self):
        self.search.return_value = ()

        body = get_instruments(q="zzzzqqqq")

        self.assertEqual(body.matches, [])

    def test_a_provider_failure_is_reported_as_a_provider_failure(self):
        self.search.side_effect = RateLimitError("Twelve Data's rate limit is exhausted.")

        with self.assertRaises(HTTPException) as caught:
            get_instruments(q="nvda")

        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("rate limit", caught.exception.detail)


class RunTests(ApiTestCase):
    def test_a_run_returns_the_stored_row_with_the_resolved_instrument(self):
        body = post_run(self.request())

        self.assertEqual(body.symbol, "NVDA")
        self.assertEqual(body.exchange, "NASDAQ")
        self.assertEqual(body.instrument_name, "NVIDIA Corporation")
        self.assertEqual(body.currency, "USD")
        self.assertEqual(body.strategy_id, "moving_average_crossover")
        self.assertEqual(body.strategy_version, "1")
        self.assertEqual(body.strategy_name, "Moving Average Crossover")

    def test_the_response_carries_the_strategy_and_the_benchmark_curves(self):
        # The request takes the schema's default costs -- 0.005 USD a share and 0.05% of
        # slippage -- so this is the costed crossover fixture, end to end. The curves are
        # checked against `test_backtest_engine`'s uncosted ones in the same file, which is
        # what shows the two differ only by what the cost model says.
        body = post_run(self.request())

        self.assertEqual(len(body.equity), 7)
        self.assertEqual(len(body.benchmark_equity), 7)
        self.assertEqual(body.equity[0].equity, Decimal("1000.00"))
        self.assertEqual(body.performance.final_value, Decimal("714.43"))
        self.assertEqual(body.performance.total_return_percent, Decimal("-28.56"))
        self.assertEqual(body.performance.max_drawdown_percent, Decimal("37.39"))
        self.assertEqual(body.benchmark.total_return_percent, Decimal("-16.69"))

    def test_the_ledger_carries_the_execution_date_side_quantity_price_and_costs(self):
        body = post_run(self.request())

        self.assertEqual(len(body.trades), 2)
        buy, sell = body.trades
        self.assertEqual(buy.side, "buy")
        self.assertEqual(buy.quantity, 71)
        self.assertEqual(buy.executed_on, DATES[5])
        self.assertEqual(buy.signalled_on, DATES[4])
        # 71 shares at 0.005 each is 0.355, carried at two decimal places.
        self.assertEqual(buy.commission, Decimal("0.36"))
        self.assertEqual(buy.cash_after, Decimal("5.14"))
        self.assertEqual(sell.side, "sell")
        self.assertEqual(sell.executed_on, DATES[9])

    def test_the_coverage_separates_the_warm_up_from_the_reported_window(self):
        body = post_run(self.request())

        coverage = body.coverage
        assert coverage is not None
        self.assertEqual(coverage.sessions, 6)
        self.assertEqual(coverage.warmup_sessions, 4)
        self.assertEqual(coverage.first_session, DATES[4])
        self.assertEqual(coverage.last_session, DATES[9])
        self.assertEqual(coverage.data_sessions, 10)

    def test_the_result_says_what_the_prices_are_and_what_they_are_not(self):
        body = post_run(self.request())

        self.assertEqual(body.adjustment_basis, "adjusted")
        self.assertEqual(body.provider_adjust_mode, "splits")
        self.assertEqual(body.provider, "twelve_data")
        self.assertTrue(any("price return" in line for line in body.limitations))
        self.assertTrue(any("Dividends" in line for line in body.limitations))

    def test_cash_remaining_and_any_open_position_are_reported(self):
        body = post_run(self.request(end_date=DATES[8]))

        # The buy settled at 994.50 plus 0.36 of commission, and nothing was sold.
        self.assertEqual(body.cash_remaining, Decimal("5.14"))
        position = body.open_position
        assert position is not None
        self.assertEqual(position.quantity, 71)
        self.assertEqual(position.mark_price, Decimal("12.0000"))
        self.assertEqual(position.market_value, Decimal("852.00"))
        self.assertEqual(body.performance.final_value, Decimal("857.14"))

    def test_a_repeated_request_is_answered_from_the_row_it_already_has(self):
        first = post_run(self.request())
        self.fetch.reset_mock()
        self.search.reset_mock()

        second = post_run(self.request())

        self.assertEqual(second.run_id, first.run_id)
        # Neither the bars nor the listing were asked for again: a double click must not cost
        # a second provider request, or a second row.
        self.fetch.assert_not_called()
        self.search.assert_not_called()
        self.assertEqual(len(self.store.rows), 1)

    def test_two_different_request_ids_are_two_runs(self):
        first = post_run(self.request(request_id="req-1"))
        second = post_run(self.request(request_id="req-2"))

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(len(self.store.rows), 2)

    def test_the_answer_serialises_with_every_decimal_as_a_string(self):
        body = post_run(self.request())

        payload = json.loads(body.model_dump_json())
        self.assertEqual(payload["performance"]["final_value"], "714.43")
        self.assertEqual(payload["equity"][0]["equity"], "1000.00")
        # 14 moved by the slippage model, at the four decimal places an execution price
        # carries -- a string, so no float has been near it.
        self.assertEqual(payload["trades"][0]["price"], "14.0070")
        self.assertIsInstance(payload["equity"][0]["on"], str)


class InstrumentResolutionTests(ApiTestCase):
    def test_a_request_without_an_exchange_resolves_one_and_records_it(self):
        # The portfolio path: a holding carries a ticker and nothing else.
        body = post_run(self.request(exchange=None))

        self.assertEqual(body.symbol, "NVDA")
        self.assertEqual(body.exchange, "NASDAQ")
        # And the bars were asked for on the exchange that was resolved, not on a guess.
        self.assertEqual(self.fetch.call_args.args[:2], ("NVDA", "NASDAQ"))

    def test_a_named_exchange_that_no_us_listing_carries_is_refused(self):
        self.search.return_value = (
            NVDA,
            InstrumentMatch("NVDA", "NVIDIA Corporation", "NYSE", "Common Stock", "USD",
                            "United States"),
        )

        detail = self.assertRefused(422, "no US-listed match on LSE", exchange="LSE")

        self.assertIn("NASDAQ", detail)
        self.fetch.assert_not_called()

    def test_a_ticker_with_no_us_listing_is_refused_before_any_bars_are_asked_for(self):
        # What the provider really gives back for a foreign-only ticker once the US-dollar
        # filter has been applied: nothing at all.
        self.search.return_value = ()

        self.assertRefused(
            422,
            "No US-listed, USD-denominated instrument matching",
            symbol="AAPL",
            exchange=None,
        )

        self.fetch.assert_not_called()

    def test_the_run_records_the_listing_the_search_returned_and_not_the_letters_typed(self):
        post_run(self.request(symbol="nvda", exchange=None))

        row = self.store.rows["run01"]
        self.assertEqual(row["symbol"], "NVDA")
        self.assertEqual(row["exchange"], "NASDAQ")
        self.assertEqual(row["instrument_name"], "NVIDIA Corporation")
        self.assertEqual(row["instrument_type"], "Common Stock")

    def test_a_provider_failure_during_resolution_is_reported_as_one(self):
        self.search.side_effect = ProviderUnavailableError("Could not reach api.twelvedata.com.")

        self.assertRefused(502, "Could not reach", exchange=None)


class ConfigurationRefusalTests(ApiTestCase):
    def test_an_unknown_strategy_is_refused_and_the_known_one_is_named(self):
        detail = self.assertRefused(422, "is not a strategy this application defines",
                                    strategy_id="buy_low_sell_high")

        self.assertIn("moving_average_crossover", detail)
        self.fetch.assert_not_called()

    def test_a_fast_period_at_or_above_the_slow_one_is_refused_before_any_provider_call(self):
        self.assertRefused(
            422,
            "must be shorter than the slow period",
            parameters={"fast_period": 10, "slow_period": 5},
        )

        self.fetch.assert_not_called()
        self.search.assert_not_called()

    def test_dates_out_of_order_are_refused(self):
        self.assertRefused(422, "must be before the end date",
                           start_date=DATES[9], end_date=DATES[4])

    def test_a_future_end_date_is_refused(self):
        self.assertRefused(422, "in the future", end_date=date(2099, 1, 1))

    def test_a_non_positive_capital_is_refused_by_the_schema(self):
        # The schema's own bound, which is the first of the two places this is checked -- so
        # the request never reaches a handler that would spend a provider credit on it.
        with self.assertRaises(ValidationError):
            self.request(initial_capital=Decimal("0"))
        with self.assertRaises(ValidationError):
            self.request(initial_capital=Decimal("-1"))

    def test_a_cost_outside_the_supported_range_is_refused(self):
        from app.schemas import BacktestCostsIn

        self.assertRefused(
            422,
            "commission must be between",
            costs=BacktestCostsIn(commission_per_share=Decimal("1.50")),
        )
        self.assertRefused(
            422,
            "slippage must be between",
            costs=BacktestCostsIn(slippage_percent=Decimal("9")),
        )

    def test_a_window_the_data_cannot_answer_is_refused_with_the_engine_explanation(self):
        # One session of warm-up for a three-session slow average.
        self.assertRefused(422, "sessions of history before the test starts",
                           start_date=DATES[1], end_date=DATES[9])

    def test_a_window_holding_no_sessions_is_refused_with_the_dates_in_the_message(self):
        self.assertRefused(422, "No trading sessions fall between", start_date=date(2024, 1, 6),
                           end_date=date(2024, 1, 7))

    def test_a_provider_failure_while_fetching_bars_is_mapped_to_a_status(self):
        self.fetch.side_effect = SymbolNotFoundError("Twelve Data does not know that ticker.")

        self.assertRefused(422, "does not know that ticker")


class PersistenceTests(ApiTestCase):
    def test_the_exact_bars_that_were_used_are_stored_with_the_run(self):
        post_run(self.request())

        snapshot = self.store.recorded_snapshots[0]
        self.assertEqual(snapshot["provider"], "twelve_data")
        self.assertEqual(snapshot["provider_adjust_mode"], "splits")
        self.assertEqual(len(snapshot["bars"]), 10)
        # Warm-up included: the run has to be reproducible, and the sessions that produced the
        # first indicator values are part of what it read.
        self.assertEqual(snapshot["bars"][0]["date"], DATES[0].isoformat())
        self.assertEqual(snapshot["bars"][0]["close"], "10")
        self.assertEqual(snapshot["bars"][6]["close"], "16")

    def test_the_stored_prices_are_strings_so_no_float_can_have_touched_them(self):
        post_run(self.request())

        bar = self.store.recorded_snapshots[0]["bars"][5]
        self.assertEqual(bar["open"], "14")
        self.assertIsInstance(bar["open"], str)
        self.assertIsInstance(bar["volume"], int)

    def test_the_row_pins_the_strategy_version_and_the_cost_model(self):
        post_run(self.request())

        row = self.store.rows["run01"]
        self.assertEqual(row["strategy_id"], "moving_average_crossover")
        self.assertEqual(row["strategy_version"], "1")
        self.assertEqual(row["commission_per_share"], Decimal("0.005"))
        self.assertEqual(row["slippage_percent"], Decimal("0.05"))
        self.assertEqual(row["parameters"], {"fast_period": 2, "slow_period": 3})

    def test_a_run_can_be_read_back_and_is_the_same_body(self):
        written = post_run(self.request())

        read = get_run(written.run_id)

        self.assertEqual(read.model_dump(), written.model_dump())

    def test_an_unknown_run_id_is_a_404(self):
        with self.assertRaises(HTTPException) as caught:
            get_run("nope")

        self.assertEqual(caught.exception.status_code, 404)

    def test_reading_a_run_back_shows_the_recorded_arithmetic_and_not_a_fresh_calculation(self):
        written = post_run(self.request())
        # Something changes about the world after the run: the snapshot is what stands.
        self.store.rows["run01"]["initial_capital"] = Decimal("2000.00")

        read = get_run("run01")

        self.assertEqual(read.initial_capital, Decimal("2000.00"))
        self.assertEqual(read.performance.final_value, written.performance.final_value)


class LatestRunTests(ApiTestCase):
    def test_no_run_yet_is_an_explicit_null_and_not_a_404(self):
        """The page asks "is there one?" on load, and "no" is an answer rather than an error."""
        body = get_latest_run()

        self.assertIsNone(body.run)

    def test_the_most_recent_run_is_the_one_that_comes_back(self):
        first = post_run(self.request())
        second = post_run(self.request(request_id="req-2"))

        body = get_latest_run()

        self.assertIsNotNone(body.run)
        self.assertEqual(body.run.run_id, second.run_id)
        self.assertNotEqual(body.run.run_id, first.run_id)

    def test_restoring_the_latest_run_asks_the_provider_for_nothing(self):
        """The row already holds the instrument, the configuration and the results.

        Re-running here would spend a provider credit to arrive at the same figures, and would
        show today's arithmetic under yesterday's evidence.
        """
        post_run(self.request())
        self.fetch.reset_mock()
        self.search.reset_mock()

        body = get_latest_run()

        self.assertIsNotNone(body.run)
        self.assertEqual(self.fetch.call_count, 0)
        self.assertEqual(self.search.call_count, 0)

    def test_the_latest_route_is_not_read_as_a_run_id(self):
        """`/runs/latest` and `/runs/{run_id}` overlap, and registration order decides which wins.

        Driven through the real application because that ordering is the thing under test: a
        direct call to the handler passes however the routes happened to be registered.
        """
        from fastapi.testclient import TestClient

        from app.main import app

        written = post_run(self.request())

        response = TestClient(app).get("/backtest/runs/latest")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run"]["run_id"], written.run_id)


class RsiRunTests(ApiTestCase):
    """The second strategy, end to end: its own parameters, its own trades, its own restore."""

    RSI_PARAMETERS = {"period": 2, "entry_threshold": 30, "exit_threshold": 70}

    def rsi_request(self, **overrides) -> BacktestRunRequest:
        overrides.setdefault("strategy_id", "rsi_reversion")
        overrides.setdefault("parameters", dict(self.RSI_PARAMETERS))
        return self.request(**overrides)

    def test_an_rsi_run_records_its_own_parameters_and_the_version_beside_them(self):
        body = post_run(self.rsi_request())

        self.assertEqual(body.strategy_id, "rsi_reversion")
        self.assertEqual(body.strategy_version, "1")
        self.assertEqual(body.strategy_name, "RSI Reversion")
        self.assertEqual(body.parameters, self.RSI_PARAMETERS)
        # On the row, not only in the response: a restored run reads the row.
        self.assertEqual(self.store.rows["run01"]["parameters"], self.RSI_PARAMETERS)

    def test_an_rsi_run_trades_on_its_own_signals(self):
        """The fixture falls from 16 to 10 over its last three sessions.

        The RSI crosses down through 30 on the second of them, so one buy is signalled and
        executed at the open of the third -- which is the last session of the window, so
        nothing sells it and the position is still open at the end.
        """
        body = post_run(self.rsi_request())

        self.assertEqual([trade.side for trade in body.trades], ["buy"])
        self.assertIsNotNone(body.open_position)

    def test_a_saved_rsi_run_restores_its_own_parameters(self):
        written = post_run(self.rsi_request())

        restored = get_latest_run()

        self.assertEqual(restored.run.parameters, written.parameters)
        self.assertEqual(restored.run.strategy_id, "rsi_reversion")

    def test_a_parameter_the_strategy_does_not_declare_is_refused(self):
        # A crossover period sent to an RSI run is a request this strategy cannot answer, and
        # answering it anyway would silently ignore half of what was asked for.
        self.assertRefused(
            422,
            "has no parameter called fast_period",
            strategy_id="rsi_reversion",
            parameters={**self.RSI_PARAMETERS, "fast_period": 5},
        )

        self.fetch.assert_not_called()

    def test_the_crossover_and_rsi_runs_are_stored_side_by_side(self):
        """Two strategies, two rows, each with the parameters it was run with."""
        crossover = post_run(self.request())
        rsi = post_run(self.rsi_request(request_id="req-rsi"))

        self.assertEqual(
            self.store.rows[crossover.run_id]["parameters"],
            {"fast_period": 2, "slow_period": 3},
        )
        self.assertEqual(self.store.rows[rsi.run_id]["parameters"], self.RSI_PARAMETERS)


class ExplanationRouteTests(ApiTestCase):
    """Explaining a run: written once, reused afterwards, and never at the results' expense."""

    EXPLANATION = "The strategy acted twice and ended at 714.43 USD, a return of -28.56%."

    def scripted(self, outcome):
        """Replace the model call with a fixed outcome, in the shape `explain` returns."""
        return mock.patch.object(backtest_api.backtest_explanation, "explain", return_value=outcome)

    def ok(self):
        return backtest_explanation.Explanation(
            status=backtest_explanation.STATUS_OK,
            text=self.EXPLANATION,
            model="deepseek-flash",
            prompt_version="1",
        )

    def test_the_explanation_is_written_once_and_then_read_from_the_row(self):
        written = post_run(self.request())

        with self.scripted(self.ok()) as model:
            first = post_explanation(written.run_id)
            second = post_explanation(written.run_id)

        self.assertEqual(first.status, "ok")
        self.assertEqual(first.explanation, self.EXPLANATION)
        self.assertEqual(second.explanation, self.EXPLANATION)
        # The second request cost nothing: the text was already on the row.
        self.assertEqual(model.call_count, 1)

    def test_the_model_and_the_prompt_version_are_stored_beside_the_text(self):
        written = post_run(self.request())

        with self.scripted(self.ok()):
            post_explanation(written.run_id)

        row = self.store.rows[written.run_id]
        self.assertEqual(row["explanation"], self.EXPLANATION)
        self.assertEqual(row["explanation_model"], "deepseek-flash")
        self.assertEqual(row["explanation_prompt_version"], "1")

    def test_a_stored_explanation_comes_back_with_the_run_it_belongs_to(self):
        written = post_run(self.request())
        with self.scripted(self.ok()):
            post_explanation(written.run_id)

        # Both the single read and the restore read carry it, so a reloaded page shows the
        # prose it was showing before.
        self.assertEqual(get_run(written.run_id).explanation, self.EXPLANATION)
        self.assertEqual(get_latest_run().run.explanation, self.EXPLANATION)

    def test_a_run_with_no_explanation_reads_as_null_rather_than_an_error(self):
        written = post_run(self.request())

        read = get_run(written.run_id)

        self.assertIsNone(read.explanation)
        # And the run itself is complete: the figures never depended on the prose.
        self.assertEqual(read.performance.total_return_percent, Decimal("-28.56"))

    def test_an_unavailable_explanation_is_a_200_and_leaves_the_results_alone(self):
        written = post_run(self.request())
        unavailable = backtest_explanation.Explanation(
            status=backtest_explanation.STATUS_UNAVAILABLE,
            reason=backtest_explanation.REASON_PROVIDER,
            detail="the provider did not answer",
        )

        with self.scripted(unavailable):
            body = post_explanation(written.run_id)

        self.assertEqual(body.status, "unavailable")
        self.assertEqual(body.reason, "provider_unavailable")
        self.assertIsNone(body.explanation)
        # Nothing was stored, so the next request tries again rather than replaying a failure.
        self.assertIsNone(self.store.rows[written.run_id]["explanation"])
        self.assertEqual(get_run(written.run_id).performance.final_value, Decimal("714.43"))

    def test_a_failed_explanation_can_be_asked_for_again(self):
        written = post_run(self.request())
        unavailable = backtest_explanation.Explanation(
            status=backtest_explanation.STATUS_UNAVAILABLE,
            reason=backtest_explanation.REASON_PROVIDER,
            detail="the provider did not answer",
        )

        with self.scripted(unavailable):
            post_explanation(written.run_id)
        with self.scripted(self.ok()):
            retried = post_explanation(written.run_id)

        self.assertEqual(retried.status, "ok")
        self.assertEqual(retried.explanation, self.EXPLANATION)

    def test_explaining_a_run_that_is_not_stored_is_a_404(self):
        with self.assertRaises(HTTPException) as caught:
            post_explanation("nope")

        self.assertEqual(caught.exception.status_code, 404)

    def test_the_response_carries_no_provider_credentials(self):
        written = post_run(self.request())

        with self.scripted(self.ok()):
            body = post_explanation(written.run_id)

        rendered = body.model_dump_json()
        self.assertNotIn("api_key", rendered)
        self.assertNotIn("sk-", rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
