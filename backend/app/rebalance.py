"""Target weights in, whole-share trades and two priced portfolios out.

Deliberately free of database and HTTP imports, exactly as `app.valuation` is: plain values in,
plain values out. That is what lets this be tested without PostgreSQL, and what makes "the model
did not calculate any of these numbers" a property of the code rather than a promise.

**The demo policy, stated once.** There is no stored allocation policy in this application and no
recorded risk preference, so this is a policy this milestone chose, disclosed rather than
implied:

* existing long stock holdings plus cash only -- no new tickers, no short positions, no margin;
* whole-share orders, so a fractional holding's remainder stays where it is;
* the agent proposes a target weight per existing holding, in [0, 1]; cash takes the remainder;
* one assumed scenario, applied to the largest holding: a fixed demo shock of -10 percent. It is
  an assumption used to show what a move would do to this portfolio. It is not a forecast, not a
  probability, and not derived from anything the model said.

None of that is a claim about what the user wants. The proposal says which policy produced it.

**Rounding goes one way on purpose.** A buy is rounded *down* and a sell *up*, to whole shares.
Rounding to the nearest share would routinely need half a share more cash than the continuous
target implies, and a plan that cannot be funded is not a better plan for being closer to its
target. Rounding this way means the rounded plan never spends cash the continuous one did not
have. The trades are then still checked against cash, because the clamp below can take that
guarantee away.

**Sells are clamped to the whole shares actually held.** A holding of 10.5 shares can sell 10, and
the 0.5 stays -- it is not rounded away, liquidated, or treated as zero. That clamp can make a
sell smaller than the continuous solution wanted, which is exactly why cash sufficiency is
validated *after* the trades are built rather than argued about before.

**An unfundable plan is refused, not repaired.** If the trades would take cash below zero, this
raises rather than quietly shrinking a buy. A proposal that silently differs from the targets it
was computed against is a proposal nobody can check.

**Two totals, kept apart.** A synchronised portfolio's equity is the broker's own figure, and the
existing valuation uses it as the denominator -- so the "before" side here is the same number
`GET /portfolio/valuation` reports, to the cent. The "after" side cannot use it: the broker's
equity is a fact about the account as it is, not about a hypothetical one. So the after side is
computed arithmetically and the difference between the two bases is reported, once, rather than
being hidden by picking whichever number made the two sides look alike.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.valuation import (
    PricedHolding,
    Scenario,
    Valuation,
    calculate_scenario,
    calculate_valuation,
)

_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")
_ONE = Decimal("1")
_WHOLE = Decimal("1")


class Outcome(StrEnum):
    """What a completed calculation produced. Both are answers; neither is a failure."""

    PROPOSED = "proposed"
    NO_CHANGE = "no_change"


class Action(StrEnum):
    """Which way a trade goes. Always shown as a word, never as a colour alone."""

    BUY = "buy"
    SELL = "sell"


# Why no calculation could be produced. Machine-readable, because the *application* branches on
# one of them (`unfundable` is reported differently from the rest) and a caller should not have to
# match on wording.
REASON_NO_HOLDINGS = "no_holdings"
REASON_NEGATIVE_CASH = "negative_cash"
REASON_INVALID_PRICE = "invalid_price"
REASON_UNPRICED_HOLDINGS = "unpriced_holdings"
REASON_UNKNOWN_TARGET = "unknown_target_symbol"
REASON_INCOMPLETE_TARGETS = "incomplete_targets"
REASON_INVALID_WEIGHT = "invalid_target_weight"
REASON_TARGETS_EXCEED_TOTAL = "targets_exceed_total"
REASON_ZERO_VALUE = "zero_value"
REASON_UNFUNDABLE = "unfundable"

# --- the disclosed demo policy ------------------------------------------------------------

POLICY_NAME = "alphadesk_demo_whole_share"

POLICY_DESCRIPTION = (
    "A demo policy, not the user's stated risk preference and not an optimum. It reallocates "
    "between the positions this portfolio already holds and its cash."
)

POLICY_CONSTRAINTS: tuple[str, ...] = (
    "Existing long stock holdings plus cash only. No new tickers, no short positions, no margin.",
    "Whole-share orders. A fractional holding keeps its remainder.",
    "One target weight per existing holding, between 0 and 1 inclusive; cash takes the "
    "remainder. Weights summing above 1 are refused rather than scaled down.",
    "A buy is rounded down and a sell up to the nearest whole share, so rounding never spends "
    "cash the target did not allow.",
    "The trades are priced at the same prices the portfolio is valued at. They are estimates, "
    "not quotes, and nothing was sent anywhere.",
)

# The one assumed scenario. A fixed constant rather than anything the model produced: an assumed
# shock stated in code can be argued with; one a model invented cannot be checked at all.
SCENARIO_SHOCK = Decimal("-0.10")
SCENARIO_SHOCK_PERCENT = Decimal("-10")

# Cash's line in the schedule. It is not a ticker and can never collide with one, which is what
# lets the schedule be one list rather than a list of holdings plus a special case.
CASH = "CASH"

# Cash is the remainder rather than a proposal, so this sentence is written here and not by the
# agent -- there is no model text to carry for a weight nobody stated.
CASH_REASON = "The remainder after the target weights."

SCENARIO_DESCRIPTION = (
    "Assumed scenario, used to show what a move would do to this portfolio: the largest holding "
    "falls 10 percent while every other price and every quantity stays as it is. This is an "
    "assumption, not a forecast and not a probability, and it is not the reason the target "
    "weights were proposed."
)


class RebalanceUnavailable(Exception):
    """No proposal can be produced from what was given, and why.

    Raised for every state this cash-only, long-only, whole-share workflow cannot express --
    including the ones that are nobody's mistake, such as a margin account's negative cash. A
    caller turns this into a proposal with a status of `unavailable`, never into a plan that looks
    actionable.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class Position:
    """A position on the *after* side. Satisfies `PricedHolding` structurally."""

    symbol: str
    quantity: Decimal


@dataclass(frozen=True)
class ProposedTrade:
    """One whole-share order, priced at the reference price and not sent anywhere."""

    symbol: str
    action: Action
    # Whole shares, always positive. The action carries the direction.
    quantity: Decimal
    reference_price: Decimal
    estimated_value: Decimal
    quantity_before: Decimal
    quantity_after: Decimal

    def as_dict(self) -> dict:
        """The trade as it is stored and shown.

        `str` rather than the normalised form used in the sentences below, so a money value keeps
        the scale it was computed at: "2400.00" is a price and "2400" is a count, and normalising
        here would blur the two on screen.
        """
        return {
            "symbol": self.symbol,
            "action": str(self.action),
            "quantity": str(self.quantity),
            "reference_price": str(self.reference_price),
            "estimated_value": str(self.estimated_value),
            "quantity_before": str(self.quantity_before),
            "quantity_after": str(self.quantity_after),
        }


@dataclass(frozen=True)
class Concentration:
    """The largest single position, which is the one a rebalance is usually about."""

    symbol: str
    allocation_percent: Decimal | None


@dataclass(frozen=True)
class Reconciliation:
    """The broker's total beside the summed one, and the gap between them.

    Reported rather than resolved. `app.valuation` already explains why equity need not equal
    positions plus cash to the cent: the account and its positions are two moments. Which of the
    two is used for which figure is stated in the docstring above, and this is where a reader can
    see the size of the difference for their own portfolio.
    """

    reported_total_value: Decimal | None
    summed_total_value: Decimal
    difference: Decimal | None
    note: str


class Movement(StrEnum):
    """Which way a *target* moves a holding, which is a different question from which way a
    trade goes: a target can rise while no trade is placed, because the rise is smaller than a
    whole share."""

    INCREASE = "increase"
    DECREASE = "decrease"
    RETAIN = "retain"


@dataclass(frozen=True)
class TargetAllocation:
    """One line of the target schedule: where a holding is, where it was asked to go, and where
    the whole-share trades actually leave it.

    Three weights rather than one, because they are three different facts and the difference
    between them is the honest part of a rebalance. `target_percent` is what the agent asked
    for; `achieved_percent` is what whole-share rounding delivers, which is rarely identical;
    and `current_percent` is what the portfolio holds now, which is what makes a movement a
    movement rather than a number in isolation.

    `reason` and `evidence_refs` are the agent's -- the application carries them rather than
    writing them, so a rationale can never be manufactured after the arithmetic. A target whose
    `evidence_refs` is empty is reasoned from the policy and the portfolio's own shape; one with
    references is reasoned from those articles, and the references have already been checked
    against what this run retrieved.
    """

    symbol: str
    current_percent: Decimal | None
    target_percent: Decimal
    achieved_percent: Decimal | None
    movement: Movement
    reason: str
    evidence_refs: tuple[str, ...]

    @property
    def is_cash(self) -> bool:
        return self.symbol == CASH

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "current_percent": _shown(self.current_percent),
            "target_percent": _shown(self.target_percent),
            "achieved_percent": _shown(self.achieved_percent),
            "movement": str(self.movement),
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class Rebalance:
    """A completed calculation: what was proposed, and what it would do."""

    outcome: Outcome
    trades: tuple[ProposedTrade, ...]
    targets: Mapping[str, Decimal]
    allocations: tuple[TargetAllocation, ...]
    cash_target: Decimal
    before: Valuation
    after: Valuation
    cash_before: Decimal
    cash_after: Decimal
    sell_proceeds: Decimal
    buy_cost: Decimal
    # True when the buys could not be paid for out of the cash already in the account, so they
    # depend on the sells above having executed. The proceeds are not available yet.
    buys_depend_on_sells: bool
    largest_before: Concentration
    largest_after: Concentration
    scenario: Scenario
    reconciliation: Reconciliation
    limitations: tuple[str, ...]


def calculate_rebalance(
    holdings: Iterable[PricedHolding],
    cash_balance: Decimal,
    prices: Mapping[str, Decimal],
    targets: Mapping[str, Decimal],
    *,
    reported_total_value: Decimal | None = None,
    reasons: Mapping[str, str] | None = None,
    references: Mapping[str, Sequence[str]] | None = None,
) -> Rebalance:
    """Turn `targets` into whole-share trades and price both sides of them.

    `targets` maps a held symbol to a target weight as a **fraction** -- `Decimal("0.20")` is
    twenty percent, the same convention `calculate_scenario` uses for its change. Every held
    symbol must have an entry: a missing one would silently mean "sell it all", and an agent that
    truncated its reply must not be able to liquidate a position by omission.

    `reasons` and `references` are the agent's own words, keyed by symbol, and they are carried
    onto the schedule rather than used in any arithmetic. They arrive from the *proposal* step,
    which ran before this one: nothing here writes a rationale, so a reason can never be
    manufactured after the numbers it is meant to explain. A symbol with no reason keeps an empty
    one, and the page shows that as an absence rather than filling it in.

    Raises `RebalanceUnavailable` for every state this workflow cannot express. Nothing is
    repaired, scaled or clamped to make an invalid request into a valid one.

    Pure: no database, no network, no writes.
    """
    ordered = sorted(holdings, key=lambda item: item.symbol)
    if not ordered:
        raise RebalanceUnavailable(
            REASON_NO_HOLDINGS,
            "This portfolio holds no positions, so there is nothing to rebalance. A cash-only "
            "account cannot be rebalanced by this workflow, which never opens a new position.",
        )

    held = {item.symbol: item.quantity for item in ordered}
    _require_prices(held, prices)

    if cash_balance < 0:
        raise RebalanceUnavailable(
            REASON_NEGATIVE_CASH,
            f"The account's cash is {_text(cash_balance)}, and this workflow funds purchases "
            "from cash and from the proceeds of its own sales. It has no way to represent a "
            "margin balance, so it will not propose trades against one.",
        )

    weights = _validated_targets(targets, held)

    before = calculate_valuation(
        ordered, cash_balance, prices, reported_total_value=reported_total_value
    )
    if before.total_value <= 0:
        raise RebalanceUnavailable(
            REASON_ZERO_VALUE,
            "This portfolio's total value is zero, so a percentage of it is undefined and "
            "there is no allocation to move toward.",
        )

    trades, cash_after, sell_proceeds, buy_cost = _trades_for(
        ordered, held, prices, weights, before.total_value, cash_balance
    )

    after = calculate_valuation(
        [Position(item.symbol, _after_quantity(item, trades)) for item in ordered],
        cash_after,
        prices,
    )

    outcome = Outcome.NO_CHANGE if not trades else Outcome.PROPOSED
    buys_depend_on_sells = buy_cost > cash_balance
    allocations = _schedule(
        ordered, first=before, second=after, weights=weights,
        reasons=reasons or {}, references=references or {},
    )
    reconciliation = _reconciliation(
        before, cash_balance, prices, ordered, reported_total_value
    )

    return Rebalance(
        outcome=outcome,
        trades=tuple(trades),
        targets=weights,
        allocations=allocations,
        cash_target=_round_2dp(before.total_value * (1 - sum(weights.values(), Decimal("0")))),
        before=before,
        after=after,
        cash_before=before.cash_balance,
        cash_after=after.cash_balance,
        sell_proceeds=sell_proceeds,
        buy_cost=buy_cost,
        buys_depend_on_sells=buys_depend_on_sells,
        largest_before=_largest(before),
        largest_after=_largest(after),
        scenario=_scenario(ordered, cash_balance, prices, before, reported_total_value),
        reconciliation=reconciliation,
        limitations=_limitations(
            outcome=outcome,
            trades=trades,
            buys_depend_on_sells=buys_depend_on_sells,
            cash_balance=cash_balance,
            reconciliation_difference=reconciliation.difference,
        ),
    )


# --- validation ---------------------------------------------------------------------------


def _require_prices(held: Mapping[str, Decimal], prices: Mapping[str, Decimal]) -> None:
    """Every held symbol priced, and every price finite and above zero."""
    unpriced = sorted(symbol for symbol in held if symbol not in prices)
    if unpriced:
        raise RebalanceUnavailable(
            REASON_UNPRICED_HOLDINGS,
            "No price is stored for held symbol(s): "
            + ", ".join(unpriced)
            + ". A quantity computed from a missing price would be a number that looks like a "
            "trade and is not one.",
        )

    for symbol, price in sorted(prices.items()):
        if not price.is_finite() or price <= 0:
            raise RebalanceUnavailable(
                REASON_INVALID_PRICE,
                f"The stored price for {symbol} is {price!r}, which is not a finite number "
                "above zero. Whole-share quantities cannot be computed from it.",
            )


def _validated_targets(
    targets: Mapping[str, Decimal], held: Mapping[str, Decimal]
) -> dict[str, Decimal]:
    """The targets as fractions, or the reason they cannot be used.

    Four refusals, each of which would otherwise become a plausible-looking plan: a weight for
    something not held, a held symbol with no weight, a weight outside [0, 1], and weights that
    together claim more than the whole portfolio.
    """
    unknown = sorted(symbol for symbol in targets if symbol not in held)
    if unknown:
        raise RebalanceUnavailable(
            REASON_UNKNOWN_TARGET,
            "The proposal named symbol(s) this portfolio does not hold: "
            + ", ".join(unknown)
            + ". This workflow never opens a new position, so a target for one cannot be "
            "expressed as trades.",
        )

    missing = sorted(symbol for symbol in held if symbol not in targets)
    if missing:
        raise RebalanceUnavailable(
            REASON_INCOMPLETE_TARGETS,
            "No target weight was given for held symbol(s): "
            + ", ".join(missing)
            + ". A missing weight would mean \"sell it all\", and a truncated reply must not be "
            "able to liquidate a position by omission.",
        )

    weights: dict[str, Decimal] = {}
    for symbol, weight in sorted(targets.items()):
        if not weight.is_finite() or weight < 0 or weight > _ONE:
            raise RebalanceUnavailable(
                REASON_INVALID_WEIGHT,
                f"The target weight for {symbol} was {weight!r}, which is not a fraction "
                "between 0 and 1.",
            )
        weights[symbol] = weight

    total = sum(weights.values(), Decimal("0"))
    if total > _ONE:
        raise RebalanceUnavailable(
            REASON_TARGETS_EXCEED_TOTAL,
            f"The target weights add up to {_text(total * _HUNDRED)} percent of the portfolio. "
            "More than 100 percent cannot be funded without margin, so the targets are refused "
            "rather than scaled down.",
        )

    return weights


# --- the trades ---------------------------------------------------------------------------


def _trades_for(
    ordered: list[PricedHolding],
    held: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    weights: Mapping[str, Decimal],
    total_value: Decimal,
    cash_balance: Decimal,
) -> tuple[list[ProposedTrade], Decimal, Decimal, Decimal]:
    """The whole-share trades, and the cash they leave behind.

    Both directions are computed here rather than in two passes, because the cash check needs the
    complete set: a sale and a purchase of the same size are not independent once the sale is what
    funds the purchase.
    """
    trades: list[ProposedTrade] = []
    sell_proceeds = Decimal("0")
    buy_cost = Decimal("0")

    for item in ordered:
        price = prices[item.symbol]
        target_quantity = total_value * weights[item.symbol] / price
        delta = target_quantity - item.quantity

        if delta > 0:
            # Rounded down: a purchase never costs more than the target allowed for.
            quantity = delta.quantize(_WHOLE, rounding=ROUND_FLOOR)
            action = Action.BUY
        elif delta < 0:
            # Rounded up, then clamped to the whole shares actually held -- the fractional
            # remainder of a position is not a share that can be sold.
            wanted = (-delta).quantize(_WHOLE, rounding=ROUND_CEILING)
            quantity = min(wanted, item.quantity.quantize(_WHOLE, rounding=ROUND_FLOOR))
            action = Action.SELL
        else:
            continue

        if quantity <= 0:
            continue

        value = _round_2dp(quantity * price)
        if action is Action.BUY:
            buy_cost += value
        else:
            sell_proceeds += value

        trades.append(
            ProposedTrade(
                symbol=item.symbol,
                action=action,
                quantity=quantity,
                reference_price=price,
                estimated_value=value,
                quantity_before=item.quantity,
                quantity_after=(
                    item.quantity + quantity
                    if action is Action.BUY
                    else item.quantity - quantity
                ),
            )
        )

    cash_after = _round_2dp(cash_balance + sell_proceeds - buy_cost)
    if cash_after < 0:
        raise RebalanceUnavailable(
            REASON_UNFUNDABLE,
            f"These trades would take cash to {_text(cash_after)}. The purchases cost "
            f"{_text(buy_cost)} and only {_text(cash_balance + sell_proceeds)} would be "
            "available, from cash plus the proposed sales. Nothing was adjusted to make it "
            "fit: an unfundable target is reported as one.",
        )

    return trades, cash_after, _round_2dp(sell_proceeds), _round_2dp(buy_cost)


def _after_quantity(item: PricedHolding, trades: list[ProposedTrade]) -> Decimal:
    for trade in trades:
        if trade.symbol == item.symbol:
            return trade.quantity_after
    return item.quantity


# --- the rest of the answer ----------------------------------------------------------------


def _schedule(
    ordered: list[PricedHolding],
    *,
    first: Valuation,
    second: Valuation,
    weights: Mapping[str, Decimal],
    reasons: Mapping[str, str],
    references: Mapping[str, Sequence[str]],
) -> tuple[TargetAllocation, ...]:
    """Every holding and the cash, before, requested and achieved.

    **The movement is compared at the precision the agent was given.** It was shown each holding's
    share to two decimal places, so it cannot intend a change finer than that -- and comparing the
    exact fractions would report "decrease" for a target that is the number it was shown, because
    the underlying percentage has more digits than the sentence did. That is a rounding artefact
    of the *comparison*, not a threshold: nothing here decides how large a change has to be before
    it counts.

    Cash is a row like any other, and it is the one row whose target the agent does not state. Its
    weight is the remainder, and its reason is written here because there is no agent text to
    carry.
    """
    before = {item.symbol: item for item in first.holdings}
    after = {item.symbol: item for item in second.holdings}

    schedule = [
        TargetAllocation(
            symbol=item.symbol,
            current_percent=before[item.symbol].allocation_percent,
            target_percent=_percent(weights[item.symbol]),
            achieved_percent=after[item.symbol].allocation_percent,
            movement=_movement(
                before[item.symbol].allocation_percent, weights[item.symbol]
            ),
            reason=reasons.get(item.symbol, "").strip(),
            evidence_refs=tuple(references.get(item.symbol, ())),
        )
        for item in ordered
    ]

    cash_weight = Decimal("1") - sum(weights.values(), Decimal("0"))
    schedule.append(
        TargetAllocation(
            symbol=CASH,
            current_percent=first.cash_allocation_percent,
            target_percent=_percent(cash_weight),
            achieved_percent=second.cash_allocation_percent,
            movement=_movement(first.cash_allocation_percent, cash_weight),
            reason=CASH_REASON,
            evidence_refs=(),
        )
    )
    return tuple(schedule)


def _movement(current_percent: Decimal | None, target_weight: Decimal) -> Movement:
    """Which way a target moves a holding, at the precision the agent was shown."""
    if current_percent is None:
        return Movement.RETAIN
    target = _percent(target_weight)
    if target > current_percent:
        return Movement.INCREASE
    if target < current_percent:
        return Movement.DECREASE
    return Movement.RETAIN


def _percent(weight: Decimal) -> Decimal:
    """A fraction as a percentage, to the two decimal places the page shows."""
    return _round_2dp(weight * _HUNDRED)


def _scenario(
    ordered: list[PricedHolding],
    cash_balance: Decimal,
    prices: Mapping[str, Decimal],
    before: Valuation,
    reported_total_value: Decimal | None,
) -> Scenario:
    """The disclosed demo shock on the largest holding, priced by the existing calculation.

    One of the holdings is always the largest, so the symbol is never absent -- the portfolio is
    non-empty and fully priced by the time this runs.
    """
    symbol = _largest(before).symbol
    return calculate_scenario(
        ordered,
        cash_balance,
        prices,
        symbol,
        SCENARIO_SHOCK,
        reported_total_value=reported_total_value,
    )


def _largest(valuation: Valuation) -> Concentration:
    """The holding with the largest value.

    By value rather than by percentage, which is the same ordering on one side of a comparison
    and the only ordering available when the total is zero and every percentage is None.
    """
    largest = max(valuation.holdings, key=lambda item: (item.holding_value, item.symbol))
    return Concentration(symbol=largest.symbol, allocation_percent=largest.allocation_percent)


def _reconciliation(
    before: Valuation,
    cash_balance: Decimal,
    prices: Mapping[str, Decimal],
    ordered: list[PricedHolding],
    reported_total_value: Decimal | None,
) -> Reconciliation:
    """The broker's total beside the summed one, from the unrounded parts.

    Summed here rather than read off `before.holdings_value`, which is a rounded total: a
    difference between two rounded numbers would report the rounding as a reconciliation gap.
    """
    summed = _round_2dp(
        sum((item.quantity * prices[item.symbol] for item in ordered), Decimal("0"))
        + cash_balance
    )
    if reported_total_value is None:
        return Reconciliation(
            reported_total_value=None,
            summed_total_value=summed,
            difference=None,
            note=(
                "This portfolio has no broker-reported equity, so its total is the sum of its "
                "holdings and cash and there is nothing to reconcile against."
            ),
        )

    return Reconciliation(
        reported_total_value=_round_2dp(reported_total_value),
        summed_total_value=summed,
        difference=_round_2dp(reported_total_value - summed),
        note=(
            "The broker's own equity figure is used for the current allocation, as it is "
            "everywhere else in this application, and the proposed allocation is computed from "
            "holdings and cash. A difference between the two bases is the broker's account and "
            "its positions being read at two moments; it is not corrected for here."
        ),
    )


def _limitations(
    *,
    outcome: Outcome,
    trades: list[ProposedTrade],
    buys_depend_on_sells: bool,
    cash_balance: Decimal,
    reconciliation_difference: Decimal | None,
) -> tuple[str, ...]:
    """What a reader has to know to read the numbers above correctly."""
    notes = [
        "Estimated only. Nothing was sent to a broker, no order was placed, and no fill is "
        "modelled. Approval and execution are not available in this build.",
        "Prices are the ones this portfolio is valued at, not live quotes, and they will have "
        "moved by the time anything could be executed.",
    ]

    if outcome is Outcome.NO_CHANGE:
        notes.append(
            "The target weights are close enough to what the portfolio already holds that every "
            "trade rounds to zero whole shares. No change is recommended, which is a result "
            "rather than a failure to produce one."
        )
    else:
        notes.append(
            f"{len(trades)} order(s) are proposed in whole shares. Rounding to whole shares "
            "means the allocation reached is slightly different from the target weights."
        )

    if buys_depend_on_sells:
        notes.append(
            f"The purchases cost more than the {_text(cash_balance)} of cash in the account. "
            "They depend on the proposed sales executing first, and those proceeds are not "
            "available yet -- so a partial fill would leave the purchases unfunded."
        )

    if reconciliation_difference is not None and reconciliation_difference != 0:
        notes.append(
            "The broker's equity and the sum of the holdings and cash differ. The current "
            "allocation is a share of the broker's figure and the proposed allocation is "
            "computed from holdings and cash, so the two sides do not share one denominator."
        )

    return tuple(notes)


# --- formatting ---------------------------------------------------------------------------


def _round_2dp(value: Decimal) -> Decimal:
    """Round to two decimal places, halves away from zero.

    The same rule `app.valuation` uses, and repeated here rather than imported from it: that one
    is a private helper of that module, and a money figure rounded differently in two places
    would be a difference nobody could explain.
    """
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _shown(value: Decimal | None) -> str | None:
    """A value as it is stored and shown, keeping the scale it was computed at.

    `str` rather than the normalised form `_text` produces, for the same reason the trades use
    it: "10.40" is a percentage and "10.4" is the same number spelled as something else.
    """
    return None if value is None else str(value)


def _text(value: Decimal) -> str:
    """A Decimal as a plain string, for a sentence a person reads."""
    return format(value.normalize(), "f")


__all__ = [
    "Action",
    "CASH",
    "CASH_REASON",
    "Concentration",
    "Movement",
    "TargetAllocation",
    "Outcome",
    "POLICY_CONSTRAINTS",
    "POLICY_DESCRIPTION",
    "POLICY_NAME",
    "Position",
    "ProposedTrade",
    "REASON_INCOMPLETE_TARGETS",
    "REASON_INVALID_PRICE",
    "REASON_INVALID_WEIGHT",
    "REASON_NEGATIVE_CASH",
    "REASON_NO_HOLDINGS",
    "REASON_TARGETS_EXCEED_TOTAL",
    "REASON_UNFUNDABLE",
    "REASON_UNKNOWN_TARGET",
    "REASON_UNPRICED_HOLDINGS",
    "REASON_ZERO_VALUE",
    "Rebalance",
    "RebalanceUnavailable",
    "Reconciliation",
    "SCENARIO_DESCRIPTION",
    "SCENARIO_SHOCK",
    "SCENARIO_SHOCK_PERCENT",
    "calculate_rebalance",
]
