"""The portfolio, frozen at one moment -- and whether a stored proposal is still about it.

A proposal is computed from prices and quantities that move. Two things follow, and this module
is both of them.

**The snapshot is taken once and used for the whole run.** The portfolio is synchronised, read,
and then frozen: the model is shown those quantities, the calculation runs over those prices, and
a proposal that re-read the portfolio half way through would be reasoning about one portfolio and
reporting trades for another. The same frozen values are what get stored on the proposal row.

**Whether a proposal is still current is a question about values, not about time.** Comparing
`last_synced_at` would answer it wrongly in both directions: a sync that re-read an unchanged
account moves the timestamp and changes nothing, and a price can move with no sync at all. So
`fingerprint` digests the quantities, prices, cash and price source, and `changes_since` names
what actually differs. A sync timestamp alone is not evidence of anything.

The basis is `app.portfolio_prices`' -- the same one `GET /portfolio/valuation` and the Module 1
portfolio tool use -- so the snapshot cannot value a real position at a demo price, or the
reverse.
"""

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app import portfolio_prices
from app.models import Portfolio
from app.valuation import MissingPriceError, calculate_valuation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotPosition:
    """One held position, at the price the snapshot was valued with."""

    symbol: str
    quantity: Decimal
    price: Decimal
    holding_value: Decimal
    allocation_percent: Decimal | None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "quantity": _shown(self.quantity),
            "price": _shown(self.price),
            "holding_value": _shown(self.holding_value),
            "allocation_percent": _shown(self.allocation_percent),
        }


@dataclass(frozen=True)
class Snapshot:
    """The portfolio as it stood when the run started.

    Immutable, and everything the run needs: the model is shown `describe()`, the calculation
    runs over `as_holdings()`, `prices` and `cash_balance`, and the whole thing is stored on the
    proposal row so a later reader can see exactly what the trades were computed against.

    `reported_total_value` is the broker's own equity where there is one, and it is what the
    *current* allocation is a share of -- the same denominator `GET /portfolio/valuation` uses.
    """

    read_at: datetime
    price_source: str
    last_synced_at: datetime | None
    currency: str
    cash_balance: Decimal
    holdings_value: Decimal
    total_value: Decimal
    reported_total_value: Decimal | None
    cash_allocation_percent: Decimal | None
    prices: Mapping[str, Decimal]
    positions: tuple[SnapshotPosition, ...]
    fingerprint: str

    @classmethod
    def from_dict(cls, stored: Mapping[str, object]) -> "Snapshot":
        """Rebuild the snapshot a proposal was computed from, out of its own stored record.

        This is what a *resumed* run reads. The alternative -- synchronising again -- would
        compute trades for a portfolio the original run never saw, and would silently replace
        the basis under a proposal that is supposed to be a record of one moment.

        The prices come from the stored positions rather than a separate column, because that is
        where the snapshot kept them: a position's price is the price it was valued at.
        """
        positions = tuple(
            SnapshotPosition(
                symbol=str(item["symbol"]),
                quantity=Decimal(str(item["quantity"])),
                price=Decimal(str(item["price"])),
                holding_value=Decimal(str(item["holding_value"])),
                allocation_percent=_optional_decimal(item.get("allocation_percent")),
            )
            for item in stored.get("positions", [])  # type: ignore[union-attr]
        )
        return cls(
            read_at=datetime.fromisoformat(str(stored["read_at"])),
            price_source=str(stored["price_source"]),
            last_synced_at=_optional_datetime(stored.get("last_synced_at")),
            currency=str(stored["currency"]),
            cash_balance=Decimal(str(stored["cash_balance"])),
            holdings_value=Decimal(str(stored["holdings_value"])),
            total_value=Decimal(str(stored["total_value"])),
            reported_total_value=_optional_decimal(stored.get("reported_total_value")),
            cash_allocation_percent=_optional_decimal(
                stored.get("cash_allocation_percent")
            ),
            prices={item.symbol: item.price for item in positions},
            positions=positions,
            fingerprint=str(stored.get("fingerprint", "")),
        )

    def as_holdings(self) -> list["FrozenPosition"]:
        """The positions in the shape `app.rebalance` takes."""
        return [
            FrozenPosition(symbol=item.symbol, quantity=item.quantity)
            for item in self.positions
        ]

    def symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.positions)

    def as_dict(self) -> dict:
        return {
            "read_at": self.read_at.isoformat(),
            "price_source": self.price_source,
            "last_synced_at": (
                None if self.last_synced_at is None else self.last_synced_at.isoformat()
            ),
            "currency": self.currency,
            "cash_balance": _shown(self.cash_balance),
            "holdings_value": _shown(self.holdings_value),
            "total_value": _shown(self.total_value),
            "reported_total_value": _shown(self.reported_total_value),
            "cash_allocation_percent": _shown(self.cash_allocation_percent),
            "positions": [item.as_dict() for item in self.positions],
            "fingerprint": self.fingerprint,
        }

    def describe_for_prompt(self) -> str:
        """The portfolio, for the model that proposes targets.

        Written out rather than dumped as JSON, and it carries two warnings the model needs: the
        prices are a snapshot, and the broker's equity may not equal the holdings plus the cash.
        """
        lines = [
            f"Portfolio: {self.currency}, valued from {self.price_source} prices.",
            f"Cash: {_text(self.cash_balance)}",
            f"Total value used for allocation percentages: {_text(self.total_value)}",
            "",
            "Positions (symbol, quantity, price, value, share of the total):",
        ]
        for item in self.positions:
            share = (
                "undefined"
                if item.allocation_percent is None
                else f"{_text(item.allocation_percent)}%"
            )
            lines.append(
                f"- {item.symbol}: {_text(item.quantity)} at {_text(item.price)} = "
                f"{_text(item.holding_value)} ({share})"
            )
        cash_share = (
            "undefined"
            if self.cash_allocation_percent is None
            else f"{_text(self.cash_allocation_percent)}%"
        )
        lines.append(f"Cash share of the total: {cash_share}")

        if self.reported_total_value is not None:
            lines.append(
                f"The broker reports this account's equity as "
                f"{_text(self.reported_total_value)}, which is the total the percentages above "
                "are a share of. It need not equal the positions plus the cash exactly: the "
                "account and its positions are two moments."
            )
        else:
            lines.append(
                "This portfolio has no broker-reported equity, so the total above is the sum of "
                "its positions and its cash."
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class FrozenPosition:
    """A position as a value. Satisfies `app.rebalance`'s PricedHolding protocol."""

    symbol: str
    quantity: Decimal


def capture(portfolio: Portfolio, *, now: datetime | None = None) -> Snapshot:
    """Read `portfolio` and freeze it.

    Raises `MissingPriceError` if any held symbol has no price, exactly as the valuation does:
    a snapshot that quietly omitted a holding would produce trades against a portfolio that is
    not the one stored.
    """
    basis = portfolio_prices.resolve(portfolio)
    holdings = sorted(portfolio.holdings, key=lambda item: item.symbol)
    read_at = now or datetime.now(timezone.utc)

    valuation = calculate_valuation(
        holdings,
        portfolio.cash_balance,
        basis.prices,
        reported_total_value=basis.reported_total_value,
    )
    by_symbol = {item.symbol: item for item in valuation.holdings}

    positions = tuple(
        SnapshotPosition(
            symbol=row.symbol,
            quantity=row.quantity,
            price=basis.prices[row.symbol],
            holding_value=by_symbol[row.symbol].holding_value,
            allocation_percent=by_symbol[row.symbol].allocation_percent,
        )
        for row in holdings
    )

    return Snapshot(
        read_at=read_at,
        price_source=basis.source,
        last_synced_at=basis.last_synced_at,
        currency=portfolio.currency,
        cash_balance=valuation.cash_balance,
        holdings_value=valuation.holdings_value,
        total_value=valuation.total_value,
        reported_total_value=basis.reported_total_value,
        cash_allocation_percent=valuation.cash_allocation_percent,
        prices=dict(basis.prices),
        positions=positions,
        fingerprint=fingerprint(
            price_source=basis.source,
            cash_balance=valuation.cash_balance,
            reported_total_value=basis.reported_total_value,
            positions=positions,
        ),
    )


class Freshness(StrEnum):
    """How a stored proposal stands against the portfolio now.

    Four answers rather than two, because "this has moved" and "this is wrong" are different
    claims and collapsing them is how a proposal that is still perfectly readable gets thrown
    away. A price that ticked is not a portfolio that changed.
    """

    # Nothing a proposal depends on has moved. However long ago it was generated.
    CURRENT = "current"
    # The portfolio is the same portfolio, at different prices. The proposal is still a faithful
    # estimate of what was proposed against what was held -- it is simply priced at an earlier
    # moment, and executing it would need fresh validation.
    PRICES_UPDATED = "prices_updated"
    # The portfolio is not the same portfolio: a position opened or closed, a quantity or the
    # cash moved, or the account or currency changed. The arithmetic was computed against
    # something that no longer exists, so it needs regenerating.
    PORTFOLIO_CHANGED = "portfolio_changed"
    # It cannot be told, because the portfolio cannot be read or cannot be valued. Not current,
    # and not "changed" either: an outage is not a fact about the holdings.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Comparison:
    """The verdict, why, and which symbols moved."""

    state: Freshness
    reason: str | None
    changed: tuple[str, ...] = ()


def compare(stored: Mapping[str, object], portfolio: Portfolio) -> Comparison:
    """How `stored` stands against the portfolio as it is now.

    **Compared on values, never on timestamps.** `last_synced_at` moves every time the broker is
    read, including when it re-reads an account that has not changed; and a price can move with no
    sync at all. So the quantities, the prices, the cash and the basis are what get compared, and
    the sync time is not consulted.

    **The broker's equity is deliberately not part of the comparison.** A synchronised
    portfolio's current allocation is a share of that figure, so it moves when prices move -- and
    treating it as a structural change would report a portfolio that has merely been re-priced as
    one whose holdings had changed, which is the single mistake this function exists to avoid.
    Equity that moves while every position is the same is a price effect, and it is reported as
    one; equity that moves while nothing at all moved is the two-moments difference
    `app.valuation` documents, and is not a change.

    **The kinds are distinguished before any of them is described**, so a price tick and a closed
    position cannot come out as the same sentence.

    **Nothing here is recalculated.** The stored proposal keeps the numbers it was generated with;
    this says whether they still describe the portfolio, and no more.
    """
    try:
        current = capture(portfolio)
    except MissingPriceError as exc:
        return Comparison(
            Freshness.UNKNOWN,
            "The portfolio's holdings can no longer all be priced (missing: "
            + ", ".join(exc.symbols)
            + "), so this proposal cannot be checked against the portfolio as it stands now. "
            "It is neither confirmed current nor shown as changed.",
            tuple(exc.symbols),
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable portfolio is not a changed one
        logger.warning("the portfolio could not be read to check a proposal: %s", type(exc).__name__)
        return Comparison(
            Freshness.UNKNOWN,
            "The portfolio could not be read, so this proposal's currency cannot be checked. "
            "This is a failure to read the stored data, not a statement that anything changed.",
        )

    structural = _structural_differences(stored, current)
    if structural:
        return Comparison(Freshness.PORTFOLIO_CHANGED, _sentence(structural), tuple(structural))

    priced = _price_differences(stored, current)
    if priced:
        return Comparison(
            Freshness.PRICES_UPDATED,
            "The portfolio holds the same positions in the same quantities, and its valuation "
            "prices have moved since this proposal was generated: "
            + ", ".join(priced)
            + ". The proposal is a faithful estimate of what was proposed against what was "
            "held -- at the prices of its snapshot. Executing anything would need the portfolio "
            "validated afresh.",
            tuple(priced),
        )

    return Comparison(Freshness.CURRENT, None)


def _structural_differences(stored: Mapping[str, object], current: Snapshot) -> list[str]:
    """Everything that means the proposal is about a portfolio that no longer exists."""
    before = {str(item["symbol"]): item for item in _stored_positions(stored)}
    after = {item.symbol: item for item in current.positions}

    differences: list[str] = []
    for symbol in sorted(set(before) | set(after)):
        was, now = before.get(symbol), after.get(symbol)
        if was is None:
            differences.append(f"{symbol} is held now and was not")
        elif now is None:
            differences.append(f"{symbol} is no longer held")
        elif _text(was["quantity"]) != _text(now.quantity):
            differences.append(
                f"{symbol}'s quantity moved from {_display(was['quantity'])} to "
                f"{_display(now.quantity)}"
            )

    if _text(stored.get("cash_balance")) != _text(current.cash_balance):
        differences.append(
            f"cash moved from {_display(stored.get('cash_balance'))} to "
            f"{_display(current.cash_balance)}"
        )
    if stored.get("currency") not in (None, current.currency):
        differences.append(
            f"the portfolio is now in {current.currency}, not {stored.get('currency')}"
        )
    # A different basis is a different valuation, not a price that moved: the whole table behind
    # every figure changed.
    if stored.get("price_source") not in (None, current.price_source):
        differences.append(
            f"the prices are now {current.price_source}, not {stored.get('price_source')}"
        )
    return differences


def _price_differences(stored: Mapping[str, object], current: Snapshot) -> list[str]:
    """Which held symbols are valued at a different price than the snapshot used."""
    before = {str(item["symbol"]): item for item in _stored_positions(stored)}
    moved: list[str] = []
    for position in current.positions:
        was = before.get(position.symbol)
        if was is None:
            # Already reported as a structural change; not a price move.
            continue
        if _text(was.get("price")) != _text(position.price):
            moved.append(
                f"{position.symbol} from {_display(was.get('price'))} to "
                f"{_display(position.price)}"
            )
    return moved


def _sentence(differences: list[str]) -> str:
    return (
        "The portfolio has changed since this proposal was generated, so it needs regenerating: "
        + "; ".join(differences)
        + "."
    )


def _stored_positions(stored: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    positions = stored.get("positions")
    return positions if isinstance(positions, list) else []


def fingerprint(
    *,
    price_source: str,
    cash_balance: Decimal,
    reported_total_value: Decimal | None,
    positions: Sequence[SnapshotPosition],
) -> str:
    """A digest of every value a proposal's arithmetic depends on.

    Quantities are included exactly as stored, fractional remainders included: a remainder is
    part of what the whole-share trades were computed against.
    """
    parts = [
        f"source={price_source}",
        f"cash={cash_balance}",
        f"reported={reported_total_value if reported_total_value is not None else '-'}",
    ]
    for item in sorted(positions, key=lambda entry: entry.symbol):
        parts.append(f"{item.symbol}:{item.quantity}:{item.price}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _optional_decimal(value: object) -> Decimal | None:
    """A stored decimal string as a `Decimal`, or None. Absent is absent, never zero."""
    return None if value is None else Decimal(str(value))


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _shown(value: Decimal | None) -> str | None:
    """A value as it is stored and shown, keeping the scale it was computed at.

    `_text` below normalises, which is what a *comparison* needs and what a money figure must
    not have: "1000.00" is an amount and "1000" is a count, and the record keeps the former.
    """
    return None if value is None else str(value)


def _display(value: object) -> str:
    """A value as a person reads it, keeping the scale it was stored or computed at.

    Separate from `_text` below, which normalises because a *comparison* needs one spelling for
    one number. A sentence is read rather than compared, and "450.00 to 500.00" is what a price
    looks like -- collapsing it to "450 to 500" would turn money into a count.
    """
    return "unknown" if value is None else str(value)


def _text(value: object) -> str:
    """A stored or computed value, in one spelling.

    Both sides of a comparison pass through here, and that is the point: a value read back off
    the row is a *string* ("50.000000") while the same value computed now is a `Decimal`, and
    comparing those two directly would report a difference between a number and itself. Numeric
    strings are parsed and normalised; anything that is not a number is left as it is.
    """
    if value is None:
        return "unknown"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


__all__ = [
    "Comparison",
    "Freshness",
    "FrozenPosition",
    "Snapshot",
    "SnapshotPosition",
    "capture",
    "compare",
    "fingerprint",
]
