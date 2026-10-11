"""Pydantic response schemas for the API.

Decimal fields are serialised as JSON *strings*, not numbers. JSON numbers are
IEEE-754 doubles, so a client that parsed `10000.00` as a float would reintroduce
exactly the precision error the NUMERIC columns exist to avoid. The trailing zeros
come from the column scale, so the format is deterministic: quantity always has 6
decimal places, price always 4, cash always 2. Valuation amounts are not read from
columns at all -- the calculation quantises them to 2 decimal places itself, so they
arrive here already rounded and keep that scale.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HoldingOut(BaseModel):
    """One stored position.

    `market_price` and `market_value` are the broker's, and are null on a portfolio that
    has never been synchronised. Null means "nothing has priced this row", which is a
    different fact from a price of zero -- and the frontend shows a dash for it rather
    than a figure.
    """

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    quantity: Decimal
    average_buy_price: Decimal
    market_price: Decimal | None = None
    market_value: Decimal | None = None


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
    """A portfolio valued against whichever prices describe it.

    `allocation_percent` and `cash_allocation_percent` are null when `total_value` is
    zero, because the share is undefined rather than zero.
    """

    portfolio_id: int
    currency: str
    # Which prices produced these numbers. "demo" means the fictional constants in
    # app.valuation, never a live quote -- hence no timestamp on that path. Any other
    # value is a broker slug, and `last_synced_at` says how old the figures are.
    price_source: str
    # When the snapshot behind these numbers was read from the broker. Null on the demo
    # basis, where there is no market data to have a time.
    last_synced_at: datetime | None = None
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
    # As on the valuation: when the broker snapshot behind the "before" column was read.
    last_synced_at: datetime | None = None
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

class PortfolioSyncOut(BaseModel):
    """The result of one broker sync.

    `applied` is False when a newer snapshot was already stored and this one was refused
    for being older. The figures below are then the **stored** ones, which is what the
    portfolio actually holds -- a refused snapshot's numbers would describe a state the
    database is not in.

    `broker_account_id` is the broker's own account identifier, not a credential. Nothing
    in this response is secret, and no key is ever echoed into it.
    """

    applied: bool
    portfolio_id: int
    broker: str | None
    broker_account_id: str | None
    last_synced_at: datetime | None
    currency: str
    cash_balance: Decimal
    equity: Decimal | None
    position_count: int


# --- news ------------------------------------------------------------------------------------
#
# The shapes the News page reads. Two times appear on every article and they are deliberately
# not interchangeable: `published_at` is when the publisher says the story ran, and
# `ingested_at` is when this database stored it. A page that showed only one of them would be
# unable to say whether a story was new or merely newly fetched.


class NewsArticleOut(BaseModel):
    """One article, as a listing shows it."""

    id: int
    provider: str
    source: str
    category: str
    title: str
    excerpt: str
    url: str
    symbols: list[str]
    # The publisher's own publication time. Never replaced by our ingestion time.
    published_at: datetime
    # The provider's own revision time, when it supplied one. NULL is "the provider said
    # nothing about revisions", not "never revised".
    provider_updated_at: datetime | None = None
    # When this database stored the article, so the two can be told apart on screen.
    ingested_at: datetime
    # False while an article is stored but not in the search index, which is the state an
    # indexing failure leaves behind.
    indexed: bool


class NewsSourceStatusOut(BaseModel):
    """What one source last did, so the page can show staleness rather than guess at it."""

    source: str
    last_success_at: datetime | None = None


class NewsListOut(BaseModel):
    """A page of articles, newest first by publication time.

    The source block is returned with the list rather than from its own route: the page needs
    it even when the list is empty, and two round trips to draw one screen is one more than
    the screen needs.
    """

    total: int
    limit: int
    offset: int
    articles: list[NewsArticleOut]
    sources: list[NewsSourceStatusOut]
    # Stated once, here, because it is a property of the feed rather than of any article.
    # Alpaca documents that news is delayed without real-time entitlement, and a successful
    # request does not prove otherwise -- so nothing in this build claims to be live.
    timeliness: str


class NewsSearchPassageOut(BaseModel):
    """One retrieved passage and the article it came from.

    `similarity` is a cosine similarity, not a percentage: it says how close two vectors are
    and nothing about whether the passage answers anything.
    """

    text: str
    similarity: float
    chunk_index: int
    article: NewsArticleOut


class NewsSearchOut(BaseModel):
    """A semantic search over the stored news.

    `status` is the whole answer: `ok`, `no_matching_results`, `nothing_indexed`,
    `index_unavailable` or `model_unavailable`. "Nothing matched" and "the index is down"
    are different things to be told, and collapsing them would report one as the other.
    """

    status: str
    reason: str | None = None
    query: str
    returned: int
    passages: list[NewsSearchPassageOut]
    warnings: list[str] = Field(default_factory=list)


class NewsSourceResultOut(BaseModel):
    """What one source did during an ingestion run."""

    source: str
    status: str
    fetched: int
    new: int
    updated: int
    unchanged: int
    indexed: int
    failed_index: int
    # How many release pages were retrieved on top of the feed itself. An official feed
    # carries a headline and often one sentence, so the release behind an entry is what there
    # is to index -- and a repeat run that already holds those articles should report zero
    # here rather than re-reading pages nothing has changed.
    releases_read: int = 0
    error: str | None = None
    last_success_at: datetime | None = None


class NewsIngestOut(BaseModel):
    """The result of one bounded ingestion.

    A partial failure is a **200**, not an error: the run completed, some sources stored
    what they fetched, and `sources` says exactly which did and which did not. Only a run in
    which every source failed is a 503, because that is the case where nothing was read.
    """

    started_at: datetime
    completed_at: datetime
    symbols: list[str]
    stored: int
    indexed: int
    failed_sources: list[str]
    sources: list[NewsSourceResultOut]


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


# --- the rebalance proposal ------------------------------------------------------------------
#
# What the News & Trading page reads. Everything numeric is a Decimal here and a *string* on the
# wire, for the same reason every other amount in this file is: a JSON number is an IEEE-754
# double, and the whole point of computing these in Decimal is lost the moment a client parses
# one as a float.
#
# The shape follows the page: a snapshot of what was computed from, the policy it was computed
# under, the article evidence, and the calculation. The calculation's before/after blocks carry
# every holding *and* cash, so each side is a complete allocation -- they are never merged into
# one, because the two sides can have different denominators.


class ProposalPositionOut(BaseModel):
    """One position in an allocation, at the price that allocation was valued with."""

    symbol: str
    quantity: Decimal
    price: Decimal
    holding_value: Decimal
    # Null when the allocation's total is zero, which is the existing valuation's convention: a
    # share of nothing is undefined rather than zero.
    allocation_percent: Decimal | None = None


class ProposalAllocationOut(BaseModel):
    """One complete allocation. The `before` and the `after` sides are two of these."""

    total_value: Decimal
    cash_balance: Decimal
    cash_allocation_percent: Decimal | None = None
    holdings: list[ProposalPositionOut]


class ProposalSnapshotOut(BaseModel):
    """The portfolio as it stood when the run started, frozen on the record.

    Every figure in the calculation was computed from these values and not from the portfolio as
    it is now, which is what makes the proposal checkable after the fact.
    """

    read_at: datetime
    price_source: str
    last_synced_at: datetime | None = None
    currency: str
    cash_balance: Decimal
    holdings_value: Decimal
    total_value: Decimal
    # The broker's own equity, where there is one. It is the total the current allocation is a
    # share of, and it need not equal the positions plus the cash exactly.
    reported_total_value: Decimal | None = None
    cash_allocation_percent: Decimal | None = None
    positions: list[ProposalPositionOut]


class ProposalScenarioAssumptionOut(BaseModel):
    """The assumed scenario a proposal was priced against. An assumption, not a forecast."""

    description: str
    shock_percent: Decimal


class ProposalPolicyOut(BaseModel):
    """The policy the targets were proposed under, as it was when they were proposed.

    Stored on the proposal rather than read from the running code, so an old proposal is always
    shown against the rules that actually produced it.
    """

    name: str
    description: str
    constraints: list[str]
    scenario: ProposalScenarioAssumptionOut


class ProposalTradeOut(BaseModel):
    """One proposed whole-share order. Nothing here has been sent anywhere."""

    symbol: str
    # "buy" or "sell". Always shown as a word: the colour alone does not carry it.
    action: str
    quantity: Decimal
    reference_price: Decimal
    estimated_value: Decimal
    quantity_before: Decimal
    quantity_after: Decimal


class ProposalAllocationLineOut(BaseModel):
    """One line of the target schedule: a held symbol, or cash.

    Three weights rather than one, because they are three different facts. `target_percent` is
    what the agent asked for; `achieved_percent` is where whole-share rounding actually leaves
    the holding, which is rarely identical; `current_percent` is what the portfolio holds now.

    `reason` is the agent's own words for that specific number, carried rather than written here,
    and `evidence_refs` are the retrieved articles it rests on -- empty when the reason is about
    the policy or the portfolio's shape rather than about an article.
    """

    symbol: str
    current_percent: Decimal | None = None
    target_percent: Decimal
    achieved_percent: Decimal | None = None
    # `increase`, `decrease` or `retain`, compared at the precision the agent was shown.
    movement: str
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)


class ProposalConcentrationOut(BaseModel):
    symbol: str
    allocation_percent: Decimal | None = None


class ProposalScenarioOut(BaseModel):
    """What the assumed shock would do, calculated rather than predicted."""

    symbol: str
    price_change_percent: Decimal
    price_before: Decimal
    price_after: Decimal
    holding_value_before: Decimal
    holding_value_after: Decimal
    total_value_before: Decimal
    total_value_after: Decimal
    change_value: Decimal
    change_percent: Decimal | None = None


class ProposalReconciliationOut(BaseModel):
    """The broker's total beside the summed one, and the gap between them.

    Disclosed rather than corrected: the current allocation is a share of the broker's figure
    and the proposed allocation is computed from the holdings and the cash.
    """

    reported_total_value: Decimal | None = None
    summed_total_value: Decimal
    difference: Decimal | None = None
    note: str


class ProposalCalculationOut(BaseModel):
    """The deterministic result. Every figure the page shows comes from here, not from prose."""

    outcome: str
    trades: list[ProposalTradeOut]
    targets: dict[str, Decimal]
    # Every holding and the cash, before, requested and achieved.
    allocations: list[ProposalAllocationLineOut] = Field(default_factory=list)
    cash_target: Decimal
    before: ProposalAllocationOut
    after: ProposalAllocationOut
    cash_before: Decimal
    cash_after: Decimal
    sell_proceeds: Decimal
    buy_cost: Decimal
    # True when the purchases cost more than the account's cash, so they depend on the proposed
    # sales having executed. Those proceeds are not available yet.
    buys_depend_on_sells: bool
    largest_before: ProposalConcentrationOut
    largest_after: ProposalConcentrationOut
    scenario: ProposalScenarioOut
    reconciliation: ProposalReconciliationOut


class ProposalEvidenceOut(BaseModel):
    """One retrieved article, as a citation.

    Publisher, publication date and a link to the original. The reference is the application's,
    assigned at retrieval, and a rationale citing one that is not in this list is refused.
    """

    reference: str
    article_id: int
    title: str
    publisher: str
    url: str
    published_at: datetime
    category: str
    symbols: list[str]
    # A cosine similarity, not a probability and not a confidence.
    similarity: float
    # False for an article older than the recent window. Older context is labelled rather than
    # dropped, and a rationale resting on it is required to say so.
    recent: bool


class DecisionOrderOut(BaseModel):
    """One order as the decision resolved it. Every figure is the server's, not a client's."""

    sequence: int
    symbol: str
    # "buy" or "sell", always shown as a word: the colour alone does not carry it.
    side: str
    quantity: Decimal
    estimated_price: Decimal
    estimated_value: Decimal
    # What the account held when this plan was validated.
    quantity_before: Decimal


class ProposalDecisionOut(BaseModel):
    """A recorded decision, as the page reads it back."""

    decision_id: str
    proposal_id: str
    # "approved" or "rejected".
    decision: str
    decided_at: datetime
    # The digest of the exact orders this decision bound to.
    plan_fingerprint: str
    price_drift_percent: Decimal | None = None
    reason: str | None = None
    order_plan: list[DecisionOrderOut] = Field(default_factory=list)


class RebalanceProposalOut(BaseModel):
    """One proposal, as the page reads it.

    `status` is `proposed`, `no_change`, `unavailable`, `interrupted`, or `generating` while the
    run is in flight. There is no approved, submitted or filled status: this milestone ends at
    generation, and the record has no field one could be written into.

    `freshness` is computed when the proposal is *read*, by comparing the portfolio's current
    quantities, prices, cash, currency and price source against the ones frozen on the record.
    It is not inferred from a sync timestamp, and it is not one flag: `prices_updated` means the
    portfolio holds the same positions at different prices and the proposal is still a faithful
    estimate, while `portfolio_changed` means the arithmetic was computed against something that
    no longer exists. `unknown` means it could not be checked -- never that it is current.
    """

    proposal_id: str
    status: str
    created_at: datetime
    completed_at: datetime | None = None

    # One of `current`, `prices_updated`, `portfolio_changed`, `unknown`.
    freshness: str = "unknown"
    freshness_reason: str | None = None
    # The symbols the comparison found a difference in, so a page can name them rather than
    # saying only that something moved.
    freshness_changed: list[str] = Field(default_factory=list)

    snapshot: ProposalSnapshotOut | None = None
    policy: ProposalPolicyOut | None = None
    calculation: ProposalCalculationOut | None = None
    evidence: list[ProposalEvidenceOut] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    # The agent's proposal: target weights and the prose explaining them. The rationale is text
    # a model wrote and is presented as such; nothing here corroborates it.
    targets: dict[str, Decimal] | None = None
    rationale: str | None = None

    # Set only when no proposal could be produced. Always a sentence a user could be shown.
    failure: str | None = None
    failure_reason: str | None = None
    usage: dict | None = None

    # --- the person's decision, when there is one ------------------------------------------
    #
    # Separate from `status`, which is the *run's* life. An approved proposal is still a
    # `proposed` proposal: what changed is that somebody decided something about it, and that is
    # a different fact recorded in a different place.
    decision: ProposalDecisionOut | None = None
    # The batch an approval opened, so a page restoring itself can offer to open Orders without
    # the browser having kept the id.
    batch_id: str | None = None


class RebalanceProposalRequest(BaseModel):
    """One request for a proposal.

    The body carries a request id and nothing else. There is no field for a portfolio, a symbol,
    a policy or a budget: the request is "propose something for this portfolio", and everything
    it acts on is this application's own record. `extra="forbid"` is doing real work here for
    the same reason it is on `ChatRequest`.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)


class RebalanceProposalView(BaseModel):
    """The latest proposal, or an explicit null.

    200 with a null body rather than a 404 when nothing has been generated: a page restoring
    itself on load is asking "is there one?", and "no" is an answer rather than an error.
    """

    proposal: RebalanceProposalOut | None = None


# --- approvals and execution -------------------------------------------------------------------


class ProposalDecisionRequest(BaseModel):
    """One decision about one proposal.

    The body carries **no trade data at all**, and `extra="forbid"` is doing real work here for
    the same reason it is on `ChatRequest`: a client that sent a quantity would be refused rather
    than quietly ignored. What is executed is resolved server-side from the proposal's own stored
    calculation, and this is the schema that makes that a fact rather than a convention.
    """

    model_config = ConfigDict(extra="forbid")

    # Only these two words. A proposal is approved or rejected; there is no third decision.
    decision: str = Field(pattern="^(approve|reject)$")
    # Chosen by the client so a retry is free, exactly as a proposal's `request_id` is.
    idempotency_key: str = Field(min_length=1, max_length=64)
    # A second click on a plan whose prices have moved. It is not a permission and it is not
    # required for an approval that needed no renewal.
    confirm: bool = False
    # An optional sentence, kept only on a rejection. It is the person's own words and is never
    # filled in for them.
    reason: str | None = Field(default=None, max_length=500)


class DecisionOutcomeOut(BaseModel):
    """What a decision request did.

    `outcome` is the answer rather than the status code, because two of these are not errors:
    `renewal_required` is a plan whose prices moved and which is waiting for a second
    confirmation, and `already_decided` is a retry or a double click landing on the decision that
    already exists. `detail` is always a sentence a person can be shown.
    """

    outcome: str
    detail: str
    # The machine-readable cause, set on a refusal.
    reason: str | None = None
    decision: ProposalDecisionOut | None = None
    # The batch an approval opened. This is what the Orders page is focused on afterwards.
    batch_id: str | None = None
    # The orders that were validated. Present on an approval and on a renewal, because both are
    # showing the person what would be sent.
    plan: list[DecisionOrderOut] = Field(default_factory=list)
    price_drift_percent: Decimal | None = None


class OrderIntentOut(BaseModel):
    """One order in a batch, and everything the broker has said about it.

    `estimated_price` is the figure the plan was approved against and is labelled as an estimate
    wherever it is shown. `filled_avg_price` is the only actual price here, and it is null until
    something has filled.
    """

    intent_id: str
    sequence: int
    symbol: str
    side: str
    # Requested, whole shares. Never resized after approval.
    quantity: Decimal
    order_type: str
    time_in_force: str
    # planned, submitting, accepted, partially_filled, filled, rejected, canceled, expired,
    # unknown, not_found, halted.
    status: str
    # The broker's own status verbatim, beside the mapped one above.
    broker_status: str | None = None
    estimated_price: Decimal
    quantity_before: Decimal
    filled_quantity: Decimal
    filled_avg_price: Decimal | None = None
    # The stable id this order was submitted under, and the broker's own id for it.
    client_order_id: str
    broker_order_id: str | None = None
    submitted_at: datetime | None = None
    resolved_at: datetime | None = None
    error: str | None = None


class ExecutionBatchOut(BaseModel):
    """One execution batch: an approved plan and how far it has got.

    `status` is `pending`, `submitting`, `completed`, `partial` or `halted`. `partial` and
    `halted` are not failures of the application: they mean the batch stopped, and `halt_reason`
    and `notes` say why in words.

    `sync_status` is the *portfolio* sync and is deliberately separate from `status`: a filled
    order that could not be written into the portfolio is a different problem from an order that
    did not fill, and a reader has to be able to tell them apart.
    """

    batch_id: str
    proposal_id: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    currency: str
    cash_at_approval: Decimal
    reserved_cash: Decimal
    price_drift_percent: Decimal | None = None
    notes: list[str] = Field(default_factory=list)
    halt_reason: str | None = None
    sync_status: str
    sync_error: str | None = None
    sync_at: datetime | None = None
    orders: list[OrderIntentOut] = Field(default_factory=list)


class OrdersView(BaseModel):
    """Every batch, newest first, and which one is running.

    History rather than only the latest: a batch that filled yesterday is still what happened, and
    a page that showed only the most recent one would lose it the moment another was approved.
    """

    active_batch_id: str | None = None
    batches: list[ExecutionBatchOut] = Field(default_factory=list)


# --- backtesting -------------------------------------------------------------------------------


class BacktestParamOut(BaseModel):
    """One number a strategy is configured with.

    The strategy declares these, and the page renders a field from each one, so a second
    strategy -- with its own parameters, its own defaults and its own bounds -- needs no change
    to the request shape or to the form. `label` carries the unit in the words a person reads.
    """

    name: str
    label: str
    default: int
    minimum: int
    maximum: int


class BacktestStrategyOut(BaseModel):
    """One predefined strategy, as the application defines it.

    The rules and the execution notes travel with the strategy rather than being written into the
    page: a page that explained the rules itself would keep explaining the old ones after they
    changed, and the whole point of a fixed catalogue is that the definition has one home.

    `parameters` is that home for the numbers too. What a strategy is configured with is a fact
    about the strategy, not about this API or the form, so it is declared once in
    `app.backtest` and served from there.
    """

    id: str
    version: str
    name: str
    summary: str
    rules: list[str]
    execution: list[str]
    parameters: list[BacktestParamOut]
    # The cost bounds the request is validated against, sent so the form can refuse an entry
    # before the round trip. The server re-checks all of them; this is a convenience, not the
    # guard.
    min_commission_per_share: Decimal
    max_commission_per_share: Decimal
    min_slippage_percent: Decimal
    max_slippage_percent: Decimal
    max_initial_capital: Decimal


class BacktestStrategyListOut(BaseModel):
    strategies: list[BacktestStrategyOut] = Field(default_factory=list)


class BacktestInstrumentOut(BaseModel):
    """One listing a search resolved to.

    `exchange` is part of the identity, not a detail: the same ticker lists on several exchanges
    and they are different instruments with different price series. A search result is a listing,
    which is why it is offered as something to choose rather than as a ticker to copy.
    """

    symbol: str
    name: str
    exchange: str
    instrument_type: str
    currency: str
    country: str


class BacktestInstrumentListOut(BaseModel):
    """What a search found, including the query it was asked.

    `query` comes back so the page can tell "this is the result set for what is in the box" from
    "this is what was typed two keystrokes ago". An empty list is an answer -- nothing matched --
    and not the same thing as a failed search, which arrives as an error instead.
    """

    query: str
    matches: list[BacktestInstrumentOut] = Field(default_factory=list)


class BacktestCostsIn(BaseModel):
    """The cost model, in explicit units.

    `commission_per_share` is USD per share, charged on each side of a trade.
    `slippage_percent` is a percentage of the execution price, always against the trade.
    """

    model_config = ConfigDict(extra="forbid")

    commission_per_share: Decimal = Field(default=Decimal("0.005"), ge=Decimal("0"))
    slippage_percent: Decimal = Field(default=Decimal("0.05"), ge=Decimal("0"))


class BacktestRunRequest(BaseModel):
    """One backtest to run.

    **The instrument is a ticker plus an optional exchange, and the exchange is resolved here
    when it is missing.** A portfolio holding carries a ticker and no exchange, so the portfolio
    path has nothing else to send; a chosen search result carries both, so the search path pins
    the listing the person actually picked. Resolving server-side is what keeps the two paths
    from needing a different request shape.

    **No quantity and no price come from the client.** Selecting a holding chooses an instrument
    and nothing else: the run starts from `initial_capital`, flat, because a backtest that
    inherited a position would be measuring something else entirely.

    `request_id` is the client's, and it is the duplicate-suppression mechanism: a retry, a double
    click and a replayed POST carry the same one and resolve to the run that already exists.

    **`parameters` are the strategy's own, named as it names them.** The request does not carry
    "a fast period and a slow period"; it carries whatever the chosen strategy declares, and the
    engine validates the set against that strategy's own definitions. A request naming a
    parameter the strategy does not have is refused rather than ignored, which is what keeps a
    period sent to an RSI run from quietly doing nothing.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)
    strategy_id: str = Field(min_length=1, max_length=48)
    symbol: str = Field(min_length=1, max_length=20)
    # Present when a search result was chosen. Absent on the portfolio path, which knows only the
    # ticker -- and the exchange is then looked up rather than assumed.
    exchange: str | None = Field(default=None, max_length=32)
    start_date: date
    end_date: date
    initial_capital: Decimal = Field(gt=Decimal("0"))
    parameters: dict[str, int] = Field(default_factory=dict)
    costs: BacktestCostsIn = Field(default_factory=BacktestCostsIn)

    @field_validator("strategy_id", "symbol")
    @classmethod
    def _strip(cls, value: str) -> str:
        # Whitespace from a text field is not part of a ticker or a strategy id, and a trailing
        # space would otherwise make a valid symbol unresolvable.
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class BacktestEquityPointOut(BaseModel):
    """One date on a curve, marked at that session's close. The first point of every curve is the
    initial capital, dated the requested start date."""

    on: date
    equity: Decimal


class BacktestTradeOut(BaseModel):
    """One executed trade.

    `price` is the price actually paid or received -- the session's open moved by the slippage
    model -- and `gross_value` is `quantity * price`. The two are separate columns rather than one
    derived from the other, so a page cannot show a slippage-adjusted price beside a gross value
    computed without it.
    """

    executed_on: date
    # "buy" or "sell". Always shown as a word, never as a sign.
    side: str
    quantity: int
    price: Decimal
    gross_value: Decimal
    commission: Decimal
    cash_after: Decimal
    # The session whose close produced this signal. One session earlier than `executed_on` by
    # construction, and stated so the timing rule is visible rather than asserted.
    signalled_on: date


class BacktestOpenPositionOut(BaseModel):
    """A position still held when the test ended, marked rather than sold."""

    quantity: int
    entry_date: date
    entry_price: Decimal
    mark_date: date
    mark_price: Decimal
    market_value: Decimal


class BacktestPerformanceOut(BaseModel):
    """Final value, total return and maximum drawdown for one curve.

    `total_return_percent` is signed. `max_drawdown_percent` is a positive magnitude: a drawdown
    is already a fall, and a minus sign in front of it would be a second one.
    """

    final_value: Decimal
    total_return_percent: Decimal
    max_drawdown_percent: Decimal


class BacktestCoverageOut(BaseModel):
    """Which sessions the run actually read and which of them it reported on.

    `warmup_sessions` are the sessions fetched before the test window and excluded from every
    figure: they exist so the averages have a value on the first reported session. `data_*` covers
    the whole fetched series, warm-up included, which is what was stored as the run's snapshot.
    """

    first_session: date
    last_session: date
    sessions: int
    warmup_sessions: int
    data_first_date: date
    data_last_date: date
    data_sessions: int


class BacktestRunOut(BaseModel):
    """One stored run, complete.

    Every run here finished. A run that failed was reported to whoever asked for it and wrote
    nothing, so there is no `status` and no `failure` to read. Nothing in this schema is computed
    at read time -- what a page shows is what was recorded, which is the only way a stored run and
    a fresh one can be compared.
    """

    run_id: str
    request_id: str
    created_at: datetime

    strategy_id: str
    strategy_version: str
    strategy_name: str

    symbol: str
    exchange: str
    instrument_name: str | None = None
    instrument_type: str | None = None
    currency: str

    start_date: date
    end_date: date
    initial_capital: Decimal
    # The strategy's own parameters as it was run with, so a stored run can be put back into the
    # form that produced it -- whatever those parameters happen to be called.
    parameters: dict[str, int]
    commission_per_share: Decimal
    slippage_percent: Decimal

    provider: str
    adjustment_basis: str
    provider_adjust_mode: str
    data_retrieved_at: datetime

    coverage: BacktestCoverageOut | None = None
    equity: list[BacktestEquityPointOut] = Field(default_factory=list)
    benchmark_equity: list[BacktestEquityPointOut] = Field(default_factory=list)
    trades: list[BacktestTradeOut] = Field(default_factory=list)
    performance: BacktestPerformanceOut | None = None
    benchmark: BacktestPerformanceOut | None = None
    cash_remaining: Decimal | None = None
    open_position: BacktestOpenPositionOut | None = None
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    # The explanation, when one has been written for this run. `None` is the ordinary state: a
    # run is complete without one. The model and prompt version travel with the text because
    # prose is only meaningful beside the request that produced it.
    explanation: str | None = None
    explanation_model: str | None = None
    explanation_prompt_version: str | None = None


class BacktestExplanationOut(BaseModel):
    """The explanation of one run, or why there is not one.

    `status` is the whole answer: `ok`, or `unavailable` with a `reason`. **An explanation that
    could not be written is not an error.** The results are complete without it, so this is a
    200 in both cases and the page keeps the figures on screen either way -- the same shape
    `NewsSearchOut` uses, and for the same reason.

    `reason` is a stable word rather than a sentence, so the page can decide what to offer: a
    provider that timed out is worth offering again, and a missing API key is not. `detail`
    carries the sentence a person can read.
    """

    status: str
    reason: str | None = None
    detail: str | None = None
    explanation: str | None = None
    model: str | None = None
    prompt_version: str | None = None


class BacktestRunView(BaseModel):
    """The latest run, or an explicit null.

    200 with a null body rather than a 404 when nothing has been run: a page restoring itself on
    load is asking "is there one?", and "no" is an answer rather than an error. Same shape as
    `RebalanceProposalView`, for the same reason.

    The stored run carries everything a page needs to put itself back -- the strategy and its
    version, the resolved instrument, the window and costs, and the results -- so restoring is
    one read and no recalculation.
    """

    run: BacktestRunOut | None = None
