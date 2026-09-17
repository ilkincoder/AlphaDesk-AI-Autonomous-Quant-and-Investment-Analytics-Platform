"""Pydantic response schemas for the API.

Decimal fields are serialised as JSON *strings*, not numbers. JSON numbers are
IEEE-754 doubles, so a client that parsed `10000.00` as a float would reintroduce
exactly the precision error the NUMERIC columns exist to avoid. The trailing zeros
come from the column scale, so the format is deterministic: quantity always has 6
decimal places, price always 4, cash always 2. Valuation amounts are not read from
columns at all -- the calculation quantises them to 2 decimal places itself, so they
arrive here already rounded and keep that scale.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class HoldingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    quantity: Decimal
    average_buy_price: Decimal


class PortfolioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    currency: str
    cash_balance: Decimal
    holdings: list[HoldingOut]


class ValuationHoldingOut(BaseModel):
    """One holding's contribution to the valuation.

    `from_attributes` because it is built from a `valuation.HoldingValuation`
    dataclass, which carries computed fields the holdings table has no column for.
    """

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    quantity: Decimal
    price: Decimal
    holding_value: Decimal
    allocation_percent: Decimal | None


class PortfolioValuationOut(BaseModel):
    """A portfolio valued against the demo prices.

    `allocation_percent` and `cash_allocation_percent` are null when `total_value` is
    zero, because the share is undefined rather than zero.
    """

    portfolio_id: int
    currency: str
    # Which prices produced these numbers. "demo" means the fictional constants in
    # app.valuation, never a live quote -- hence no timestamp: there is no market data
    # here to have a time.
    price_source: str
    cash_balance: Decimal
    holdings_value: Decimal
    total_value: Decimal
    cash_allocation_percent: Decimal | None
    holdings: list[ValuationHoldingOut]


class ScenarioRequest(BaseModel):
    """A hypothetical price move for one held symbol.

    `price_change_percent` is a signed percentage, so `-10` means a ten percent fall.
    It is declared as `Decimal` and accepts a JSON *string*, which is what the frontend
    sends: parsing "-10" straight to `Decimal` never passes through a binary float.
    """

    symbol: str = Field(min_length=1)
    price_change_percent: Decimal = Field(ge=Decimal("-100"), le=Decimal("100"))


class ScenarioOut(BaseModel):
    """One holding re-priced, and what it did to the portfolio.

    Every `*_before` is the portfolio as it stands; every `*_after` is the same portfolio
    with that single price changed. Cash, the other holdings, and all quantities are the
    same on both sides, which is what makes the difference attributable to one move.

    `change_percent` is null when `total_value_before` is zero — nothing to be a
    percentage of.
    """

    portfolio_id: int
    currency: str
    price_source: str
    symbol: str
    price_change_percent: Decimal
    price_before: Decimal
    price_after: Decimal
    holding_value_before: Decimal
    holding_value_after: Decimal
    total_value_before: Decimal
    total_value_after: Decimal
    change_value: Decimal
    change_percent: Decimal | None