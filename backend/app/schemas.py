"""Pydantic response schemas for the API.

Decimal fields are serialised as JSON *strings*, not numbers. JSON numbers are
IEEE-754 doubles, so a client that parsed `10000.00` as a float would reintroduce
exactly the precision error the NUMERIC columns exist to avoid. The trailing zeros
come from the column scale, so the format is deterministic: quantity always has 6
decimal places, price always 4, cash always 2.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


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