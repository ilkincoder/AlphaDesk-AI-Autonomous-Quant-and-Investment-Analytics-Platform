"""The backtest API: the strategy catalogue, instrument search, running one, reading one back.

Four routes, and one rule about what they are. **Nothing here places an order.** A backtest is
arithmetic over prices that have already happened; the trades it reports were never sent
anywhere, and there is no route in this module that could send one. That is the whole difference
between this feature and the execution milestone beside it.

**The calculation is in `app.backtest`, not here.** These handlers resolve an instrument, obtain
bars, call the engine and store what it returned. Nothing decides a position, sizes a trade or
computes a return -- so the arithmetic can be tested against hand-checkable fixtures without an
HTTP client, a provider or a database.

**The instrument is resolved, not assumed.** A search result carries its exchange and is sent
back with it; a portfolio holding carries only a ticker, so the exchange is looked up here. The
resolved listing -- symbol, exchange, name, type -- is what gets recorded, because the same
ticker on two exchanges is two instruments with two price series.

**Handlers are `def`, not `async def`,** for the same reason `app.rebalance_api`'s are: the work
blocks on httpx and psycopg2, and FastAPI runs a plain `def` path operation in its threadpool, so
`/health` keeps answering while a backtest is being fetched.

**A failure is an error, not a stored row.** A run that could not reach the provider, or whose
window holds too few sessions for the chosen periods, writes nothing: `app.backtests` records
answers, and there is no answer here to record. The parameters stay on the client, which is what
lets the page keep the configuration visible after a failed run.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException

from app import backtest, backtest_explanation, backtests
from app.schemas import (
    BacktestCoverageOut,
    BacktestExplanationOut,
    BacktestEquityPointOut,
    BacktestInstrumentListOut,
    BacktestInstrumentOut,
    BacktestOpenPositionOut,
    BacktestParamOut,
    BacktestPerformanceOut,
    BacktestRunOut,
    BacktestRunRequest,
    BacktestRunView,
    BacktestStrategyListOut,
    BacktestStrategyOut,
    BacktestTradeOut,
)
from app.twelvedata import (
    DailyBar,
    DataValidationError,
    InvalidRequestError,
    MalformedResponseError,
    MissingApiKeyError,
    ProviderAuthError,
    ProviderUnavailableError,
    RateLimitError,
    SymbolNotFoundError,
    TwelveDataError,
    fetch_daily_bars_range,
    search_instruments,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/backtest", tags=["backtest"])


@dataclass(frozen=True)
class _Instrument:
    """The listing a run is over: the ticker *and* the exchange it was resolved to.

    Its own type rather than a tuple, because three of these five fields are carried for the
    record and not for anything the run does -- and a tuple whose positions have to be remembered
    is how the wrong one gets stored.
    """

    symbol: str
    exchange: str
    name: str
    instrument_type: str
    currency: str


# --- the catalogue ----------------------------------------------------------------------


@router.get(
    "/strategies",
    response_model=BacktestStrategyListOut,
    summary="The strategies this application defines",
)
def get_strategies() -> BacktestStrategyListOut:
    """Every predefined strategy, with its rules and the bounds a configuration is checked against.

    Users choose from this list; they do not write a strategy and they do not write code. Served
    from the engine's own definition rather than duplicated in the page, so the explanation of a
    strategy and the arithmetic that runs it cannot drift apart.
    """
    return BacktestStrategyListOut(
        strategies=[
            BacktestStrategyOut(
                id=strategy.id,
                version=strategy.version,
                name=strategy.name,
                summary=strategy.summary,
                rules=list(strategy.rules),
                execution=list(strategy.execution),
                parameters=[
                    BacktestParamOut(
                        name=param.name,
                        label=param.label,
                        default=param.default,
                        minimum=param.minimum,
                        maximum=param.maximum,
                    )
                    for param in strategy.parameters
                ],
                min_commission_per_share=backtest.MIN_COMMISSION_PER_SHARE,
                max_commission_per_share=backtest.MAX_COMMISSION_PER_SHARE,
                min_slippage_percent=backtest.MIN_SLIPPAGE_PERCENT,
                max_slippage_percent=backtest.MAX_SLIPPAGE_PERCENT,
                max_initial_capital=backtest.MAX_INITIAL_CAPITAL,
            )
            for strategy in backtest.STRATEGIES
        ]
    )


# --- instrument search ------------------------------------------------------------------


@router.get(
    "/instruments",
    response_model=BacktestInstrumentListOut,
    summary="Find a stock by ticker or company name",
    responses={503: {"description": "The market-data provider could not answer."}},
)
def get_instruments(q: str = "") -> BacktestInstrumentListOut:
    """US-listed, USD-denominated instruments matching `q`, best match first.

    A ticker or a company name, because the provider matches on either and a ticker-only lookup
    would have to guess a symbol from a name -- which this does not do.

    An empty `matches` list is an answer: nothing matched. It is not an error, and the page says
    so differently from a search that could not be run at all.
    """
    query = q.strip()
    if not query:
        # No request is made for an empty box. A search for nothing has no results by
        # definition, and spending a credit to confirm that would be spending it on nothing.
        return BacktestInstrumentListOut(query=query, matches=[])

    try:
        matches = search_instruments(query)
    except TwelveDataError as exc:
        raise _provider_error(exc) from exc

    return BacktestInstrumentListOut(
        query=query,
        matches=[
            BacktestInstrumentOut(
                symbol=match.symbol,
                name=match.name,
                exchange=match.exchange,
                instrument_type=match.instrument_type,
                currency=match.currency,
                country=match.country,
            )
            for match in matches
        ],
    )


# --- running one ------------------------------------------------------------------------


def _find_existing(request_id: str) -> BacktestRunOut | None:
    """The run this request id already produced, as a response, or `None`."""

    stored = backtests.find(request_id)
    return _to_out(stored) if stored is not None else None


@router.post(
    "/runs",
    response_model=BacktestRunOut,
    summary="Run a backtest and store it",
    responses={
        200: {"description": "The run this request produced, or the one it already had."},
        422: {"description": "The configuration is not one this engine will run."},
        502: {"description": "The market-data provider refused, or could not be reached."},
    },
)
def post_run(payload: BacktestRunRequest) -> BacktestRunOut:
    """Resolve the instrument, fetch the bars, run the strategy and the benchmark, and store it.

    The response is the stored row rather than a value built beside it, so what a client reads is
    exactly what was recorded.

    **A repeated `request_id` is answered from the row it already has.** Nothing is fetched, no
    credit is spent, and the second request costs one indexed lookup. That is what makes a double
    click and a retried POST the same as one.
    """
    existing = _find_existing(payload.request_id)
    if existing is not None:
        return existing

    strategy = backtest.STRATEGIES_BY_ID.get(payload.strategy_id)
    if strategy is None:
        known = ", ".join(sorted(backtest.STRATEGIES_BY_ID))
        raise HTTPException(
            status_code=422,
            detail=(
                f"{payload.strategy_id!r} is not a strategy this application defines. "
                f"Available: {known}."
            ),
        )

    config = backtest.BacktestConfig(
        strategy=strategy,
        symbol=payload.symbol.strip().upper(),
        start_date=payload.start_date,
        end_date=payload.end_date,
        initial_capital=payload.initial_capital,
        parameters=dict(payload.parameters),
        costs=backtest.CostModel(
            commission_per_share=payload.costs.commission_per_share,
            slippage_percent=payload.costs.slippage_percent,
        ),
    )
    try:
        backtest.validate_configuration(config, today=date.today())
    except backtest.InvalidConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    instrument = _resolve_instrument(config.symbol, payload.exchange)
    series = _fetch_bars(instrument, config)

    try:
        outcome = backtest.run_backtest(list(series.bars), config)
    except backtest.InvalidConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except backtest.UnusableDataError as exc:
        # 422 rather than a service error: nothing is broken and nothing is unavailable. The
        # window and the periods are a request this data cannot answer, and one of them has to
        # change -- which is what the message says.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except backtest.CostModelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    stored = backtests.record(
        request_id=payload.request_id,
        run=_columns(config, instrument, series, outcome),
        snapshot=_snapshot(series),
    )
    return _to_out(stored)


def _resolve_instrument(symbol: str, exchange: str | None) -> _Instrument:
    """The listing this run is over, looked up rather than assumed.

    A ticker alone is not an instrument. The provider's search is asked which US-listed,
    USD-denominated listings answer to this symbol, and the exchange that came with the request --
    present when a search result was chosen -- picks between them. When there is no exchange, the
    first match is taken, which is the provider's own relevance order.

    **A symbol with no US listing is refused here, before any bars are asked for.** The
    alternative is a `time_series` call that may quietly answer about a foreign listing, and a
    backtest whose prices are in another currency is not one this application runs.
    """
    requested = exchange.strip().upper() if exchange and exchange.strip() else None

    try:
        matches = search_instruments(symbol)
    except TwelveDataError as exc:
        raise _provider_error(exc) from exc

    if requested is not None:
        for match in matches:
            if match.symbol == symbol and match.exchange == requested:
                return _Instrument(match.symbol, match.exchange, match.name, match.instrument_type,
                                   match.currency)
        if matches:
            # The exchange was named and no US listing carries it. Falling back to another
            # exchange would run the test over an instrument nobody chose.
            available = ", ".join(sorted({match.exchange for match in matches}))
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{symbol} has no US-listed match on {requested}. Matches found on: "
                    f"{available}. Choose one of those."
                ),
            )
        raise HTTPException(
            status_code=422,
            detail=f"No US-listed instrument matching {symbol!r} was found.",
        )

    if not matches:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No US-listed, USD-denominated instrument matching {symbol!r} was found. "
                "Check the ticker, or search for it by name."
            ),
        )

    first = matches[0]
    return _Instrument(
        first.symbol, first.exchange, first.name, first.instrument_type, first.currency
    )


def _fetch_bars(instrument: _Instrument, config: backtest.BacktestConfig):
    """The bars this run needs: the test window, plus the warm-up the periods require.

    The start is pulled back by `warmup_lookback_days`, which is a calendar-day estimate of the
    sessions the strategy's indicator needs with room for weekends and exchange holidays. Asking
    for a few more days than are needed costs nothing -- the engine trims the warm-up to what the
    indicator actually uses -- while asking for too few is a run that cannot start.
    """
    sessions = config.strategy.warmup_sessions(config.parameters)
    start = config.start_date - timedelta(days=backtest.warmup_lookback_days(sessions))

    try:
        return fetch_daily_bars_range(
            instrument.symbol, instrument.exchange, start, config.end_date
        )
    except TwelveDataError as exc:
        raise _provider_error(exc) from exc


def _provider_error(exc: TwelveDataError) -> HTTPException:
    """Map a provider failure onto a response a person can act on.

    The messages come from the client and are already written to be read. What is decided here is
    only the status: a missing key is the application's own misconfiguration and says so with 503,
    a ticker nobody knows is a 422 about the request, a rate limit or an outage is a 502 about the
    provider, and a body this build cannot read is a 502 as well -- the provider answered, and the
    answer was not something this application can use.
    """
    if isinstance(exc, MissingApiKeyError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, (SymbolNotFoundError, InvalidRequestError)):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ProviderAuthError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, (RateLimitError, ProviderUnavailableError)):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, (MalformedResponseError, DataValidationError)):
        logger.warning("the price provider answered unusably: %s", type(exc).__name__)
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


# --- reading one back -------------------------------------------------------------------


@router.get(
    "/runs/latest",
    response_model=BacktestRunView,
    summary="Read the most recent run",
)
def get_latest_run() -> BacktestRunView:
    """The most recently stored run, or an explicit null.

    This is what the page reads on load. Nothing is fetched and nothing is recalculated: the row
    already holds the resolved instrument, the configuration and the results, so restoring is one
    indexed read. Re-running the backtest here would spend a provider credit to arrive at the
    same figures, and would show today's arithmetic under yesterday's evidence.

    **Declared before `/runs/{run_id}` on purpose.** FastAPI matches in registration order, so a
    `latest` segment would otherwise be read as a run id and answered as a 404.
    """
    stored = backtests.latest()
    if stored is None:
        return BacktestRunView(run=None)
    return BacktestRunView(run=_to_out(stored))


@router.get(
    "/runs/{run_id}",
    response_model=BacktestRunOut,
    summary="Read one stored run",
    responses={404: {"description": "No such run."}},
)
def get_run(run_id: str) -> BacktestRunOut:
    stored = backtests.load(run_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"No backtest run {run_id!r} is stored.")
    return _to_out(stored)


# --- explaining one ---------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/explanation",
    response_model=BacktestExplanationOut,
    summary="Explain a stored run, writing the explanation once",
    responses={404: {"description": "No such run."}},
)
def post_explanation(run_id: str) -> BacktestExplanationOut:
    """The explanation of this run, generating it the first time and reusing it afterwards.

    POST because the first call asks a model to write something and stores it. A repeated call
    is answered from the stored text and costs nothing -- the same shape as a repeated run
    `request_id`, and what makes a reload, a double click or a retry free.

    **Every failure is a 200 with a status.** A run's results are complete without an
    explanation, so no model condition is allowed to present itself as a failure of this page:
    a missing key, a provider that will not answer and a reply that cannot be used all come back
    as `unavailable` with a reason, and the figures stay on screen.
    """
    stored = backtests.load(run_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"No backtest run {run_id!r} is stored.")

    existing = stored.get("explanation")
    if existing:
        # Already written. Nothing is asked of the model, and the stored provenance comes back
        # with it, so the same request twice is one explanation.
        return BacktestExplanationOut(
            status=backtest_explanation.STATUS_OK,
            explanation=existing,
            model=stored.get("explanation_model"),
            prompt_version=stored.get("explanation_prompt_version"),
        )

    outcome = backtest_explanation.explain(stored)
    if not outcome.is_ok or not outcome.text:
        return BacktestExplanationOut(
            status=outcome.status, reason=outcome.reason, detail=outcome.detail
        )

    # Only text is stored, so a run that could not be explained keeps its nulls and the next
    # request tries again rather than replaying the failure.
    saved = backtests.save_explanation(
        run_id,
        text=outcome.text,
        model=outcome.model,
        prompt_version=outcome.prompt_version,
    )
    if saved is None:  # pragma: no cover - the run was deleted between the two statements
        return BacktestExplanationOut(
            status=backtest_explanation.STATUS_OK,
            explanation=outcome.text,
            model=outcome.model,
            prompt_version=outcome.prompt_version,
        )

    return BacktestExplanationOut(
        status=outcome.status,
        explanation=outcome.text,
        model=outcome.model,
        prompt_version=outcome.prompt_version,
    )


# --- mapping ----------------------------------------------------------------------------


def _columns(config, instrument, series, outcome) -> dict:
    """The row's own columns, as the store takes them.

    Kept apart from the response body: the row records what a run *was* -- its resolved listing,
    its pinned strategy version and the coverage of the data it read -- while the result holds
    what it produced. `result` is a JSONB dict rather than columns because it is a document
    shaped by the engine, and giving every metric and curve point a column would be a schema that
    grows with the engine.
    """
    return {
        "strategy_id": config.strategy.id,
        "strategy_version": config.strategy.version,
        "symbol": instrument.symbol,
        "exchange": instrument.exchange,
        "instrument_name": instrument.name,
        "instrument_type": instrument.instrument_type,
        "currency": instrument.currency,
        "start_date": config.start_date,
        "end_date": config.end_date,
        "initial_capital": config.initial_capital,
        "parameters": dict(config.parameters),
        "commission_per_share": config.costs.commission_per_share,
        "slippage_percent": config.costs.slippage_percent,
        "provider": series.provider,
        "adjustment_basis": series.adjustment_basis,
        "provider_adjust_mode": series.provider_adjust_mode,
        "data_retrieved_at": series.retrieved_at,
        "data_first_date": outcome.data_first_date,
        "data_last_date": outcome.data_last_date,
        "data_sessions": len(series.bars),
        "result": _result(outcome),
    }


def _snapshot(series) -> dict:
    """The exact bars this run consumed, as plain values.

    This is the run's evidence. `daily_prices` holds the same sessions, but a row there can be
    replaced by an upsert and a provider that revises its split history rewrites them all -- so a
    reference would not be a record of what was read. Prices are written as strings, like every
    other price this API sends: a JSON number would be a binary float, and the whole reason these
    values are `Decimal` is that a float is not one.
    """
    return {
        "provider": series.provider,
        "interval": series.interval,
        "adjustment_basis": series.adjustment_basis,
        "provider_adjust_mode": series.provider_adjust_mode,
        "exchange": series.exchange,
        "currency": series.currency,
        "exchange_timezone": series.exchange_timezone,
        "retrieved_at": series.retrieved_at.isoformat(),
        "requested_start": series.requested_start.isoformat(),
        "requested_end": series.requested_end.isoformat(),
        "bars": [_snapshot_bar(bar) for bar in series.bars],
    }


def _snapshot_bar(bar: DailyBar) -> dict:
    return {
        "date": bar.trading_date.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
    }


def _result(outcome) -> dict:
    """What the engine produced, as the document `result` stores."""
    return {
        "equity": [_point(point) for point in outcome.equity],
        "benchmark_equity": [_point(point) for point in outcome.benchmark_equity],
        "trades": [
            {
                "executed_on": trade.executed_on.isoformat(),
                "side": trade.side,
                "quantity": trade.quantity,
                "price": str(trade.price),
                "gross_value": str(trade.gross_value),
                "commission": str(trade.commission),
                "cash_after": str(trade.cash_after),
                "signalled_on": trade.signalled_on.isoformat(),
            }
            for trade in outcome.trades
        ],
        "performance": _performance(outcome.performance),
        "benchmark": _performance(outcome.benchmark),
        "cash_remaining": str(outcome.cash_remaining),
        "open_position": (
            {
                "quantity": outcome.open_position.quantity,
                "entry_date": outcome.open_position.entry_date.isoformat(),
                "entry_price": str(outcome.open_position.entry_price),
                "mark_date": outcome.open_position.mark_date.isoformat(),
                "mark_price": str(outcome.open_position.mark_price),
                "market_value": str(outcome.open_position.market_value),
            }
            if outcome.open_position is not None
            else None
        ),
        "coverage": {
            "first_session": outcome.first_session.isoformat(),
            "last_session": outcome.last_session.isoformat(),
            "sessions": outcome.sessions,
            "warmup_sessions": outcome.warmup_sessions,
            "data_first_date": outcome.data_first_date.isoformat(),
            "data_last_date": outcome.data_last_date.isoformat(),
        },
        "assumptions": list(outcome.assumptions),
        "limitations": list(outcome.limitations),
    }


def _point(point) -> dict:
    """Dates and money as strings, for the same reason the snapshot's prices are."""
    return {"on": point.on.isoformat(), "equity": str(point.equity)}


def _performance(performance) -> dict:
    return {
        "final_value": str(performance.final_value),
        "total_return_percent": str(performance.total_return_percent),
        "max_drawdown_percent": str(performance.max_drawdown_percent),
    }


def _to_out(stored: dict) -> BacktestRunOut:
    """A stored row as the API returns it.

    The stored decimal strings are parsed back into `Decimal` by the schema and re-serialised, so
    a run read from the database and the run that was just performed produce the same body.
    Nothing is recomputed: a stored result is a record of what was calculated, and recalculating
    it at read time would show today's arithmetic under yesterday's evidence.
    """
    result = stored.get("result") or {}
    coverage = result.get("coverage")

    return BacktestRunOut(
        run_id=stored["id"],
        request_id=stored["request_id"],
        created_at=stored["created_at"],
        strategy_id=stored["strategy_id"],
        strategy_version=stored["strategy_version"],
        strategy_name=_strategy_name(stored["strategy_id"]),
        symbol=stored["symbol"],
        exchange=stored["exchange"],
        instrument_name=stored["instrument_name"],
        instrument_type=stored["instrument_type"],
        currency=stored["currency"],
        start_date=stored["start_date"],
        end_date=stored["end_date"],
        initial_capital=stored["initial_capital"],
        parameters=dict(stored["parameters"]),
        commission_per_share=stored["commission_per_share"],
        slippage_percent=stored["slippage_percent"],
        provider=stored["provider"],
        adjustment_basis=stored["adjustment_basis"],
        provider_adjust_mode=stored["provider_adjust_mode"],
        data_retrieved_at=stored["data_retrieved_at"],
        coverage=(
            BacktestCoverageOut(
                first_session=coverage["first_session"],
                last_session=coverage["last_session"],
                sessions=coverage["sessions"],
                warmup_sessions=coverage["warmup_sessions"],
                data_first_date=coverage["data_first_date"],
                data_last_date=coverage["data_last_date"],
                data_sessions=stored["data_sessions"],
            )
            if coverage
            else None
        ),
        equity=[BacktestEquityPointOut(**point) for point in result.get("equity", [])],
        benchmark_equity=[
            BacktestEquityPointOut(**point) for point in result.get("benchmark_equity", [])
        ],
        trades=[BacktestTradeOut(**trade) for trade in result.get("trades", [])],
        performance=(
            BacktestPerformanceOut(**result["performance"])
            if result.get("performance")
            else None
        ),
        benchmark=(
            BacktestPerformanceOut(**result["benchmark"]) if result.get("benchmark") else None
        ),
        cash_remaining=result.get("cash_remaining"),
        open_position=(
            BacktestOpenPositionOut(**result["open_position"])
            if result.get("open_position")
            else None
        ),
        assumptions=list(result.get("assumptions", [])),
        limitations=list(result.get("limitations", [])),
        explanation=stored.get("explanation"),
        explanation_model=stored.get("explanation_model"),
        explanation_prompt_version=stored.get("explanation_prompt_version"),
    )


def _strategy_name(strategy_id: str) -> str:
    """The strategy's display name, or its id when this build no longer defines it.

    A stored run whose strategy was removed still reads back -- its name is a fact about the row,
    and refusing to render it because the definition moved on would lose the only record of it.
    """
    strategy = backtest.STRATEGIES_BY_ID.get(strategy_id)
    return strategy.name if strategy is not None else strategy_id


__all__ = ["router"]
