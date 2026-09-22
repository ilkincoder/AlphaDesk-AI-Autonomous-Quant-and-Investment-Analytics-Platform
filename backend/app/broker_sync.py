"""Synchronise the AlphaDesk portfolio with the Alpaca paper account. Writes only.

One direction, always: the broker is authoritative for holdings, cash and equity, and
PostgreSQL keeps the latest successful snapshot. Nothing here sends anything to the broker.

**The fetch and the write are deliberately separate.** `fetch_snapshot` touches the network
and no database; `apply_snapshot` takes a session and never touches the network. That is
what lets the whole result be validated before a single row changes -- one atomic update
covering cash, equity, the link and every position, or no update at all. It is also what
makes the interesting cases testable without a broker.

Three rules carry the weight:

* **Nothing partial is ever written.** A validation failure, an unreachable broker or a
  refused account leaves the previous snapshot exactly as it was, timestamp included. The
  portfolio then reads as stale, which is what it is.
* **Positions are upserted and removed within one portfolio.** Repeating a sync cannot
  duplicate a holding -- the unique key on (portfolio_id, symbol) would refuse that
  anyway -- and a symbol absent from a *complete* response is a closed position, so it is
  deleted. An empty positions list is a valid answer meaning zero holdings.
* **An older fetch never overwrites a newer one.** `started_at` is when the broker read
  began, and a snapshot that began before the stored `last_synced_at` is refused. Without
  that, two overlapping syncs could finish out of order and the slower one would win.

The module holds no state and opens no transaction of its own: `apply_snapshot` writes
into the caller's transaction, so the caller decides when it commits.
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alpaca import BROKER, BrokerSnapshot, fetch_snapshot
from app.models import Holding, Portfolio
# One row, two names, and this module is what changes the name -- see `app.portfolio_identity`.
from app.portfolio_identity import BROKER_PORTFOLIO_NAME, DEMO_PORTFOLIO_NAME, find

logger = logging.getLogger(__name__)


class SyncError(Exception):
    """Base class for failures this module reports."""


class PortfolioNotStoredError(SyncError):
    """There is no portfolio row to synchronise into."""


class AccountMismatchError(SyncError):
    """The portfolio is already bound to a different broker account.

    A refusal, not a rebind. Switching the account would relabel every stored figure as
    belonging to something else, and nothing in the response would say so.
    """


class CurrencyMismatchError(SyncError):
    """The account's currency is not the portfolio's, so its figures are not its figures."""


class SyncInProgressError(SyncError):
    """Another sync is running in this process. Rejected, not queued."""


@dataclass(frozen=True)
class SyncOutcome:
    """What one sync call did, and what the portfolio holds afterwards.

    `applied` is False when a newer snapshot was already stored and this one was refused
    for being older. The figures reported are then the *stored* ones, because those are
    what the portfolio actually holds -- reporting the refused snapshot's numbers here
    would describe a state the database is not in.
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


# One sync at a time per process. The API runs as sync `def` handlers, so a second request
# arrives on another thread of the threadpool and this is what stops it piling on. It is
# per process, like the analysis semaphore: two uvicorn workers would admit two.
_SYNC_LOCK = threading.Lock()


@contextmanager
def _exclusive() -> Iterator[None]:
    if not _SYNC_LOCK.acquire(blocking=False):
        raise SyncInProgressError(
            "A portfolio sync is already running. Nothing was started, and the stored "
            "snapshot is unchanged."
        )
    try:
        yield
    finally:
        _SYNC_LOCK.release()


def sync(session: Session, *, now: datetime | None = None) -> SyncOutcome:
    """Read the broker, then apply the result in one transaction.

    The fetch happens with no transaction open and no row locked, for the same reason the
    analysis flow does not hold one across a model call: a slow provider should not hold a
    database connection hostage.
    """
    with _exclusive():
        snapshot = fetch_snapshot(now=now)
        # `session.begin()` rather than a commit inside `apply_snapshot`: the caller owns
        # the transaction boundary, and this is the one place that boundary is drawn.
        with session.begin():
            return apply_snapshot(session, snapshot)


def apply_snapshot(session: Session, snapshot: BrokerSnapshot) -> SyncOutcome:
    """Write one broker snapshot into the portfolio. Writes into the caller's transaction.

    Raises `PortfolioNotStoredError`, `AccountMismatchError` or `CurrencyMismatchError`
    before anything is written.
    """
    portfolio = find(session, for_update=True)

    if portfolio is None:
        raise PortfolioNotStoredError(
            f"No portfolio named {DEMO_PORTFOLIO_NAME!r} is stored, so there is nothing "
            "to synchronise into. Run: docker compose exec backend python -m app.seed"
        )

    _require_same_account(portfolio, snapshot)
    _require_same_currency(portfolio, snapshot)

    # Read here rather than through `portfolio.holdings`. A relationship collection is
    # loaded once and cached on the instance, and rows this module has just inserted are
    # not in it -- so a second sync in one session would compare against the holdings as
    # they were before the first, and re-insert what is already there. A query is the
    # rows as they are.
    stored = {
        holding.symbol: holding
        for holding in session.scalars(
            select(Holding).where(Holding.portfolio_id == portfolio.id)
        )
    }

    if not _is_newer_than_stored(portfolio, snapshot):
        logger.info(
            "refused a snapshot that began before the stored one (portfolio %s)",
            portfolio.id,
        )
        return _stored_outcome(portfolio, applied=False, position_count=len(stored))

    _write_holdings(session, stored, portfolio, snapshot)

    portfolio.cash_balance = snapshot.cash
    portfolio.broker_equity = snapshot.equity
    portfolio.broker = BROKER
    portfolio.broker_account_id = snapshot.account_id
    portfolio.last_synced_at = snapshot.started_at
    # The rename happens here, with the link, and only ever once: the name says what the
    # row is, and until this moment it was the placeholder portfolio. A migration cannot
    # make that judgement -- it runs against a database it knows nothing about -- whereas
    # this code has just read the account the row now belongs to.
    portfolio.name = BROKER_PORTFOLIO_NAME

    session.flush()

    return _stored_outcome(portfolio, applied=True, position_count=snapshot.position_count)


def _require_same_account(portfolio: Portfolio, snapshot: BrokerSnapshot) -> None:
    bound = portfolio.broker_account_id
    if bound is not None and bound != snapshot.account_id:
        raise AccountMismatchError(
            f"This portfolio is bound to Alpaca account {bound}, but the configured "
            f"credentials belong to {snapshot.account_id}. Nothing was written. Point "
            "ALPACA_API_KEY_ID at the bound account, or clear the binding deliberately."
        )


def _require_same_currency(portfolio: Portfolio, snapshot: BrokerSnapshot) -> None:
    if snapshot.currency != portfolio.currency:
        raise CurrencyMismatchError(
            f"The Alpaca account reports its figures in {snapshot.currency}, but this "
            f"portfolio is in {portfolio.currency}. Storing one as the other would "
            "mislabel every amount, so nothing was written."
        )


def _is_newer_than_stored(portfolio: Portfolio, snapshot: BrokerSnapshot) -> bool:
    """True when this snapshot began at or after the stored one.

    The comparison is on `started_at` rather than on when a fetch finished, because a
    fetch that started first but returned last is *older* data. Equal timestamps are
    accepted: they mean the same instant read twice, which changes nothing.
    """
    stored = portfolio.last_synced_at
    if stored is None:
        return True
    if stored.tzinfo is None:
        # A naive value can only come from a hand-written row or a driver that dropped
        # the offset. Treated as UTC rather than guessed at, because the alternative is
        # comparing it against an aware value and raising deep inside the sync.
        stored = stored.replace(tzinfo=timezone.utc)
    return snapshot.started_at >= stored


def _write_holdings(
    session: Session,
    stored: dict[str, Holding],
    portfolio: Portfolio,
    snapshot: BrokerSnapshot,
) -> None:
    """Upsert every reported position, and delete this portfolio's rows for any it no
    longer reports."""
    reported = set()

    for position in snapshot.positions:
        reported.add(position.symbol)
        holding = stored.get(position.symbol)
        if holding is None:
            session.add(
                Holding(
                    portfolio_id=portfolio.id,
                    symbol=position.symbol,
                    quantity=position.quantity,
                    average_buy_price=position.average_entry_price,
                    market_price=position.current_price,
                    market_value=position.market_value,
                )
            )
            continue

        # Updated in place rather than deleted and re-inserted: the row's id is what a
        # later feature will hang history from, and re-creating it every thirty seconds
        # would make that history impossible to write.
        holding.quantity = position.quantity
        holding.average_buy_price = position.average_entry_price
        holding.market_price = position.current_price
        holding.market_value = position.market_value

    # A closed position. Only within this portfolio, and only after a complete fetch --
    # which is what the caller has already established by getting this far.
    for symbol, holding in stored.items():
        if symbol not in reported:
            session.delete(holding)


def _stored_outcome(
    portfolio: Portfolio, *, applied: bool, position_count: int
) -> SyncOutcome:
    """What the portfolio holds after the call, read off the rows rather than the
    snapshot: on the refused path those are different numbers, and the rows are the ones
    that are true."""
    return SyncOutcome(
        applied=applied,
        portfolio_id=portfolio.id,
        broker=portfolio.broker,
        broker_account_id=portfolio.broker_account_id,
        last_synced_at=portfolio.last_synced_at,
        currency=portfolio.currency,
        cash_balance=portfolio.cash_balance,
        equity=portfolio.broker_equity,
        position_count=position_count,
    )


__all__ = [
    "AccountMismatchError",
    "CurrencyMismatchError",
    "PortfolioNotStoredError",
    "SyncError",
    "SyncInProgressError",
    "SyncOutcome",
    "apply_snapshot",
    "sync",
]
