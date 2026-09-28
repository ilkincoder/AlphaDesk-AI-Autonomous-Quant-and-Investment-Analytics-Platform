"""Turning an approved proposal into the exact orders that may be sent, and refusing when it may not.

**Nothing here talks to a broker, and nothing here writes.** It takes what the broker said -- an
account, its positions, its open orders and the clock -- and the proposal's own frozen record, and
it answers one question: may these exact orders be sent now, and what are they?

Four rules carry the weight.

* **The quantities are the proposal's, never a client's, and never recomputed.** A plan is built
  from the `calculation` stored on the proposal row. If anything about the portfolio has moved
  enough that those quantities no longer fit, the answer is a refusal -- not an adjusted
  quantity. Silently resizing a trade is the one thing an approval must never survive.
* **Cash, not buying power.** The budget is the account's `cash`, because buying power counts
  margin this workflow does not use. A disclosed buffer is held back on top of it, because a
  market order is not a guaranteed price and an account that spends to its last cent can be
  overdrawn by a tick.
* **Orders already working are counted first.** Cash and shares that another order has already
  committed are not available, whether that order was placed here or directly in Alpaca. Share
  availability is a position's quantity *minus* what open sells have claimed; cash is the balance
  *minus* what open buys will cost.
* **A price that has moved is a different answer from a portfolio that has changed.** The
  portfolio's *shape* -- quantities, cash, currency, price source -- must be untouched, or the
  arithmetic was computed against something that no longer exists and the proposal needs
  regenerating. A price that has merely drifted is a smaller thing, and `DRIFT_TOLERANCE_PERCENT`
  is where the line is drawn: inside it the plan proceeds and the drift is recorded; beyond it the
  approval no longer describes what would be sent, so the person is shown the refreshed estimate
  and asked again.
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DivisionByZero, InvalidOperation

from app.alpaca import BrokerOrder, BrokerSnapshot, ClockReading
from app.models import Portfolio
from app.rebalance_snapshot import Freshness, compare

# --- the disclosed execution policy --------------------------------------------------------

POLICY_NAME = "alphadesk_paper_market_day"

POLICY_STATEMENT = (
    "Whole-share market orders, DAY time in force, regular trading hours only, submitted to the "
    "Alpaca paper account. Sells are submitted first and buys only after every sell has filled, "
    "because the buys may depend on the proceeds."
)

# How far a price may have moved from the proposal's estimate before the approval no longer
# describes what would be sent. One percent: large enough that ordinary intraday movement between
# generating a proposal and approving it does not force a second click, small enough that the
# figure a person approved is still the figure they are getting.
DRIFT_TOLERANCE_PERCENT = Decimal("1.00")

# What is held back from the cash the account reports, so that a market order filling at a worse
# price than the one checked against cannot overdraw it. Two percent, or a dollar, whichever is
# greater -- a percentage of a small buy is no protection at all.
CASH_BUFFER_PERCENT = Decimal("0.02")
CASH_BUFFER_MINIMUM = Decimal("1.00")

# How much of the session must be left for a DAY market order to be worth sending. A DAY order
# submitted in the last minutes before the close is one that expires unfilled, which would look
# like a failure and is really just the wrong time of day to have asked.
MINIMUM_SESSION_REMAINING_SECONDS = 300

_HUNDRED = Decimal("100")

# Why no plan could be resolved. Machine-readable, because the application branches on some of
# them: `renewal_required` is a request for a second click, and the rest are refusals.
REASON_PROPOSAL_NOT_ACTIONABLE = "proposal_not_actionable"
REASON_ACCOUNT_MISMATCH = "account_mismatch"
REASON_CURRENCY_MISMATCH = "currency_mismatch"
REASON_TRADING_RESTRICTED = "trading_restricted"
REASON_MARKET_CLOSED = "market_closed"
REASON_SESSION_TOO_SHORT = "session_too_short"
REASON_PORTFOLIO_CHANGED = "portfolio_changed"
REASON_FRESHNESS_UNKNOWN = "freshness_unknown"
REASON_UNPRICED_SYMBOL = "unpriced_symbol"
REASON_INSUFFICIENT_SHARES = "insufficient_shares"
REASON_INSUFFICIENT_CASH = "insufficient_cash"
REASON_UNBOUNDED_PENDING_ORDER = "unbounded_pending_order"
# Not a refusal: the plan is fine, its *estimates* are stale, and the person is asked again.
REASON_RENEWAL_REQUIRED = "renewal_required"


@dataclass(frozen=True)
class ResolvedOrder:
    """One order this application may send, fully resolved server-side."""

    sequence: int
    symbol: str
    side: str
    quantity: Decimal
    # The fresh broker price at the moment of validation. An estimate, not a quote: a market
    # order fills at whatever the market gives, and nothing here promises otherwise.
    estimated_price: Decimal
    # What the broker reported holding when this plan was validated. Carried so that a submission
    # made later can tell this plan's own fills apart from somebody else's trades: the position it
    # expects to see is this, plus or minus what this order has filled. A position that differs
    # from that is a portfolio that moved underneath the plan, and the remaining orders stop.
    quantity_before: Decimal

    @property
    def estimated_value(self) -> Decimal:
        return (self.quantity * self.estimated_price).quantize(Decimal("0.01"))

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": _shown(self.quantity),
            "estimated_price": _shown(self.estimated_price),
            "estimated_value": _shown(self.estimated_value),
            "quantity_before": _shown(self.quantity_before),
        }


@dataclass(frozen=True)
class ResolvedPlan:
    """The orders that may be sent, and everything that was checked before saying so."""

    orders: tuple[ResolvedOrder, ...]
    fingerprint: str
    proposal_fingerprint: str
    cash_at_approval: Decimal
    reserved_cash: Decimal
    price_drift_percent: Decimal
    estimated_buy_cost: Decimal
    estimated_sell_proceeds: Decimal
    notes: tuple[str, ...]

    @property
    def sells(self) -> tuple[ResolvedOrder, ...]:
        return tuple(order for order in self.orders if order.side == "sell")

    @property
    def buys(self) -> tuple[ResolvedOrder, ...]:
        return tuple(order for order in self.orders if order.side == "buy")

    def as_dict(self) -> list[dict]:
        """The plan as it is stored on the decision row and read back."""
        return [order.as_dict() for order in self.orders]

    def payload(self) -> dict:
        """The whole plan as a JSON-safe dict, for a checkpoint and for an HTTP response.

        Everything is a string or a plain value, because this crosses two boundaries that do not
        carry `Decimal`: LangGraph's checkpointer, which serialises the graph's state, and the
        API's JSON. Storing the plan in the checkpoint is what lets a confirmation resumed in a
        *later process* still know exactly which orders it is confirming.
        """
        return {
            "orders": self.as_dict(),
            "fingerprint": self.fingerprint,
            "proposal_fingerprint": self.proposal_fingerprint,
            "cash_at_approval": _shown(self.cash_at_approval),
            "reserved_cash": _shown(self.reserved_cash),
            "price_drift_percent": _shown(self.price_drift_percent),
            "estimated_buy_cost": _shown(self.estimated_buy_cost),
            "estimated_sell_proceeds": _shown(self.estimated_sell_proceeds),
            "notes": list(self.notes),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ResolvedPlan":
        """Rebuild a plan from `payload()`. The inverse, and the only reader of its shape."""
        orders = tuple(
            ResolvedOrder(
                sequence=int(str(item["sequence"])),
                symbol=str(item["symbol"]),
                side=str(item["side"]),
                quantity=Decimal(str(item["quantity"])),
                estimated_price=Decimal(str(item["estimated_price"])),
                quantity_before=Decimal(str(item.get("quantity_before") or "0")),
            )
            for item in payload.get("orders", [])  # type: ignore[union-attr]
        )
        return cls(
            orders=orders,
            fingerprint=str(payload["fingerprint"]),
            proposal_fingerprint=str(payload.get("proposal_fingerprint") or ""),
            cash_at_approval=Decimal(str(payload["cash_at_approval"])),
            reserved_cash=Decimal(str(payload["reserved_cash"])),
            price_drift_percent=Decimal(str(payload.get("price_drift_percent") or "0")),
            estimated_buy_cost=Decimal(str(payload.get("estimated_buy_cost") or "0")),
            estimated_sell_proceeds=Decimal(str(payload.get("estimated_sell_proceeds") or "0")),
            notes=tuple(str(note) for note in payload.get("notes", [])),  # type: ignore[union-attr]
        )


@dataclass(frozen=True)
class PlanRefusal:
    """Why these orders may not be sent, in a sentence a person can be shown.

    `plan` is present for exactly one reason: `renewal_required`, where the orders are fine and
    the figures the person approved are stale. Every other refusal has nothing to show, because
    there is nothing that could be sent.
    """

    reason: str
    detail: str
    plan: ResolvedPlan | None = None

    @property
    def is_renewal(self) -> bool:
        return self.reason == REASON_RENEWAL_REQUIRED


def resolve(
    *,
    proposal: Mapping[str, object],
    portfolio: Portfolio,
    broker: BrokerSnapshot,
    clock: ClockReading,
    open_orders: Sequence[BrokerOrder],
    now: datetime,
) -> ResolvedPlan | PlanRefusal:
    """The orders this proposal authorises, or why none may be sent.

    Every check happens before anything is recorded, and the checks are ordered cheapest-and-most
    fundamental first: whether this proposal is actionable at all, then whether it is still about
    this account and this portfolio, then whether now is a moment at which orders may be sent, and
    only then the arithmetic.
    """
    actionable = _actionable_orders(proposal)
    if isinstance(actionable, PlanRefusal):
        return actionable

    for refusal in (
        _account_check(portfolio, broker),
        _currency_check(portfolio, broker),
        _eligibility_check(broker),
        _session_check(clock, now),
        _freshness_check(proposal, portfolio),
    ):
        if refusal is not None:
            return refusal

    prices = {position.symbol: position.current_price for position in broker.positions}
    quantities = {position.symbol: position.quantity for position in broker.positions}

    drift = _price_drift(actionable, prices)
    if isinstance(drift, PlanRefusal):
        return drift

    commitment = _commitment(open_orders, prices)

    refusal = _shares_check(actionable, quantities, commitment)
    if refusal is not None:
        return refusal

    funding = _cash_check(actionable, broker, prices, commitment)
    if isinstance(funding, PlanRefusal):
        return funding

    plan = _build(
        actionable=actionable,
        prices=prices,
        proposal=proposal,
        cash=broker.cash,
        reserved=funding,
        drift=drift,
        commitment=commitment,
    )

    if plan.price_drift_percent > DRIFT_TOLERANCE_PERCENT:
        # The orders are sound and the figures on screen are not. Nothing is sent under an
        # approval that describes a different price, and nothing is silently re-priced: the
        # refreshed plan goes back with the refusal for the person to look at.
        return PlanRefusal(
            REASON_RENEWAL_REQUIRED,
            "Prices have moved since this proposal was generated: the largest move is "
            f"{_text(plan.price_drift_percent)}%, beyond the {_text(DRIFT_TOLERANCE_PERCENT)}% "
            "this approval covers. The estimates below are the refreshed ones. Approving again "
            "confirms the plan at these prices; nothing has been sent.",
            plan=plan,
        )

    return plan


# --- the checks ------------------------------------------------------------------------------


def _actionable_orders(proposal: Mapping[str, object]) -> list[dict] | PlanRefusal:
    """The proposal's own trades, if this proposal is one a person could approve.

    A proposal that failed, that found nothing to do, or that is still being generated has no
    orders to send, and there is deliberately no path from one to an approval: the button is not
    offered for them, and this refuses them as well rather than trusting the interface.
    """
    status = str(proposal.get("status") or "")
    if status != "proposed":
        return PlanRefusal(
            REASON_PROPOSAL_NOT_ACTIONABLE,
            f"This proposal is {status or 'not recorded'}, so there is nothing to approve. Only a "
            "proposal that completed with trades can be approved.",
        )

    calculation = proposal.get("calculation")
    if not isinstance(calculation, Mapping):
        return PlanRefusal(
            REASON_PROPOSAL_NOT_ACTIONABLE,
            "This proposal carries no calculation, so the trades it proposed are not recorded "
            "and cannot be resolved into orders.",
        )

    trades = calculation.get("trades")
    if not isinstance(trades, list) or not trades:
        return PlanRefusal(
            REASON_PROPOSAL_NOT_ACTIONABLE,
            "This proposal proposed no trades, so there is nothing to approve.",
        )

    usable: list[dict] = []
    for trade in trades:
        if not isinstance(trade, Mapping):
            return PlanRefusal(
                REASON_PROPOSAL_NOT_ACTIONABLE,
                "This proposal's recorded trades are not readable, so its orders cannot be "
                "resolved. Regenerate the proposal.",
            )
        usable.append(dict(trade))
    return usable


def _account_check(portfolio: Portfolio, broker: BrokerSnapshot) -> PlanRefusal | None:
    """The configured credentials must be the account this portfolio is bound to.

    The same refusal `app.broker_sync` makes, for the same reason and with more riding on it: an
    order sent to a different account than the one the plan was computed for spends money that
    was never approved.
    """
    bound = portfolio.broker_account_id
    if bound is None:
        return PlanRefusal(
            REASON_ACCOUNT_MISMATCH,
            "This portfolio is not linked to an Alpaca account, so there is no account an order "
            "could be placed against. Synchronise the portfolio first (POST /portfolio/sync).",
        )
    if bound != broker.account_id:
        return PlanRefusal(
            REASON_ACCOUNT_MISMATCH,
            f"This portfolio is bound to Alpaca account {bound}, but the configured credentials "
            f"belong to {broker.account_id}. Nothing was sent.",
        )
    return None


def _currency_check(portfolio: Portfolio, broker: BrokerSnapshot) -> PlanRefusal | None:
    if broker.currency != portfolio.currency:
        return PlanRefusal(
            REASON_CURRENCY_MISMATCH,
            f"The Alpaca account reports its figures in {broker.currency}, but this portfolio is "
            f"in {portfolio.currency}. Orders priced against one currency and settled in another "
            "are not something this workflow can size, so nothing was sent.",
        )
    return None


def _eligibility_check(broker: BrokerSnapshot) -> PlanRefusal | None:
    restriction = broker.restriction
    if restriction is None:
        return None
    return PlanRefusal(REASON_TRADING_RESTRICTED, f"{restriction} Nothing was sent.")


def _session_check(clock: ClockReading, now: datetime) -> PlanRefusal | None:
    """Regular trading hours, with enough of the session left to be worth sending into.

    Alpaca's clock rather than a locally computed session: holidays and early closes are the
    exchange calendar's business. Nothing is queued for the next open -- a DAY order sent when
    the market is closed is not an order waiting, it is a decision made against prices that will
    have moved by the time it is acted on.
    """
    if not clock.is_open:
        return PlanRefusal(
            REASON_MARKET_CLOSED,
            "The US market is closed, so nothing can be executed now. Market orders are only "
            "sent during regular trading hours and are never queued overnight. Approve again "
            "once the market opens and the plan has been validated afresh.",
        )

    remaining = (clock.next_close - now).total_seconds()
    if remaining < MINIMUM_SESSION_REMAINING_SECONDS:
        return PlanRefusal(
            REASON_SESSION_TOO_SHORT,
            f"There are about {int(remaining)} seconds left in the session, which is less than "
            f"the {MINIMUM_SESSION_REMAINING_SECONDS} this workflow requires: a DAY order sent "
            "now would expire unfilled. Nothing was sent, and nothing was queued for tomorrow.",
        )
    return None


def _freshness_check(proposal: Mapping[str, object], portfolio: Portfolio) -> PlanRefusal | None:
    """Whether this proposal is still about this portfolio.

    The *shape* is what is compared, and only the shape. A price that ticked is handled by the
    drift check below, which is a much smaller claim; a quantity, the cash, the currency or the
    price source moving is the arithmetic having been computed against something that no longer
    exists.
    """
    snapshot = proposal.get("snapshot") or {}
    if not snapshot:
        return PlanRefusal(
            REASON_FRESHNESS_UNKNOWN,
            "This proposal has no stored snapshot, so there is nothing to check it against the "
            "portfolio as it stands now. It cannot be approved.",
        )

    comparison = compare(snapshot, portfolio)

    if comparison.state is Freshness.PORTFOLIO_CHANGED:
        return PlanRefusal(
            REASON_PORTFOLIO_CHANGED,
            f"{comparison.reason} This proposal needs regenerating before it can be approved; "
            "nothing was sent.",
        )

    if comparison.state is Freshness.UNKNOWN:
        return PlanRefusal(
            REASON_FRESHNESS_UNKNOWN,
            f"{comparison.reason or 'The portfolio could not be read.'} Nothing was sent.",
        )

    return None


def _price_drift(
    trades: Sequence[Mapping[str, object]], prices: Mapping[str, Decimal]
) -> Decimal | PlanRefusal:
    """The largest move, in percent, between the proposal's estimates and the broker's prices.

    Every traded symbol must be priced by the broker. One that is not is a refusal rather than a
    zero drift: an unpriced symbol is a plan that cannot be checked, and checking is the whole
    point.
    """
    largest = Decimal("0")
    for trade in trades:
        symbol = str(trade["symbol"]).upper()
        price = prices.get(symbol)
        if price is None or price <= 0:
            return PlanRefusal(
                REASON_UNPRICED_SYMBOL,
                f"Alpaca reports no usable current price for {symbol}, so the order against it "
                "cannot be checked against the estimate it was approved at. Nothing was sent.",
            )

        reference = _decimal(trade.get("reference_price"))
        if reference is None or reference <= 0:
            return PlanRefusal(
                REASON_PROPOSAL_NOT_ACTIONABLE,
                f"This proposal's recorded price for {symbol} is not usable, so its orders cannot "
                "be re-checked. Regenerate the proposal.",
            )

        try:
            move = abs(price - reference) / reference * _HUNDRED
        except (DivisionByZero, InvalidOperation):  # pragma: no cover - reference > 0 above
            return PlanRefusal(
                REASON_PROPOSAL_NOT_ACTIONABLE,
                f"This proposal's recorded price for {symbol} cannot be compared with the "
                "broker's. Regenerate the proposal.",
            )
        largest = max(largest, move)

    return largest.quantize(Decimal("0.0001"))


@dataclass(frozen=True)
class _Commitment:
    """What other orders have already claimed, per symbol and in cash.

    Includes this application's own working orders as well as any placed directly in Alpaca:
    both spend the same cash and reserve the same shares, and the account does not distinguish
    between them.
    """

    shares: Mapping[str, Decimal]
    cash: Decimal
    unbounded: tuple[str, ...]


def _commitment(
    open_orders: Sequence[BrokerOrder], prices: Mapping[str, Decimal]
) -> _Commitment:
    """What the account's working orders have already committed.

    A sell that is working has claimed the shares it would deliver; a buy that is working has
    claimed the cash it would spend, priced at its limit if it has one and at the broker's
    current price if it does not.

    A market buy with no price to bound it -- for a symbol this account does not hold -- is
    reported as unbounded rather than guessed at. Its cost is genuinely unknown, and an unknown
    cost is not a cost of zero.
    """
    shares: dict[str, Decimal] = {}
    cash = Decimal("0")
    unbounded: list[str] = []

    for order in open_orders:
        if order.side == "sell":
            shares[order.symbol] = shares.get(order.symbol, Decimal("0")) + order.remaining_quantity
            continue

        price = order.limit_price if order.limit_price is not None else prices.get(order.symbol)
        if price is None:
            unbounded.append(order.symbol)
            continue
        cash += order.remaining_quantity * price

    return _Commitment(shares=shares, cash=cash.quantize(Decimal("0.01")), unbounded=tuple(unbounded))


def _shares_check(
    trades: Sequence[Mapping[str, object]],
    quantities: Mapping[str, Decimal],
    commitment: _Commitment,
) -> PlanRefusal | None:
    """Every sell must be deliverable out of what is not already promised elsewhere."""
    for trade in trades:
        if str(trade["action"]) != "sell":
            continue

        symbol = str(trade["symbol"]).upper()
        quantity = _decimal(trade.get("quantity")) or Decimal("0")
        held = quantities.get(symbol, Decimal("0"))
        claimed = commitment.shares.get(symbol, Decimal("0"))
        available = held - claimed

        if available < quantity:
            return PlanRefusal(
                REASON_INSUFFICIENT_SHARES,
                f"{symbol}: this plan would sell {_text(quantity)} shares, but only "
                f"{_text(available)} are available ({_text(held)} held, {_text(claimed)} "
                "already committed to working sell orders). The plan was not resized to fit -- "
                "that would be a different plan than the one proposed. Nothing was sent.",
            )
    return None


def _cash_check(
    trades: Sequence[Mapping[str, object]],
    broker: BrokerSnapshot,
    prices: Mapping[str, Decimal],
    commitment: _Commitment,
) -> Decimal | PlanRefusal:
    """What the plan may spend, or why it may not be funded.

    The budget is **cash**, not buying power: buying power counts margin this workflow does not
    use, and treating it as spendable is how an account that was only ever meant to trade its own
    money ends up borrowing. Working buy orders are subtracted first, then a disclosed buffer is
    held back.
    """
    if commitment.unbounded:
        return PlanRefusal(
            REASON_UNBOUNDED_PENDING_ORDER,
            "A working buy order exists for "
            + ", ".join(sorted(set(commitment.unbounded)))
            + ", which this account does not hold and which carries no limit price. Its cost "
            "cannot be bounded, so what is left for this plan cannot be either. Cancel it in "
            "Alpaca, or wait for it to resolve. Nothing was sent.",
        )

    buy_cost = Decimal("0")
    sell_proceeds = Decimal("0")
    for trade in trades:
        symbol = str(trade["symbol"]).upper()
        price = prices.get(symbol, Decimal("0"))
        value = (_decimal(trade.get("quantity")) or Decimal("0")) * price
        if str(trade["action"]) == "sell":
            sell_proceeds += value
        else:
            buy_cost += value

    buy_cost = buy_cost.quantize(Decimal("0.01"))
    sell_proceeds = sell_proceeds.quantize(Decimal("0.01"))

    buffer = max((buy_cost * CASH_BUFFER_PERCENT).quantize(Decimal("0.01")), CASH_BUFFER_MINIMUM)
    available = broker.cash - commitment.cash

    # The buys are funded by cash plus this plan's own sale proceeds, which is exactly what the
    # calculation assumed when it said the purchases depended on the sales. Whether the proceeds
    # have actually arrived is re-checked before each buy is submitted, against the cash the
    # broker reports then.
    spendable = available + sell_proceeds - buffer
    if buy_cost > spendable:
        return PlanRefusal(
            REASON_INSUFFICIENT_CASH,
            f"This plan would buy {_text(buy_cost)} of stock. The account holds "
            f"{_text(broker.cash)} in cash"
            + (f", with {_text(commitment.cash)} already committed to working buy orders" if commitment.cash else "")
            + f", and the plan's own sales would raise about {_text(sell_proceeds)}. After the "
            f"{_text(buffer)} held back for price movement, that leaves {_text(spendable)}. "
            "Buying power is deliberately not used as the budget: it counts margin this workflow "
            "does not trade on. Nothing was sent.",
        )

    return buffer


def _build(
    *,
    actionable: Sequence[Mapping[str, object]],
    prices: Mapping[str, Decimal],
    proposal: Mapping[str, object],
    cash: Decimal,
    reserved: Decimal,
    drift: Decimal,
    commitment: _Commitment,
) -> ResolvedPlan:
    """Number the orders, sells first, and digest them.

    Sells are numbered before buys because that is the order they are submitted in, and the
    sequence is part of each order's derived client id -- so the identity of a sell does not
    depend on how many buys happened to be in front of it.
    """
    sells = [trade for trade in actionable if str(trade["action"]) == "sell"]
    buys = [trade for trade in actionable if str(trade["action"]) != "sell"]

    orders: list[ResolvedOrder] = []
    for index, trade in enumerate([*sells, *buys], start=1):
        symbol = str(trade["symbol"]).upper()
        orders.append(
            ResolvedOrder(
                sequence=index,
                symbol=symbol,
                side="sell" if str(trade["action"]) == "sell" else "buy",
                quantity=_decimal(trade.get("quantity")) or Decimal("0"),
                estimated_price=prices[symbol],
                quantity_before=_decimal(trade.get("quantity_before")) or Decimal("0"),
            )
        )

    estimated_buy_cost = sum(
        (order.estimated_value for order in orders if order.side == "buy"), Decimal("0")
    )
    estimated_sell_proceeds = sum(
        (order.estimated_value for order in orders if order.side == "sell"), Decimal("0")
    )

    notes = [
        POLICY_STATEMENT,
        f"Prices are the Alpaca paper account's own at validation; the largest move from this "
        f"proposal's estimates is {_text(drift)}%.",
        f"{_text(reserved)} of the account's cash is held back from the buys so that a market "
        "order filling at a worse price than the one checked cannot overdraw the account.",
    ]
    if commitment.cash:
        notes.append(
            f"{_text(commitment.cash)} of the account's cash is already committed to working "
            "buy orders and was counted before this plan's own cost."
        )

    return ResolvedPlan(
        orders=tuple(orders),
        fingerprint=_fingerprint(orders, proposal),
        proposal_fingerprint=str(proposal.get("snapshot_fingerprint") or ""),
        cash_at_approval=cash.quantize(Decimal("0.01")),
        reserved_cash=reserved,
        price_drift_percent=drift,
        estimated_buy_cost=estimated_buy_cost,
        estimated_sell_proceeds=estimated_sell_proceeds,
        notes=tuple(notes),
    )


def _fingerprint(orders: Sequence[ResolvedOrder], proposal: Mapping[str, object]) -> str:
    """A digest of the exact orders, beside the proposal version they came from.

    Quantities and sides are in it, so a plan that differs from another in any trade is a
    different plan -- which is what makes "the approval binds to these orders" checkable rather
    than merely stated.
    """
    parts = [
        f"proposal={proposal.get('proposal_id')}",
        f"snapshot={proposal.get('snapshot_fingerprint')}",
    ]
    for order in orders:
        parts.append(
            f"{order.sequence}:{order.symbol}:{order.side}:{_shown(order.quantity)}:"
            f"market:day:from={_shown(order.quantity_before)}"
        )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _decimal(value: object) -> Decimal | None:
    """A stored decimal string as a `Decimal`, or None when it is not a number at all."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _shown(value: Decimal) -> str:
    """A value as it is stored and shown, keeping the scale it was computed at."""
    return str(value)


def _text(value: Decimal) -> str:
    """A value inside a sentence, in one spelling.

    The same split `app.rebalance` makes, for the same reason: "1000.00" is an amount and "1000"
    is a count, and a sentence reads better with the count.
    """
    return format(value.normalize(), "f")


__all__ = [
    "CASH_BUFFER_MINIMUM",
    "CASH_BUFFER_PERCENT",
    "DRIFT_TOLERANCE_PERCENT",
    "MINIMUM_SESSION_REMAINING_SECONDS",
    "POLICY_NAME",
    "POLICY_STATEMENT",
    "PlanRefusal",
    "REASON_ACCOUNT_MISMATCH",
    "REASON_CURRENCY_MISMATCH",
    "REASON_FRESHNESS_UNKNOWN",
    "REASON_INSUFFICIENT_CASH",
    "REASON_INSUFFICIENT_SHARES",
    "REASON_MARKET_CLOSED",
    "REASON_PORTFOLIO_CHANGED",
    "REASON_PROPOSAL_NOT_ACTIONABLE",
    "REASON_RENEWAL_REQUIRED",
    "REASON_SESSION_TOO_SHORT",
    "REASON_TRADING_RESTRICTED",
    "REASON_UNBOUNDED_PENDING_ORDER",
    "REASON_UNPRICED_SYMBOL",
    "ResolvedOrder",
    "ResolvedPlan",
    "resolve",
]
