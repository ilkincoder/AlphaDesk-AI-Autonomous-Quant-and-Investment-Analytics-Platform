"""Pydantic response schemas for the API.

Decimal fields are serialised as JSON *strings*, not numbers. JSON numbers are
IEEE-754 doubles, so a client that parsed `10000.00` as a float would reintroduce
exactly the precision error the NUMERIC columns exist to avoid. The trailing zeros
come from the column scale, so the format is deterministic: quantity always has 6
decimal places, price always 4, cash always 2. Valuation amounts are not read from
columns at all -- the calculation quantises them to 2 decimal places itself, so they
arrive here already rounded and keep that scale.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

# --- the analysis chat ---------------------------------------------------------------------
#
# These describe the conversation API in `app/analysis_api.py`. They live here with the rest of
# the API's vocabulary rather than beside the routes, so there is one place to look for the
# shape of what this application accepts and returns.

# The examples below are what the OpenAPI page shows. They are real: the question and the
# clarification are the ones in the README's worked example, and they are what a first client
# should try.
_CHAT_EXAMPLE = {
    "conversation_id": "9f2c1e4b7a3d4c8e9b1f0a2d3c4e5f60",
    "request_id": "req-1",
    "message": "Compare NVDA's price movement with insider activity.",
}


class ConversationCreatedOut(BaseModel):
    """A conversation with nothing in it yet."""

    conversation_id: str = Field(description="server-generated; use it for every later turn")
    created_at: str
    updated_at: str
    settled: dict
    pending_clarification: dict | None = None
    processing: dict | None = None


class ChatRequest(BaseModel):
    """One user turn.

    Only user content and the arguments the run context already understands. `extra="forbid"`
    is doing real work here: a client that sends a system message, an assistant message, a
    tool output, an evidence map or a budget override gets a 422 rather than having it quietly
    ignored -- or, worse, honoured. None of those are this API's to accept; the conversation
    the Supervisor sees is assembled by the backend from its own records.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"example": _CHAT_EXAMPLE})

    conversation_id: str = Field(min_length=1, max_length=32)
    # Client-generated, and the only thing standing between a retry and a second paid
    # analysis. Unique within a conversation; two clients that both start at "1" do not clash.
    request_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    # Optional, and authoritative when given -- exactly as `--symbol` is on the command line.
    symbol: str | None = Field(default=None, max_length=20)
    start_date: date | None = None
    end_date: date | None = None
    as_of: date | None = None

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be blank")
        return stripped


class ChatTurnOut(BaseModel):
    """What one turn produced.

    `status` is the run's own vocabulary -- `completed`, `clarification_needed`,
    `company_not_stored`, `unsupported_capability`, `invalid_citations`, `budget_exhausted`,
    `provider_failed`, `service_failed`, `configuration_error` -- plus the store's
    `processing` and `interrupted`. The HTTP status is derived from it, so the same request id
    always gets the same answer.
    """

    conversation_id: str
    turn_id: str
    sequence: int | None = None
    request_id: str
    status: str
    run_id: str | None = None

    # Where the request went and why. Declared here because the handler returns them -- the
    # route answers with a JSONResponse, which bypasses `response_model`, so anything not
    # listed above was a field the API sent and the schema denied. These are additive.
    destination: str | None = None
    route_reason: str | None = None
    # The question as the run received it, and the date its relative periods resolved against.
    # Both are the server's, not the client's, which is why a resumed clarification can differ
    # from what was just typed.
    question: str | None = None
    reference_date: date | None = None
    model: str | None = None

    answer: str | None = None
    symbol: str | None = None
    resolved: dict | None = None
    # The instant the run's own information date stops being knowable, derived from
    # `resolved.as_of`. Distinct from any narrower cutoff a tool reported inside its payload.
    information_cutoff: str | None = None
    citations: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    tool_executions: list[dict] = Field(default_factory=list)
    usage: dict | None = None
    warnings: list[str] = Field(default_factory=list)

    # Set when the turn did not complete. Always a sentence a user could be shown: no
    # tracebacks, no connection strings, no prompts.
    failure: str | None = None
    # Present when the turn is still running, so a client knows how long to wait.
    retry_after_seconds: int | None = None


class ConversationHistoryOut(BaseModel):
    """A conversation's state and a page of its turns, oldest first.

    The metadata is here and not only on the creation response, because it is what a client
    needs to know *what to do next*: whether a clarification is waiting, and whether a turn is
    still running. Asking for it separately would mean two round trips to answer one question.
    """

    conversation_id: str
    created_at: str | None = None
    updated_at: str | None = None
    settled: dict | None = None
    pending_clarification: dict | None = None
    processing: dict | None = None
    total_turns: int
    limit: int
    offset: int
    turns: list[dict]
