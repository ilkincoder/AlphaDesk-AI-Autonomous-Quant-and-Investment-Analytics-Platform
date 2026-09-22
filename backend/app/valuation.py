"""Portfolio valuation: the demo price table and the calculation that uses it.

Deliberately free of database and HTTP imports. `calculate_valuation` takes plain values
in and returns plain values out, so a future AI tool can reuse it with data it already
holds, and it can be tested without a running PostgreSQL.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

DEMO_PRICE_SOURCE = "demo"

# Fictional valuation prices for the demo portfolio. These are NOT live market prices and
# NOT the purchase prices stored on the holdings -- they exist only so the demo has
# something to value against.
#
# Each is built from a *string*: Decimal("200.00") is exactly 200.00, whereas
# Decimal(200.00) would take a binary float as input and inherit that float's error first.
DEMO_PRICES: dict[str, Decimal] = {
    "AAPL": Decimal("200.00"),
    "MSFT": Decimal("450.00"),
    "NVDA": Decimal("150.00"),
}

_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")


class MissingPriceError(Exception):
    """A held symbol has no price, so no honest total can be produced.

    Raised for the whole calculation rather than skipping the symbol: a total that
    quietly omits a holding is more misleading than no total at all.
    """

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        super().__init__(f"no price for held symbol(s): {', '.join(symbols)}")


class HoldingNotFoundError(Exception):
    """A scenario named a symbol this portfolio does not hold.

    Distinct from MissingPriceError: one is "I cannot price what you hold", the other is
    "you do not hold that at all". Reporting the first when the second is true would send
    someone looking for a price they never needed.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(f"portfolio does not hold {symbol!r}")


class PricedHolding(Protocol):
    """The only two things `calculate_valuation` needs from a holding.

    Structural typing: a SQLAlchemy `Holding` satisfies this without importing anything
    from here, and a test can pass a three-line stub.
    """

    symbol: str
    quantity: Decimal


@dataclass(frozen=True)
class HoldingValuation:
    symbol: str
    quantity: Decimal
    price: Decimal
    holding_value: Decimal
    # None, not 0, when the portfolio's total value is zero: with nothing to divide by,
    # the share is undefined rather than zero.
    allocation_percent: Decimal | None


@dataclass(frozen=True)
class Valuation:
    cash_balance: Decimal
    holdings_value: Decimal
    total_value: Decimal
    cash_allocation_percent: Decimal | None
    holdings: list[HoldingValuation]


@dataclass(frozen=True)
class Scenario:
    """One holding re-priced, and what that did to the portfolio.

    Every `*_before` is the portfolio as it stands; every `*_after` is the same portfolio
    with exactly one price changed.
    """

    symbol: str
    # The fraction applied, not the percentage: this function is not told about percent.
    price_change: Decimal
    price_before: Decimal
    price_after: Decimal
    holding_value_before: Decimal
    holding_value_after: Decimal
    total_value_before: Decimal
    total_value_after: Decimal
    change_value: Decimal
    change_percent: Decimal | None


def _round_2dp(value: Decimal) -> Decimal:
    """Round to two decimal places, halves away from zero.

    ROUND_HALF_UP explicitly, not Python's default ROUND_HALF_EVEN: the default sends
    exactly 0.005 to 0.00, which is not what someone reading a money value expects.
    """
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _percent_of(part: Decimal, whole: Decimal) -> Decimal | None:
    """`part` as a percentage of `whole`, or None when `whole` is zero.

    Used for both a holding's share of a portfolio and a scenario's change against the
    portfolio it changed — the arithmetic is the same in both cases.

    None rather than 0: with nothing to divide by, the share is undefined, not zero.
    """
    if whole == 0:
        return None
    return _round_2dp(part / whole * _HUNDRED)


def calculate_valuation(
    holdings: Iterable[PricedHolding],
    cash_balance: Decimal,
    prices: Mapping[str, Decimal],
    *,
    reported_total_value: Decimal | None = None,
) -> Valuation:
    """Value `holdings` and `cash_balance` against `prices`.

        holding_value = quantity * price
        holdings_value = sum of holding values
        total_value = holdings_value + cash_balance
        allocation_percent = holding_value / total_value * 100
        cash_allocation_percent = cash_balance / total_value * 100

    Pure: no database, no network, no writes. Every figure is computed at full Decimal
    precision and rounded only on the way out, so the result does not depend on where an
    intermediate rounding happened to be applied. Two visible consequences:

    * the returned holdings may not sum to the returned `holdings_value` to the cent,
      because that total was rounded once from the unrounded values;
    * the rounded percentages may not sum to exactly 100.

    Neither is an error. Both are what "round only the output" costs, and the alternative
    -- rounding every step -- accumulates error instead.

    `reported_total_value` is for a portfolio whose total is already a fact rather than an
    arithmetic result: a broker's own equity figure. When it is given it *is* the total,
    and it is what the allocation percentages are a share of, so the parts and the whole
    stay in one frame. It is not a rounding of the computed total and not a correction to
    it -- a broker's account and positions are two moments, and equity need not equal
    positions plus cash to the cent. None (the default) computes the total as above.

    Raises MissingPriceError, naming every affected symbol, if any held symbol is absent
    from `prices`. `average_buy_price` is never consulted as a substitute: a purchase
    price is not a valuation price.
    """
    # Sorted here rather than trusting the caller's order, so the output is stable for
    # every caller including the ones that pass an unordered collection.
    ordered = sorted(holdings, key=lambda item: item.symbol)

    missing = sorted({item.symbol for item in ordered if item.symbol not in prices})
    if missing:
        raise MissingPriceError(missing)

    # Kept unrounded so that holdings_value below is a sum of exact parts.
    priced = [(item.symbol, item.quantity, prices[item.symbol]) for item in ordered]

    holdings_value = sum(
        (quantity * price for _, quantity, price in priced), start=Decimal("0")
    )
    total_value = (
        holdings_value + cash_balance
        if reported_total_value is None
        else reported_total_value
    )

    return Valuation(
        cash_balance=_round_2dp(cash_balance),
        holdings_value=_round_2dp(holdings_value),
        total_value=_round_2dp(total_value),
        cash_allocation_percent=_percent_of(cash_balance, total_value),
        holdings=[
            HoldingValuation(
                symbol=symbol,
                quantity=quantity,
                price=price,
                holding_value=_round_2dp(quantity * price),
                allocation_percent=_percent_of(quantity * price, total_value),
            )
            for symbol, quantity, price in priced
        ],
    )


def calculate_scenario(
    holdings: Iterable[PricedHolding],
    cash_balance: Decimal,
    prices: Mapping[str, Decimal],
    symbol: str,
    price_change: Decimal,
    *,
    reported_total_value: Decimal | None = None,
) -> Scenario:
    """Re-price one holding by `price_change` and report what moves.

    `price_change` is a **fraction**: `Decimal("-0.10")` is a ten percent fall. The
    percentage is the caller's business — an HTTP handler parses "-10" and divides by a
    hundred, and a future AI tool will do the same from its own phrasing. Keeping the
    conversion outside means this function never has to guess whether "10" means a tenth
    or ten percent.

    Nothing else moves: cash, the other holdings, and every quantity stay exactly as they
    are, so the only difference between the two sides is one price.

    Built by calling `calculate_valuation` twice rather than by doing its arithmetic again.
    A scenario *is* two valuations, and sharing the code is what guarantees the "before"
    column matches what `GET /portfolio/valuation` reports for the same portfolio.

    Pure: no database, no network, no writes.

    Raises HoldingNotFoundError if the portfolio does not hold `symbol`,
    MissingPriceError if it holds it but has no price for it, and ValueError if
    `price_change` is below -1 (a price cannot fall by more than all of itself).
    """
    if price_change < Decimal("-1"):
        raise ValueError(
            "price_change is a fraction: -1 is a total loss, and a price cannot fall "
            "further than that"
        )

    ordered = sorted(holdings, key=lambda item: item.symbol)

    # Checked before the price, so an unheld symbol is reported as unheld rather than as
    # an unpriceable one.
    if not any(item.symbol == symbol for item in ordered):
        raise HoldingNotFoundError(symbol)

    if symbol not in prices:
        raise MissingPriceError([symbol])

    # Prices keep full precision here: the shocked price is an intermediate value, and
    # only the numbers handed back are rounded.
    price_before = prices[symbol]
    price_after = price_before * (Decimal("1") + price_change)

    shocked = dict(prices)
    shocked[symbol] = price_after

    # A reported total moves with the one price that moved. Cash, quantities and every
    # other price are identical on both sides, so the shocked holding's own change is the
    # only thing the total can have moved by -- whereas handing both sides the same
    # reported total would report a portfolio whose value did not change when a price did.
    after_total = (
        None
        if reported_total_value is None
        else reported_total_value
        + (price_after - price_before)
        * next(item.quantity for item in ordered if item.symbol == symbol)
    )

    before = calculate_valuation(
        ordered, cash_balance, prices, reported_total_value=reported_total_value
    )
    after = calculate_valuation(
        ordered, cash_balance, shocked, reported_total_value=after_total
    )

    holding_before = next(item for item in before.holdings if item.symbol == symbol)
    holding_after = next(item for item in after.holdings if item.symbol == symbol)

    # Taken from the rounded totals, not from raw intermediates, so that the change on
    # screen equals the difference between the two totals on screen. A change that does
    # not match the numbers either side of it reads as a bug, however it was computed.
    change_value = after.total_value - before.total_value

    return Scenario(
        symbol=symbol,
        price_change=price_change,
        # Rounded to cents for the same reason: a shocked price arrives with more decimal
        # places than the price it came from, and showing "135.0000" next to "150.00"
        # would look like two different kinds of number.
        price_before=_round_2dp(holding_before.price),
        price_after=_round_2dp(holding_after.price),
        holding_value_before=holding_before.holding_value,
        holding_value_after=holding_after.holding_value,
        total_value_before=before.total_value,
        total_value_after=after.total_value,
        change_value=_round_2dp(change_value),
        change_percent=_percent_of(change_value, before.total_value),
    )
