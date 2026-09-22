"""Tool D: whether a symbol is held, how much of it, and what the portfolio is worth.

Reads the portfolio and values it with `app.valuation` -- the same `calculate_valuation` the
`/portfolio/valuation` endpoint uses, over the same prices. No second valuation system, no
re-derived arithmetic, and no substitution of imported daily prices for either basis.

**Three different answers, kept apart.** "There is no portfolio", "the portfolio does not hold
this symbol" and "it holds it but cannot price it" would all collapse into a vague "no data" if
this returned one shape. They are different outcomes, so they get different statuses and
different reasons.

**These are current holdings, not a historical position.** A Form 4 question is asked about a
date; this is not. The rows read here are the portfolio as it stands now, and nothing about
them is dated to the analysis cutoff. Presenting them as "your position on 2026-09-17" would be
an invention, so every answer says so.

**Which prices these are is the portfolio's business.** `app.portfolio_prices` answers it, and
it is either the broker's own stored prices (a synchronised portfolio) or the fictional demo
constants in `app/valuation.py` (one that has never been synchronised). The answer reports
which, and never mixes the two: a real quantity valued at a demo price would be a number that
looks like a valuation and is not one. Combining either basis with the stored daily prices of
the other companies would produce a portfolio that looks real and is not, so they stay apart,
and the answer says that rather than approximating it.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import portfolio_prices
from app.models import Holding, Portfolio
from app.portfolio_identity import DEMO_PORTFOLIO_NAME, find
from app.portfolio_prices import PriceBasis
from app.tools.results import (
    ToolResult,
    ToolStatus,
    database_unavailable,
    merged_warnings,
    unavailable,
)
from app.valuation import (
    DEMO_PRICE_SOURCE,
    HoldingValuation,
    MissingPriceError,
    calculate_valuation,
)

logger = logging.getLogger(__name__)

TOOL_NAME = "portfolio_context"

DESCRIPTION = (
    "Report whether one symbol is held by the AlphaDesk portfolio, how much of it, and what "
    "the portfolio is currently worth. Use it when asked whether the user owns a stock, or how "
    "a holding relates to the rest of the portfolio. Returns the portfolio's identity, whether "
    "the symbol is held, the stored quantity and average purchase price, the price and holding "
    "value where one is available, the portfolio's cash, holdings value, total value and "
    "allocation percentages, which prices produced them, and when the portfolio was read. "
    "Three outcomes are distinct: no portfolio stored, the symbol not held, and held but "
    "unpriceable. These are the portfolio's currently stored holdings, not a historical "
    "snapshot, and they are not dated to any analysis cutoff. Prices are either the broker's "
    "own, as of the last successful sync, or a fictional demo table -- the answer says which, "
    "and they are never mixed. Either way they must not be combined with stored daily prices "
    "to imply real exposure. Real, consistently dated portfolio exposure is not available in "
    "this system."
)

# Why the request could not be answered.
REASON_PORTFOLIO_NOT_FOUND = "portfolio_not_found"
REASON_MISSING_PRICE = "valuation_unavailable_missing_price"

# On every answer, whatever the outcome and whichever prices were used.
STANDING_WARNINGS = (
    "These are the portfolio's currently stored holdings, not a historical position. Nothing "
    "here is dated to an analysis cutoff and nothing is a snapshot as at one.",
    "Real, consistently dated portfolio exposure is not available in this system. Nothing "
    "here says what the portfolio was worth on any past date.",
)

# The two price bases say different things about themselves, and neither sentence is true of
# the other, so each answer carries only the one that applies to it.
DEMO_PRICE_WARNING = (
    "Values use the fictional demo price table in app/valuation.py -- not live quotes and not "
    "the average purchase price stored on the holding. They must not be combined with stored "
    "daily prices to imply a real portfolio exposure."
)

DEMO_PRICE_SOURCE_NOTE = (
    "The fictional constants in app.valuation.DEMO_PRICES. Not imported market prices, and "
    "not the average_buy_price recorded on the holding."
)

BROKER_PRICE_WARNING = (
    "Values use the prices the broker last reported for these positions, as of the sync "
    "recorded below -- not a live quote, and not the average purchase price stored on the "
    "holding. The figures are only as current as that read, and they must not be combined "
    "with stored daily prices to imply a longer history than that."
)


def _price_warnings(basis: PriceBasis) -> list[str]:
    """The one price-source caveat this basis needs."""
    if basis.source == DEMO_PRICE_SOURCE:
        return [DEMO_PRICE_WARNING]
    return [
        BROKER_PRICE_WARNING,
        f"Read from {basis.source} at {basis.last_synced_at.isoformat()}.",
    ]


def _price_source_note(basis: PriceBasis) -> str:
    if basis.source == DEMO_PRICE_SOURCE:
        return DEMO_PRICE_SOURCE_NOTE
    return (
        f"The prices stored by the {basis.source} sync, as the broker reported them. Not "
        "imported market prices, and not the average_buy_price recorded on the holding."
    )


class PortfolioContextRequest(BaseModel):
    """Which symbol to place in the portfolio."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)

    @field_validator("symbol")
    @classmethod
    def _normalise_symbol(cls, value: str) -> str:
        normalised = value.strip().upper()
        if not normalised:
            raise ValueError("symbol must not be blank")
        return normalised


class PortfolioIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    name: str
    currency: str


class HoldingContext(BaseModel):
    """One symbol's stored position, and its demo valuation where one could be produced.

    `quantity` and `average_buy_price` are read from the row and are present whether or not a
    valuation could be produced. `price`, `holding_value` and `allocation_percent` are the
    valuation's, and are None when it declined to produce any figure.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    quantity: Decimal
    average_buy_price: Decimal
    price: Decimal | None
    holding_value: Decimal | None
    allocation_percent: Decimal | None


class PortfolioValuation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cash_balance: Decimal
    holdings_value: Decimal
    total_value: Decimal
    cash_allocation_percent: Decimal | None
    holdings: list[HoldingContext]


class PortfolioContextData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    portfolio: PortfolioIdentity
    price_source: str
    price_source_note: str
    read_at: datetime
    held: bool
    holding: HoldingContext | None
    valuation: PortfolioValuation | None
    valuation_unavailable_reason: str | None
    limitations: list[str]


def run(session: Session, request: PortfolioContextRequest) -> ToolResult:
    """Read the portfolio and value it. Reads only; writes nothing."""
    try:
        # The holdings come back with it, so nothing is left to load lazily after the
        # session has closed.
        portfolio = find(session)
        holdings = list(portfolio.holdings) if portfolio is not None else []
        identity = (
            None
            if portfolio is None
            else PortfolioIdentity(
                id=portfolio.id, name=portfolio.name, currency=portfolio.currency
            )
        )
        cash_balance = None if portfolio is None else portfolio.cash_balance
    except SQLAlchemyError as exc:
        logger.exception("portfolio context could not read the stored data")
        return database_unavailable(
            tool=TOOL_NAME,
            error_name=type(exc).__name__,
            symbol=request.symbol,
            warnings=STANDING_WARNINGS,
        )

    if identity is None or cash_balance is None:
        return unavailable(
            tool=TOOL_NAME,
            reason=REASON_PORTFOLIO_NOT_FOUND,
            symbol=request.symbol,
            warnings=merged_warnings(
                STANDING_WARNINGS,
                [
                    f"No portfolio named {DEMO_PORTFOLIO_NAME!r} is stored, so there is "
                    "nothing to place this symbol in. Run: "
                    "`python -m app.seed`.",
                ],
            ),
        )

    # Read once, so every value in the answer describes the same moment.
    read_at = datetime.now(timezone.utc)
    held_row = next(
        (row for row in holdings if row.symbol.strip().upper() == request.symbol), None
    )

    valuation_error: str | None = None
    valuation: PortfolioValuation | None = None
    by_symbol: dict[str, HoldingValuation] = {}

    try:
        # Which prices describe this portfolio is the portfolio's business, and it is
        # answered in one place so this tool cannot value a real position at a fictional
        # price. A synchronised portfolio has no demo fallback.
        basis = portfolio_prices.resolve(portfolio)
        valued = calculate_valuation(
            holdings,
            cash_balance,
            basis.prices,
            reported_total_value=basis.reported_total_value,
        )
        by_symbol = {item.symbol.strip().upper(): item for item in valued.holdings}
        valuation = PortfolioValuation(
            cash_balance=valued.cash_balance,
            holdings_value=valued.holdings_value,
            total_value=valued.total_value,
            cash_allocation_percent=valued.cash_allocation_percent,
            holdings=[
                _holding(row, basis, by_symbol.get(row.symbol.strip().upper()))
                for row in sorted(holdings, key=lambda item: item.symbol)
            ],
        )
    except MissingPriceError as exc:
        # The valuation refuses to produce a total that omits a holding, because a total that
        # quietly leaves one out looks complete while being wrong. So one unpriceable symbol
        # withholds every derived figure here too -- deliberately, not by accident.
        #
        # Whatever prices do exist, so the answer can still say whose they are and what the
        # priced rows are worth. `resolve` refused to return a *complete* basis; it did not
        # make the prices it did find untrue.
        basis = portfolio_prices.partial(portfolio)
        valuation_error = REASON_MISSING_PRICE
        logger.warning(
            "portfolio valuation unavailable: %d held symbol(s) have no price",
            len(exc.symbols),
        )

    holding = (
        None
        if held_row is None
        else _holding(held_row, basis, by_symbol.get(request.symbol))
    )

    warnings = merged_warnings(STANDING_WARNINGS, _price_warnings(basis))
    status = ToolStatus.OK
    reason: str | None = None

    if held_row is None:
        warnings.append(
            f"{request.symbol} is not a holding of {identity.name!r}. That is an answer about "
            "the portfolio as it stands, not a statement that it was never held. Stored "
            f"holdings: {_held_symbols(holdings)}."
        )
    if valuation_error is not None:
        status = ToolStatus.PARTIAL
        reason = valuation_error
        warnings.append(
            "No price exists for at least one held symbol, so the valuation produced no "
            "figures at all -- not even for the holdings that do have one. That is the "
            "existing valuation's deliberate refusal to report a total that omits a holding. "
            "Stored quantities are unaffected and are reported as they are."
        )

    data = PortfolioContextData(
        portfolio=identity,
        price_source=basis.source,
        price_source_note=_price_source_note(basis),
        read_at=read_at,
        held=held_row is not None,
        holding=holding,
        valuation=valuation,
        valuation_unavailable_reason=valuation_error,
        limitations=list(warnings),
    )

    return ToolResult(
        tool=TOOL_NAME,
        status=status,
        reason=reason,
        symbol=request.symbol,
        # No `as_of` and no cutoff: the answer is about the present, not about a historical
        # moment, and supplying a date here would invite it to be read as one.
        as_of=None,
        information_cutoff=None,
        warnings=merged_warnings(warnings),
        data=data.model_dump(mode="json"),
    )


def _holding(
    row: Holding, basis: PriceBasis, valued: HoldingValuation | None
) -> HoldingContext:
    """One holding, from the stored row plus the valuation's figures where it produced them."""
    return HoldingContext(
        symbol=row.symbol,
        quantity=row.quantity,
        # The recorded purchase price. Never used as a valuation price -- that is what
        # `price` and `holding_value` are for, and the two are kept visibly apart.
        average_buy_price=row.average_buy_price,
        # A lookup in the same table the valuation used. Shown even when the valuation
        # declined, because a stored fact is not made untrue by an unrelated missing price.
        price=basis.prices.get(row.symbol.strip().upper()),
        holding_value=None if valued is None else valued.holding_value,
        allocation_percent=None if valued is None else valued.allocation_percent,
    )


def _held_symbols(holdings: list[Holding]) -> str:
    symbols = sorted(row.symbol for row in holdings)
    return ", ".join(symbols) if symbols else "none"


__all__ = [
    "DESCRIPTION",
    "PortfolioContextData",
    "PortfolioContextRequest",
    "TOOL_NAME",
    "run",
]
