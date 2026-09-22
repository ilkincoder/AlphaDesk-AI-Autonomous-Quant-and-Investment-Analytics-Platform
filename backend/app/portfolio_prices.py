"""Which prices value this portfolio, and where they came from.

One question, asked in one place, because three callers need the same answer: the two
portfolio endpoints and the Module 1 agent's portfolio tool. If each decided for itself,
one of them would eventually be valuing a real position at a fictional price.

**The rule is the portfolio's broker link, not the caller's preference.** Once a portfolio
has been synchronised with an account, the broker's own prices are the only prices that
describe it: the quantities on screen are real, so a demo price against them would be a
number that looks like a valuation and is not one. A linked holding with no stored price
therefore raises `MissingPriceError` rather than falling back -- "I cannot price what you
hold" is a truthful answer, and a demo price would be a false one.

A portfolio that has never been synchronised keeps the demo table, which is exactly what
`GET /portfolio/valuation` has always returned. That is the state the UI labels "Demo
prices", and it stays distinguishable from the real one.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.models import Portfolio
from app.valuation import DEMO_PRICES, DEMO_PRICE_SOURCE, MissingPriceError


@dataclass(frozen=True)
class PriceBasis:
    """The prices to value with, and everything the response says about them."""

    prices: Mapping[str, Decimal]
    source: str
    # The broker's own equity figure, when there is one. None for the demo table, which
    # has no total of its own to prefer over the arithmetic.
    reported_total_value: Decimal | None
    last_synced_at: datetime | None


def partial(portfolio: Portfolio) -> PriceBasis:
    """The basis as far as it goes: every price that exists, and no refusal.

    The table is incomplete exactly when a held symbol has no price, and only `resolve`
    treats that as an error. This is here because a caller that has already been refused a
    valuation may still want to say whose prices were involved and what each stored figure
    is -- an unpriced symbol does not make the *other* symbols' prices untrue.
    """
    if portfolio.broker is None:
        return PriceBasis(
            prices=DEMO_PRICES,
            source=DEMO_PRICE_SOURCE,
            reported_total_value=None,
            last_synced_at=None,
        )

    prices: dict[str, Decimal] = {}
    for holding in portfolio.holdings:
        if holding.market_price is not None:
            prices[holding.symbol] = holding.market_price

    return PriceBasis(
        prices=prices,
        source=portfolio.broker,
        reported_total_value=portfolio.broker_equity,
        last_synced_at=portfolio.last_synced_at,
    )


def resolve(portfolio: Portfolio) -> PriceBasis:
    """The complete price basis for `portfolio`.

    Raises `MissingPriceError` naming every held symbol with no price, because a valuation
    built on an incomplete table is a total that quietly omits a holding. There is no
    fallback to the demo table: once a portfolio holds a broker's positions, a fictional
    price against them would be a number that looks like a valuation and is not one.
    """
    basis = partial(portfolio)
    missing = sorted(
        holding.symbol
        for holding in portfolio.holdings
        if holding.symbol not in basis.prices
    )
    if missing:
        raise MissingPriceError(missing)
    return basis


__all__ = ["PriceBasis", "partial", "resolve"]
