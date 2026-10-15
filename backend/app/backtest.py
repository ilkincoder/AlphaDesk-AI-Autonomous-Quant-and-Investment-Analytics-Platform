"""The backtest engine: a predefined strategy over stored-price bars.

Deterministic arithmetic and nothing else. No model is asked anything, no order is sent
anywhere, and no database session is opened -- `run_backtest` takes bars and a
configuration and returns a result. That is what makes it testable on small fixtures whose
answers can be checked by hand, which is the only way a performance figure means anything.

**Three decisions define every number this module produces, and all three are stated in
the result rather than left for a reader to assume.**

* **A signal executes at the *next* session's open, never on the bar that produced it.**
  A crossover is only knowable once its session has closed; filling it at that session's
  own close would be deciding with a price the strategy could not have traded at. So a
  signal raised at session *i* is executed at session *i+1*'s open, and a signal raised on
  the last session of the test is discarded -- there is no session left to execute it in.

* **The test starts flat, and warm-up history is only there to make the averages
  computable.** Sessions before `start_date` produce indicator values and nothing else:
  they contribute no equity point, no trade and no return. A buy is created by a
  *crossing*, so a fast average that is already above the slow one when the test opens
  produces no position. A crossing on the last warm-up session does produce one, because
  it is a crossing whose execution falls inside the test -- which is a different fact from
  an average that was already above.

* **Sizing is whole shares, and cash is never allowed to go negative.** The quantity is the
  largest whole number of shares whose price *and commission* fit in the cash available,
  and the search for it is a real one rather than a division followed by a subtraction:
  rounding a quotient to the nearest cent can put the total a fraction above the balance,
  so the quantity is stepped down until the arithmetic fits exactly.

**Prices are split-adjusted and not dividend-adjusted.** That is what the provider's
`splits` mode means, and it is why every return here is a *price* return: a shareholder's
dividends are not in these bars and are therefore not in these figures. The result says so
in its own limitations rather than calling the figure a total return.

Money is quantised with `ROUND_HALF_UP`, matching `app.valuation`: the default
`ROUND_HALF_EVEN` sends a half-cent to whichever neighbour is even, which is not a rule
anyone expects a currency to follow. Execution prices carry four decimal places, the scale
`order_intents.estimated_price` already uses.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from app.twelvedata import DailyBar

_CENT = Decimal("0.01")
_PRICE_SCALE = Decimal("0.0001")
# An RSI reading is a point on a 0-100 scale rather than money, but four places is the same
# scale an execution price carries and it makes a reading exactly reproducible.
_RSI_SCALE = Decimal("0.0001")
_WHOLE = Decimal("1")
_HUNDRED = Decimal("100")

# --- what a configuration may say -----------------------------------------------------

# A moving average of one session is the price itself, and an average longer than this is
# a claim about years of history that a daily bar series cannot support usefully. Both
# bounds are enforced on the request as well; they are repeated here so the engine refuses
# a configuration it was handed directly rather than only one that came through HTTP.
MIN_PERIOD = 2
MAX_PERIOD = 400

# The engine's own ceiling on a starting balance, in USD. A backtest is a simulation of an
# account, and an account large enough to move the market it is trading is not one this
# model describes.
MAX_INITIAL_CAPITAL = Decimal("100000000.00")

# USD per share, charged on each side of a trade. Zero is a legitimate answer: a
# commission-free broker is a real commission model, and it is the one the Alpaca paper
# account this application already uses would give.
MIN_COMMISSION_PER_SHARE = Decimal("0")
MAX_COMMISSION_PER_SHARE = Decimal("1.00")

# Percent of the execution price, always charged against the trade: a buy pays more than
# the open and a sell receives less. Zero means "assume the open was available".
MIN_SLIPPAGE_PERCENT = Decimal("0")
MAX_SLIPPAGE_PERCENT = Decimal("5.00")

# The provider's daily history does not reach meaningfully before this, and a start date
# further back than it is a request for bars that do not exist.
EARLIEST_START_DATE = date(1980, 1, 1)

# Sessions of history fetched before the test window, on top of what the slow period
# needs. The indicator requirement is `slow_period + 1` sessions; this covers weekends and
# exchange holidays between the requested date and that many sessions back, so the request
# is not made to fail for a window that begins on a Monday after a long weekend.
_WARMUP_CALENDAR_SLACK_SESSIONS = 12


class BacktestError(Exception):
    """Base class for every failure this module reports.

    One subclass per cause, so a route can map a refused configuration to 422 and unusable
    data to a 503-style "the provider cannot answer this" without reading messages.
    """


class InvalidConfigurationError(BacktestError):
    """The configuration is not one this engine will run. The message names the fix."""


class UnusableDataError(BacktestError):
    """The bars cannot answer the request.

    Either there are no sessions in the test window at all, or there is not enough history
    before it for the chosen periods to have a value. Both are statements about the data,
    not about the configuration, and both are actionable -- a later start date or shorter
    periods.
    """


class CostModelError(BacktestError):
    """The costs make a trade impossible rather than merely expensive.

    A sell whose commission exceeds its own execution price would hand the seller negative
    proceeds, which is a cost model describing something that cannot happen.
    """


# --- the strategy catalogue ------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    """One number a strategy is configured with, and the values it may take.

    Declared by the strategy rather than by the request, the form or the table, so there is
    one definition and three readers: the engine validates against it, the catalogue route
    serves it, and the page renders a field from it. A parameter the page invents and the
    engine does not know is then not a thing that can happen.
    """

    name: str
    # What the number is, unit included, in the words a person reads.
    label: str
    default: int
    minimum: int
    maximum: int


# Reads the signal a session's close raises, or `None`. Built once per run from the closes and
# the configuration, because the indicator series it closes over is computed in a single pass.
SignalReader = Callable[[int], str | None]


@dataclass(frozen=True)
class Strategy:
    """One predefined strategy, as the application defines it.

    Users choose from this list; they do not write a description and they do not write
    code. `version` is stored on every run, because a run's arithmetic is only reproducible
    against the definition that produced it -- changing these rules later must not silently
    change what a stored run means.

    **The arithmetic travels with the description.** `warmup_sessions`, `check_parameters` and
    `signals` are this strategy's own, held as fields rather than looked up again from its id.
    The engine reads them off the object it was handed, so a run cannot be executed by one
    definition and recorded against another.
    """

    id: str
    version: str
    name: str
    summary: str
    # How the signals are produced, in the order they are evaluated.
    rules: tuple[str, ...]
    # When a signal becomes a trade, and what happens to one that cannot.
    execution: tuple[str, ...]
    parameters: tuple[Param, ...]
    # Sessions of history the indicator needs before the test window, for one configuration.
    warmup_sessions: Callable[[Mapping[str, int]], int]
    # A complaint about the parameters *taken together*, or `None`. The bounds each one must
    # sit inside are its `Param`; this is for the relations between them, which no single
    # bound can express.
    check_parameters: Callable[[Mapping[str, int]], str | None]
    # The signal reader for one run: the closes it reads, and the configuration it reads
    # them under.
    signals: Callable[[Sequence[Decimal], Mapping[str, int]], SignalReader]


def _crossover_warmup(parameters: Mapping[str, int]) -> int:
    """Sessions of history the two averages need before the test window.

    `slow_period + 1`, and the extra one is load-bearing. A crossover at session *i* is
    decided by comparing session *i* with session *i-1*, so evaluating the last warm-up
    session -- whose execution falls on the test's first session -- needs a slow average at
    the session before it too.
    """
    return parameters["slow_period"] + 1


def _crossover_check(parameters: Mapping[str, int]) -> str | None:
    if parameters["fast_period"] >= parameters["slow_period"]:
        return (
            f"the fast period ({parameters['fast_period']}) must be shorter than the slow "
            f"period ({parameters['slow_period']}), or the averages would cross on every "
            "session"
        )
    return None


def _crossover_signals(
    closes: Sequence[Decimal], parameters: Mapping[str, int]
) -> SignalReader:
    """A crossover read at each session's close, or `None` where there is not one.

    A crossover is a *change* of sign between consecutive sessions' differences, which is why
    the previous session is needed and why the first session of the series can never produce
    one. The inclusive comparisons on the previous difference are deliberate: an average that
    came down to exactly the other and then moved above it has crossed, and requiring a strict
    sign on both sides would silently drop that case.
    """
    fast = _simple_moving_average(list(closes), parameters["fast_period"])
    slow = _simple_moving_average(list(closes), parameters["slow_period"])

    def signal_at(index: int) -> str | None:
        if index < 1:
            return None

        here, before = fast[index], fast[index - 1]
        slow_here, slow_before = slow[index], slow[index - 1]
        if None in (here, before, slow_here, slow_before):
            return None

        was = before - slow_before
        now = here - slow_here
        if was <= 0 and now > 0:
            return "buy"
        if was >= 0 and now < 0:
            return "sell"
        return None

    return signal_at


MOVING_AVERAGE_CROSSOVER = Strategy(
    id="moving_average_crossover",
    version="1",
    name="Moving Average Crossover",
    summary=(
        "Buys when a fast simple moving average crosses above a slow one, and sells when "
        "it crosses back below. A trend-following strategy: it aims to be holding while a "
        "rise persists and to be flat while it does not."
    ),
    rules=(
        "Both averages are simple moving averages of daily closing prices.",
        "A bullish crossover (fast crossing above slow) is a buy signal while flat.",
        "A bearish crossover (fast crossing below slow) is a sell signal while holding.",
        "A crossover in the direction already held changes nothing: the strategy is "
        "either flat or fully invested, never partially.",
        "The test starts flat. An average that was already above the other when the test "
        "opened is not a crossover and does not create a position.",
    ),
    execution=(
        "A signal executes at the next session's open, never at the close that produced it.",
        "A buy spends the largest whole number of shares that fits in the cash available "
        "after commission, and keeps the remainder in cash.",
        "A sell closes the whole position.",
        "A signal raised on the final session is discarded: there is no later session "
        "inside the test to execute it in.",
        "A final position still open at the end is marked to the last close, not sold.",
    ),
    parameters=(
        Param(
            name="fast_period",
            label="Fast period (sessions)",
            default=20,
            minimum=MIN_PERIOD,
            maximum=MAX_PERIOD,
        ),
        Param(
            name="slow_period",
            label="Slow period (sessions)",
            default=50,
            minimum=MIN_PERIOD,
            maximum=MAX_PERIOD,
        ),
    ),
    warmup_sessions=_crossover_warmup,
    check_parameters=_crossover_check,
    signals=_crossover_signals,
)


def _rsi_warmup(parameters: Mapping[str, int]) -> int:
    """Sessions of history Wilder's RSI needs before the test window.

    `period + 1` closes give the first average gain and the first average loss, and the extra
    session is the same one the crossover needs: a signal is a *crossing*, so the session
    before the test's first must have a reading too, or the first session could not be
    compared with anything.
    """
    return parameters["period"] + 1


def _rsi_check(parameters: Mapping[str, int]) -> str | None:
    if parameters["entry_threshold"] >= parameters["exit_threshold"]:
        return (
            f"the entry threshold ({parameters['entry_threshold']}) must be below the exit "
            f"threshold ({parameters['exit_threshold']}), or a position would be sold on the "
            "same reading that bought it"
        )
    return None


def _rsi_signals(closes: Sequence[Decimal], parameters: Mapping[str, int]) -> SignalReader:
    """An oversold/overbought crossing read at each session's close.

    Both signals are *crossings* rather than levels, and that is the whole strategy. Buying
    merely because the RSI is below the entry threshold would buy again on every session of a
    decline; the entry is the session the RSI *falls through* the threshold, which is the
    first session of the condition rather than one somewhere inside it.
    """
    period = parameters["period"]
    entry = parameters["entry_threshold"]
    exit_level = parameters["exit_threshold"]
    values = _wilder_rsi(list(closes), period)

    def signal_at(index: int) -> str | None:
        if index < 1:
            return None

        here, before = values[index], values[index - 1]
        if here is None or before is None:
            return None

        # Buy while flat, on falling through the entry threshold from at or above it.
        if before >= entry and here < entry:
            return "buy"
        # Sell while holding, on rising through the exit threshold from at or below it.
        if before <= exit_level and here > exit_level:
            return "sell"
        return None

    return signal_at


RSI_REVERSION = Strategy(
    id="rsi_reversion",
    version="1",
    name="RSI Reversion",
    summary=(
        "Buys when Wilder's Relative Strength Index falls through an oversold threshold, and "
        "sells when it rises back through an overbought one. A mean-reversion strategy: it "
        "aims to buy weakness and sell strength, which is the opposite bet to a trend follower."
    ),
    rules=(
        "The indicator is Wilder's RSI over the configured period: an average gain and an "
        "average loss, each smoothed from the one before rather than recomputed over a "
        "window, so every session since the series began carries some weight.",
        "A buy signal while flat is the RSI falling through the entry threshold -- at or "
        "above it on the previous session and below it on this one. Being below it is not "
        "enough on its own, or a decline would buy on every session of its way down.",
        "A sell signal while holding is the RSI rising through the exit threshold, on the "
        "same rule read the other way.",
        "A reading that has already reached a level changes nothing: the strategy is either "
        "flat or fully invested, never partially.",
        "The test starts flat. An RSI already below the entry threshold when the test opened "
        "is not a crossing and does not create a position.",
        "A series that did not move has no average gain and no average loss, so its RSI is "
        "read as 50 -- neither overbought nor oversold -- rather than as the 100 that a "
        "no-loss rule would give a series which never rose.",
    ),
    execution=(
        "A signal executes at the next session's open, never at the close that produced it.",
        "A buy spends the largest whole number of shares that fits in the cash available "
        "after commission, and keeps the remainder in cash.",
        "A sell closes the whole position.",
        "A signal raised on the final session is discarded: there is no later session "
        "inside the test to execute it in.",
        "A final position still open at the end is marked to the last close, not sold.",
    ),
    parameters=(
        Param(
            name="period",
            label="RSI period (sessions)",
            default=14,
            minimum=MIN_PERIOD,
            maximum=MAX_PERIOD,
        ),
        Param(
            name="entry_threshold",
            label="Entry threshold (buy below this RSI)",
            default=30,
            minimum=1,
            maximum=99,
        ),
        Param(
            name="exit_threshold",
            label="Exit threshold (sell above this RSI)",
            default=70,
            minimum=1,
            maximum=99,
        ),
    ),
    warmup_sessions=_rsi_warmup,
    check_parameters=_rsi_check,
    signals=_rsi_signals,
)

# Every strategy this application defines. The routes and the page read this rather than
# hard-coding a name, and each entry carries its own parameters and its own arithmetic.
STRATEGIES: tuple[Strategy, ...] = (MOVING_AVERAGE_CROSSOVER, RSI_REVERSION)

STRATEGIES_BY_ID: dict[str, Strategy] = {item.id: item for item in STRATEGIES}


def warmup_lookback_days(sessions: int) -> int:
    """Calendar days to reach back for, to obtain `sessions` trading sessions.

    A week holds five sessions in seven days, and the extra allowance covers the exchange
    holidays that a pure five-in-seven estimate would miss. Asking for fewer bars than are
    needed is the failure this avoids; asking for more costs nothing, because the warm-up
    is trimmed to what the indicator actually needs.
    """
    needed = sessions + _WARMUP_CALENDAR_SLACK_SESSIONS
    return (needed * 7 + 4) // 5


# --- what a run is asked for -----------------------------------------------------------

@dataclass(frozen=True)
class CostModel:
    """What a trade costs, in the units the request states them in.

    `commission_per_share` is USD per share per side. Per share rather than per trade
    because a percentage or a flat fee would both need a minimum to be meaningful, and
    both would make the affordable quantity a smaller problem than it looks.

    `slippage_percent` is a percentage of the execution price, always adverse.
    """

    commission_per_share: Decimal = Decimal("0.005")
    slippage_percent: Decimal = Decimal("0.05")

    def buy_price(self, open_price: Decimal) -> Decimal:
        """What a buy actually pays at a session that opened at `open_price`."""
        return _price(open_price * (1 + self.slippage_percent / _HUNDRED))

    def sell_price(self, open_price: Decimal) -> Decimal:
        """What a sell actually receives. The same slippage, in the other direction."""
        return _price(open_price * (1 - self.slippage_percent / _HUNDRED))

    def commission(self, quantity: int) -> Decimal:
        return _money(self.commission_per_share * quantity)


@dataclass(frozen=True)
class BacktestConfig:
    """One run, exactly as it was asked for.

    Carries the strategy *object* rather than its id: the engine must know which rules it
    is executing, and looking the id up again inside would let a run be recorded against a
    definition other than the one that produced it.

    `parameters` are that strategy's own -- `fast_period`/`slow_period` for the crossover,
    `period`/`entry_threshold`/`exit_threshold` for RSI. The engine never reads a parameter
    by name; the strategy's own functions do, which is what lets one engine run both.
    """

    strategy: Strategy
    symbol: str
    start_date: date
    end_date: date
    initial_capital: Decimal
    parameters: Mapping[str, int]
    costs: CostModel = CostModel()


def validate_configuration(config: BacktestConfig, *, today: date | None = None) -> None:
    """Refuse a configuration this engine will not run, naming what to change.

    Called by the route before any provider request, so an invalid request costs no
    credit, and called again by `run_backtest`, so the engine cannot be handed one by a
    caller that skipped the route.

    The parameters are checked against the strategy's own `Param` declarations, so a strategy
    that gains a parameter is validated here without this function being touched -- and a
    request that omits one, or sends one the strategy does not declare, is refused rather
    than silently defaulted.
    """
    declared = {param.name: param for param in config.strategy.parameters}
    unknown = sorted(set(config.parameters) - set(declared))
    if unknown:
        raise InvalidConfigurationError(
            f"{config.strategy.name} has no parameter called {', '.join(unknown)}. It takes: "
            f"{', '.join(declared)}."
        )

    for param in config.strategy.parameters:
        value = config.parameters.get(param.name)
        if value is None:
            raise InvalidConfigurationError(
                f"the {param.label.lower()} is required, and {param.default} is the default "
                "this strategy suggests"
            )
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidConfigurationError(
                f"the {param.label.lower()} must be a whole number, got {value!r}"
            )
        if not param.minimum <= value <= param.maximum:
            raise InvalidConfigurationError(
                f"the {param.label.lower()} must be between {param.minimum} and "
                f"{param.maximum}, got {value}"
            )

    complaint = config.strategy.check_parameters(config.parameters)
    if complaint is not None:
        raise InvalidConfigurationError(complaint)

    if config.start_date >= config.end_date:
        raise InvalidConfigurationError(
            f"the start date ({config.start_date}) must be before the end date "
            f"({config.end_date})"
        )

    if config.start_date < EARLIEST_START_DATE:
        raise InvalidConfigurationError(
            f"the start date must not be before {EARLIEST_START_DATE}, which is older than "
            "the daily history this application can obtain"
        )

    if today is not None and config.end_date > today:
        raise InvalidConfigurationError(
            f"the end date ({config.end_date}) is in the future. A backtest is a "
            "simulation over sessions that have happened."
        )

    if config.initial_capital <= 0:
        raise InvalidConfigurationError(
            f"the initial capital must be greater than zero, got {config.initial_capital}"
        )
    if config.initial_capital > MAX_INITIAL_CAPITAL:
        raise InvalidConfigurationError(
            f"the initial capital must be at most {MAX_INITIAL_CAPITAL}, got "
            f"{config.initial_capital}"
        )

    commission = config.costs.commission_per_share
    if not MIN_COMMISSION_PER_SHARE <= commission <= MAX_COMMISSION_PER_SHARE:
        raise InvalidConfigurationError(
            f"the commission must be between {MIN_COMMISSION_PER_SHARE} and "
            f"{MAX_COMMISSION_PER_SHARE} USD per share, got {commission}"
        )

    slippage = config.costs.slippage_percent
    if not MIN_SLIPPAGE_PERCENT <= slippage <= MAX_SLIPPAGE_PERCENT:
        raise InvalidConfigurationError(
            f"the slippage must be between {MIN_SLIPPAGE_PERCENT} and "
            f"{MAX_SLIPPAGE_PERCENT} percent, got {slippage}"
        )


# --- what a run produced ---------------------------------------------------------------

@dataclass(frozen=True)
class Trade:
    """One executed trade, as the ledger reports it.

    `price` is the price that was actually paid or received -- the session's open moved by
    the slippage model -- and `gross_value` is `quantity * price`. `cash_after` is the
    balance once the trade and its commission have both settled, so the ledger and the
    equity curve cannot disagree about what the account held.
    """

    executed_on: date
    # "buy" or "sell". Never inferred from a sign: a quantity the reader has to interpret
    # is a quantity that can be misread.
    side: str
    quantity: int
    price: Decimal
    gross_value: Decimal
    commission: Decimal
    cash_after: Decimal
    # Which session's close produced the signal this executed.
    signalled_on: date


@dataclass(frozen=True)
class EquityPoint:
    """The account's value on one date, marked at that session's close.

    The first point of every curve is the initial capital, dated the requested start date:
    the series has to begin at the money that was put in, or the first return it reports
    would be measured from the first trade rather than from the start.
    """

    on: date
    equity: Decimal


@dataclass(frozen=True)
class OpenPosition:
    """A position still held when the test ended.

    Reported rather than closed. Selling it would invent a trade nobody made, at a price
    the strategy did not choose; marking it to the last close is what the account is
    actually worth on the last day of the test.
    """

    quantity: int
    entry_date: date
    entry_price: Decimal
    mark_date: date
    mark_price: Decimal
    market_value: Decimal


@dataclass(frozen=True)
class Performance:
    """The three figures a curve is read for. Money in USD, percentages in percent."""

    final_value: Decimal
    total_return_percent: Decimal
    # The largest peak-to-trough fall on the curve, as a positive magnitude. Zero means the
    # account was never worth less than its own running maximum. Reported unsigned because
    # a drawdown is already a fall; a minus sign in front of it would be a second one.
    max_drawdown_percent: Decimal


@dataclass(frozen=True)
class BacktestOutcome:
    """Everything one run produced, ready to be stored and returned."""

    equity: tuple[EquityPoint, ...]
    benchmark_equity: tuple[EquityPoint, ...]
    trades: tuple[Trade, ...]
    performance: Performance
    benchmark: Performance
    cash_remaining: Decimal
    open_position: OpenPosition | None
    # The sessions the test actually covered, which is what the metrics were computed over.
    first_session: date
    last_session: date
    sessions: int
    # How much history was fetched and consumed before the window, and what it spanned.
    warmup_sessions: int
    data_first_date: date
    data_last_date: date
    # Prose the page renders verbatim, so the explanation of a figure cannot drift from the
    # arithmetic that produced it.
    assumptions: tuple[str, ...]
    limitations: tuple[str, ...]


# --- the engine ------------------------------------------------------------------------

def run_backtest(bars: tuple[DailyBar, ...] | list[DailyBar], config: BacktestConfig) -> BacktestOutcome:
    """Run the configured strategy over `bars`, and buy-and-hold over the same bars.

    `bars` is oldest-first and may -- must -- begin before `config.start_date`; the sessions
    before it are warm-up. Anything outside `[start_date, end_date]` contributes an
    indicator value or nothing at all, never a reported figure.

    Raises `InvalidConfigurationError` for a configuration this engine refuses and
    `UnusableDataError` when the bars cannot answer the request. See the module docstring
    for the three rules the arithmetic follows.
    """
    validate_configuration(config)

    ordered = sorted(bars, key=lambda bar: bar.trading_date)
    sessions = [index for index, bar in enumerate(ordered)
                if config.start_date <= bar.trading_date <= config.end_date]
    if not sessions:
        raise UnusableDataError(
            f"No trading sessions fall between {config.start_date} and {config.end_date} "
            f"in the data available for {config.symbol}. Widen the dates, or check that "
            "the ticker traded over that period."
        )

    first = sessions[0]
    required = config.strategy.warmup_sessions(config.parameters)
    if first < required:
        raise UnusableDataError(
            f"The data for {config.symbol} begins on {ordered[0].trading_date}, which gives "
            f"{first} session(s) before {config.start_date}. {config.strategy.name} over "
            f"these settings needs {required} sessions of history before the test starts, so "
            "its indicator would not exist yet at the first session. Start the test later, or "
            "shorten the periods."
        )

    closes = [bar.close for bar in ordered]
    signal_at = config.strategy.signals(closes, config.parameters)

    strategy_run = _simulate(ordered, sessions, first, config, signal_at)
    benchmark_run = _buy_and_hold(ordered, sessions, config)

    performance = _performance(strategy_run.equity, config.initial_capital)
    benchmark = _performance(benchmark_run.equity, config.initial_capital)

    return BacktestOutcome(
        equity=strategy_run.equity,
        benchmark_equity=benchmark_run.equity,
        trades=strategy_run.trades,
        performance=performance,
        benchmark=benchmark,
        cash_remaining=strategy_run.cash,
        open_position=strategy_run.open_position,
        first_session=ordered[first].trading_date,
        last_session=ordered[sessions[-1]].trading_date,
        sessions=len(sessions),
        warmup_sessions=first,
        data_first_date=ordered[0].trading_date,
        data_last_date=ordered[-1].trading_date,
        assumptions=_assumptions(config, benchmark_run.trades),
        limitations=_limitations(config),
    )


@dataclass
class _Run:
    """A curve, a ledger, and where the account ended up. Internal to this module."""

    equity: tuple[EquityPoint, ...]
    trades: tuple[Trade, ...]
    cash: Decimal
    open_position: OpenPosition | None


def _simulate(
    bars: list[DailyBar],
    sessions: list[int],
    first: int,
    config: BacktestConfig,
    signal_at: SignalReader,
) -> _Run:
    """Walk the test window one session at a time, executing yesterday's signal at today's open.

    The order inside the loop is the whole strategy and cannot be rearranged: a signal is
    executed at this session's open, the account is then marked at this session's close, and
    only then is *this* session's close compared with the previous one to raise tomorrow's
    signal. Marking before signalling is what keeps a session's own close out of the
    decision it produced.

    Nothing here knows which strategy it is running. `signal_at` is the strategy's own reader
    of a session's close, so the crossing rule -- whatever it is -- stays with the strategy
    that defines it, and this loop is only the execution and the accounting.
    """
    cash = config.initial_capital
    quantity = 0
    entry: Trade | None = None
    trades: list[Trade] = []
    equity = [EquityPoint(config.start_date, config.initial_capital)]

    # A crossing on the last warm-up session is a signal like any other: it happened before
    # the test opened, but its execution -- the test's first open -- is inside it. This is
    # not the same thing as an indicator that was already past its threshold: that creates
    # no signal at all, which is what leaves the account flat.
    pending = signal_at(first - 1)

    for index in sessions:
        bar = bars[index]
        signalled_on = bars[index - 1].trading_date

        if pending == "buy" and quantity == 0:
            fill = config.costs.buy_price(bar.open)
            bought = _affordable_quantity(cash, fill, config.costs)
            if bought > 0:
                gross = _money(fill * bought)
                fee = config.costs.commission(bought)
                cash = cash - gross - fee
                trade = Trade(
                    executed_on=bar.trading_date,
                    side="buy",
                    quantity=bought,
                    price=fill,
                    gross_value=gross,
                    commission=fee,
                    cash_after=cash,
                    signalled_on=signalled_on,
                )
                trades.append(trade)
                quantity = bought
                entry = trade
        elif pending == "sell" and quantity > 0:
            fill = config.costs.sell_price(bar.open)
            fee = config.costs.commission(quantity)
            gross = _money(fill * quantity)
            if gross - fee < 0:
                raise CostModelError(
                    f"The commission of {config.costs.commission_per_share} USD per share "
                    f"exceeds the {fill} USD execution price on {bar.trading_date}. A sale "
                    "would return less than nothing, which is a cost model rather than a "
                    "trade. Lower the commission, or run this instrument over different "
                    "dates."
                )
            cash = cash + gross - fee
            trades.append(
                Trade(
                    executed_on=bar.trading_date,
                    side="sell",
                    quantity=quantity,
                    price=fill,
                    gross_value=gross,
                    commission=fee,
                    cash_after=cash,
                    signalled_on=signalled_on,
                )
            )
            quantity = 0
            entry = None

        # A signal is consumed whether or not it produced a trade. A buy that could not
        # afford a single share is not retried tomorrow: a crossover is an event, and the
        # strategy holding no view afterwards is the honest reading of one it could not act on.
        pending = None

        equity.append(EquityPoint(bar.trading_date, _money(cash + quantity * bar.close)))

        pending = signal_at(index)

    # The last session's signal was assigned and never executed: there is no later session
    # in the test window for it, so it is discarded by the loop simply ending.
    open_position = None
    if quantity > 0 and entry is not None:
        mark = bars[sessions[-1]]
        open_position = OpenPosition(
            quantity=quantity,
            entry_date=entry.executed_on,
            entry_price=entry.price,
            mark_date=mark.trading_date,
            mark_price=_price(mark.close),
            market_value=_money(quantity * mark.close),
        )

    return _Run(equity=tuple(equity), trades=tuple(trades), cash=_money(cash),
                open_position=open_position)


def _buy_and_hold(bars: list[DailyBar], sessions: list[int], config: BacktestConfig) -> _Run:
    """The benchmark: the same instrument, capital, window, sizing and costs, bought once.

    Bought at the first eligible session's open -- the same instant the strategy's first
    possible execution happens, so the two curves start from the same opportunity -- and
    then held, marked to each close, with the unspent remainder sitting in cash. It is not
    sold at the end either: forcing a sale here would apply a cost to the benchmark that the
    strategy is not charged for when it finishes holding.
    """
    first_bar = bars[sessions[0]]
    fill = config.costs.buy_price(first_bar.open)
    quantity = _affordable_quantity(config.initial_capital, fill, config.costs)
    fee = config.costs.commission(quantity)
    gross = _money(fill * quantity)
    cash = config.initial_capital - gross - fee

    trades: list[Trade] = []
    if quantity > 0:
        trades.append(
            Trade(
                executed_on=first_bar.trading_date,
                side="buy",
                quantity=quantity,
                price=fill,
                gross_value=gross,
                commission=fee,
                cash_after=_money(cash),
                signalled_on=first_bar.trading_date,
            )
        )

    equity = [EquityPoint(config.start_date, config.initial_capital)]
    for index in sessions:
        bar = bars[index]
        equity.append(EquityPoint(bar.trading_date, _money(cash + quantity * bar.close)))

    open_position = None
    if quantity > 0:
        last = bars[sessions[-1]]
        open_position = OpenPosition(
            quantity=quantity,
            entry_date=first_bar.trading_date,
            entry_price=fill,
            mark_date=last.trading_date,
            mark_price=_price(last.close),
            market_value=_money(quantity * last.close),
        )

    return _Run(equity=tuple(equity), trades=tuple(trades), cash=_money(cash),
                open_position=open_position)


# --- indicators ------------------------------------------------------------------------

def _simple_moving_average(
    closes: list[Decimal], period: int
) -> list[Decimal | None]:
    """The average of the last `period` closes at each session, `None` before there are enough.

    `None` rather than a shorter average: a "20-session average" computed from nine sessions
    is not a 20-session average, and letting one exist would put a signal in the first nine
    sessions of the series that the strategy could never have had. The running sum is carried
    rather than re-added, so the whole series costs one pass instead of one pass per session.
    """
    averages: list[Decimal | None] = []
    running = Decimal("0")
    for index, close in enumerate(closes):
        running += close
        if index >= period:
            running -= closes[index - period]
        averages.append(running / period if index >= period - 1 else None)
    return averages


def _wilder_rsi(closes: list[Decimal], period: int) -> list[Decimal | None]:
    """Wilder's Relative Strength Index at each session, `None` before it can be computed.

    The first value sits at index `period`: it takes `period` price *changes*, and a change
    needs two closes, so `period + 1` closes are the earliest a reading exists. Every value
    after it is smoothed rather than recomputed over a window --

        average = (previous_average * (period - 1) + this_change) / period

    -- which is Wilder's own definition and the reason a long series never forgets its past.
    An RSI computed as a plain moving average of gains and losses over the last `period`
    sessions gives different numbers, and different signals, which is why the smoothing is
    written out here rather than reusing `_simple_moving_average`.

    Three degenerate readings are defined rather than left to a division by zero:

    * **No losses**, so the average loss is zero and the ratio is undefined: 100, which is
      what "nothing fell" means on this scale.
    * **No gains**: 0, the same fact the other way round.
    * **Neither gains nor losses** -- a series that did not move at all: 50. The no-loss rule
      would report 100, calling a flat line extremely overbought, which is a claim about
      momentum where there is none. 50 is the honest reading, and it is why a flat series
      produces no signal on either threshold default.
    """
    values: list[Decimal | None] = [None] * len(closes)
    if period < 1 or len(closes) <= period:
        return values

    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for index in range(1, period + 1):
        change = closes[index] - closes[index - 1]
        gains.append(change if change > 0 else Decimal("0"))
        losses.append(-change if change < 0 else Decimal("0"))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    values[period] = _rsi_from(average_gain, average_loss)

    divisor = Decimal(period)
    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        gain = change if change > 0 else Decimal("0")
        loss = -change if change < 0 else Decimal("0")
        average_gain = (average_gain * (divisor - 1) + gain) / divisor
        average_loss = (average_loss * (divisor - 1) + loss) / divisor
        values[index] = _rsi_from(average_gain, average_loss)

    return values


def _rsi_from(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    """One reading from a pair of averages, with the degenerate cases named.

    Quantised to four decimal places, unlike the averages the crossover compares. Those are
    only ever subtracted from each other, so a trailing digit cancels; an RSI is compared with
    a *threshold*, and a reading that is 24.9999999999999998 rather than 25 puts a signal on
    the wrong side of it. Rounding also makes a reading a number two runs agree on, which is
    what a stored run's signals have to be reproducible from.
    """
    if average_loss == 0:
        return Decimal("50") if average_gain == 0 else _HUNDRED
    if average_gain == 0:
        return Decimal("0")
    strength = average_gain / average_loss
    return _rsi_scaled(_HUNDRED - (_HUNDRED / (Decimal("1") + strength)))


def _rsi_scaled(value: Decimal) -> Decimal:
    return value.quantize(_RSI_SCALE, rounding=ROUND_HALF_UP)


# --- money -----------------------------------------------------------------------------

def _affordable_quantity(cash: Decimal, price: Decimal, costs: CostModel) -> int:
    """The largest whole number of shares whose price *and commission* fit in `cash`.

    The division gives an upper bound, and the loop is what makes it a bound rather than the
    answer: `qty * price` and the commission are each rounded to the cent before they are
    added, so the rounded total can sit a fraction above the exact one the division
    compared against. Stepping down until the rounded arithmetic fits is what guarantees the
    balance never goes negative -- which is the invariant that matters, because a negative
    cash balance is a trade that could not have happened.
    """
    if price <= 0 or cash <= 0:
        return 0

    per_share = price + costs.commission_per_share
    if per_share <= 0:
        return 0

    quantity = int((cash / per_share).to_integral_value(rounding=ROUND_FLOOR))
    while quantity > 0:
        if _money(price * quantity) + costs.commission(quantity) <= cash:
            return quantity
        quantity -= 1
    return 0


def _money(value: Decimal) -> Decimal:
    """A currency amount: two decimal places, halves away from zero."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _price(value: Decimal) -> Decimal:
    """An execution price: four decimal places, halves away from zero."""
    return value.quantize(_PRICE_SCALE, rounding=ROUND_HALF_UP)


def _percent(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


# --- metrics ---------------------------------------------------------------------------

def _performance(equity: tuple[EquityPoint, ...], initial_capital: Decimal) -> Performance:
    """Final value, total return and maximum drawdown, read off one curve.

    The curve's own first point is the initial capital, so the return is measured from the
    money that was put in rather than from the first trade, and the drawdown can be taken
    from the start -- an account that only ever fell is a drawdown, and a peak that ignored
    the opening balance would report none.
    """
    final_value = equity[-1].equity
    total_return = _percent((final_value - initial_capital) / initial_capital * _HUNDRED)

    peak = equity[0].equity
    worst = Decimal("0")
    for point in equity:
        peak = max(peak, point.equity)
        if peak > 0:
            fall = (peak - point.equity) / peak * _HUNDRED
            worst = max(worst, fall)

    return Performance(
        final_value=final_value,
        total_return_percent=total_return,
        max_drawdown_percent=_percent(worst),
    )


# --- prose the result carries ----------------------------------------------------------

def _assumptions(config: BacktestConfig, benchmark_trades: list[Trade]) -> tuple[str, ...]:
    """What was assumed, stated beside the figures rather than left to the reader.

    Written from the configuration that actually ran, so the sentence and the arithmetic
    cannot disagree -- a page that hard-codes "0.005 USD per share" would keep saying it
    after the default changed.
    """
    lines = [
        f"Signals execute at the next session's open, never at the close that produced them.",
        f"Commission is {config.costs.commission_per_share} USD per share, charged on each "
        "side of a trade. The engine's own default is 0.005.",
        f"Slippage is {config.costs.slippage_percent}% of the execution price, always "
        "against the trade: a buy pays more and a sell receives less. The engine's own "
        "default is 0.05%.",
        "Positions are whole shares, bought with the cash available and no leverage. The "
        "remainder stays in cash and earns nothing.",
        f"The buy-and-hold benchmark uses {config.symbol}, {config.initial_capital} USD, "
        "the same dates, the same whole-share sizing and the same costs, bought at the "
        "first session's open and marked to the last close.",
    ]
    if not benchmark_trades:
        lines.append(
            f"The benchmark could not buy a single share of {config.symbol} with "
            f"{config.initial_capital} USD at the first session's open, so it holds cash "
            "for the whole test."
        )
    return tuple(lines)


def _limitations(config: BacktestConfig) -> tuple[str, ...]:
    """What these figures are not, which is the part a performance chart is read without."""
    return (
        "Prices are split-adjusted and not dividend-adjusted, so every figure here is a "
        "price return. Dividends a shareholder would have received are not counted, and a "
        "high-yield stock will therefore look worse than an investor's actual experience.",
        f"One instrument ({config.symbol}), long only, fully invested or flat. No short "
        "selling, no leverage, no position sizing, and nothing else in the account trades.",
        "Whole-share sizing leaves a cash remainder, so a small account tracks the signal "
        "less closely than a large one: the unspent balance does not participate in a rise.",
        "The signal is evaluated on closes and executed at the next open. A session that "
        "gapped between those two prices is real, and the model trades at the open it "
        "actually saw.",
        "This is a simulation over past prices. It places no orders, it says nothing about "
        "what the strategy would do next, and past performance here is not a forecast.",
    )


__all__ = [
    "BacktestConfig",
    "BacktestError",
    "BacktestOutcome",
    "CostModel",
    "CostModelError",
    "EARLIEST_START_DATE",
    "EquityPoint",
    "InvalidConfigurationError",
    "MAX_COMMISSION_PER_SHARE",
    "MAX_INITIAL_CAPITAL",
    "MAX_PERIOD",
    "MAX_SLIPPAGE_PERCENT",
    "MIN_PERIOD",
    "MOVING_AVERAGE_CROSSOVER",
    "OpenPosition",
    "Param",
    "Performance",
    "RSI_REVERSION",
    "STRATEGIES",
    "STRATEGIES_BY_ID",
    "SignalReader",
    "Strategy",
    "Trade",
    "UnusableDataError",
    "run_backtest",
    "validate_configuration",
    "warmup_lookback_days",
]
