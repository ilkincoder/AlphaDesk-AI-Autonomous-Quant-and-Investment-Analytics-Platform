"""Doubles for the execution tests: a broker that answers from a script, and a session redirect.

The execution tests exercise the real validation, the real workflow, the real database and the
real sequencing rules. Two things are replaced, and both are things that must not be real in a
test:

* **The broker.** `FakeBroker` answers the four reads and the one write from dictionaries, and
  records every submission it was asked to make. A test that submitted to Alpaca would place an
  order, and the whole point of these tests is to check the logic without one.
* **The session factory.** Every store module opens `app.db.SessionLocal`. Pointing those at the
  test's rolled-back transaction is what keeps a test from writing decisions and batches into the
  development database and then asserting against rows nothing else can see.

The proposal a test approves is built by `a_proposal`, which produces the same *mapping* the
stores return -- a frozen snapshot, and a calculation whose trades are the ones that get resolved.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import contextlib
from collections.abc import Iterator, Sequence
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest import mock

from sqlalchemy.orm import Session

from app.alpaca import BrokerOrder, BrokerPosition, BrokerSnapshot, ClockReading
from app.models import RebalanceProposal
from app.rebalance_snapshot import capture

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "acct-1"


# --- the proposal a test approves ---------------------------------------------------------------


def a_trade(
    symbol: str,
    action: str,
    quantity: str,
    price: str,
    *,
    quantity_before: str = "50",
) -> dict:
    """One trade in the shape the calculation stores.

    `reference_price` is what the proposal estimated at, and `quantity_before` is what the account
    held when it did -- both are the fields the execution path resolves orders from.
    """
    before = Decimal(quantity_before)
    delta = Decimal(quantity)
    return {
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "reference_price": price,
        "estimated_value": str((delta * Decimal(price)).quantize(Decimal("0.01"))),
        "quantity_before": quantity_before,
        "quantity_after": str(before - delta if action == "sell" else before + delta),
    }


def a_sell_and_a_buy() -> list[dict]:
    """A ten-share sale of AAPL and a two-share purchase of MSFT, priced as the account is.

    The shape almost every test needs: one of each side, small enough that the arithmetic is
    checkable by hand.
    """
    return [
        a_trade("AAPL", "sell", "10", "200.00"),
        a_trade("MSFT", "buy", "2", "450.00", quantity_before="10"),
    ]


def a_proposal(
    portfolio,
    *,
    trades: Sequence[dict] | None = None,
    status: str = "proposed",
    proposal_id: str = "prop-1",
) -> dict:
    """A stored proposal, as `app.proposals.get` returns one.

    The snapshot is the real `capture` of the portfolio, so the freshness comparison in the
    execution path is comparing genuine values rather than a fixture that happens to agree with
    them.
    """
    snapshot = capture(portfolio, now=NOW)
    return {
        "proposal_id": proposal_id,
        "request_id": f"req-{proposal_id}",
        "status": status,
        "snapshot": snapshot.as_dict(),
        "snapshot_fingerprint": snapshot.fingerprint,
        "calculation": a_calculation(portfolio, list(trades or [])),
        "targets": None,
        "policy": {},
        "evidence": [],
        "assumptions": [],
        "limitations": [],
        "failure": None,
        "failure_reason": None,
    }


def a_calculation(portfolio, trades: Sequence[dict]) -> dict:
    """A stored calculation, built from the portfolio and the trades it proposes.

    Written out rather than produced by running the real calculation -- that is checked in
    `test_rebalance.py`. What matters here is that it is *self-consistent*: the before side is
    the portfolio, the after side is the portfolio with the trades applied, and the totals follow
    from them. A fixture whose numbers did not add up would make the proposal endpoint's
    validation the only thing under test.

    The full shape is needed even by the tests that only care about the trades, because the
    proposal endpoint validates the whole record before it will return one.
    """
    cash_before = portfolio.cash_balance
    sell_proceeds = Decimal("0")
    buy_cost = Decimal("0")
    after_quantities = {holding.symbol: holding.quantity for holding in portfolio.holdings}

    for trade in trades:
        symbol = str(trade["symbol"])
        quantity = Decimal(str(trade["quantity"]))
        value = (quantity * Decimal(str(trade["reference_price"]))).quantize(Decimal("0.01"))
        if str(trade["action"]) == "sell":
            sell_proceeds += value
            after_quantities[symbol] = after_quantities.get(symbol, Decimal("0")) - quantity
        else:
            buy_cost += value
            after_quantities[symbol] = after_quantities.get(symbol, Decimal("0")) + quantity

    cash_after = cash_before + sell_proceeds - buy_cost

    def side(holdings) -> list[dict]:
        rows = []
        total = sum(
            (holding.quantity * (holding.market_price or Decimal("0")) for holding in holdings),
            Decimal("0"),
        ) + (cash_before if holdings is portfolio.holdings else cash_after)
        for holding in holdings:
            value = holding.quantity * (holding.market_price or Decimal("0"))
            rows.append(
                {
                    "symbol": holding.symbol,
                    "quantity": str(holding.quantity),
                    "price": str(holding.market_price),
                    "holding_value": str(value.quantize(Decimal("0.01"))),
                    "allocation_percent": str(
                        (value / total * 100).quantize(Decimal("0.01")) if total else 0
                    ),
                }
            )
        return rows

    before_rows = side(portfolio.holdings)
    after_holdings = [
        _a_holding(holding, after_quantities[holding.symbol], holding.market_price)
        for holding in portfolio.holdings
    ]
    after_rows = side(after_holdings)

    def total_of(rows, cash) -> Decimal:
        return sum((Decimal(row["holding_value"]) for row in rows), Decimal("0")) + cash

    total_before = total_of(before_rows, cash_before)
    total_after = total_of(after_rows, cash_after)
    largest_before = max(before_rows, key=lambda row: Decimal(row["holding_value"]))
    largest_after = max(after_rows, key=lambda row: Decimal(row["holding_value"]))
    shock = Decimal("-10")
    shocked = Decimal(largest_before["price"]) * (1 + shock / 100)

    return {
        "outcome": "proposed",
        "trades": list(trades),
        "targets": {row["symbol"]: str(Decimal("0.10")) for row in before_rows},
        "cash_target": str(cash_after.quantize(Decimal("0.01"))),
        "before": {
            "total_value": str(total_before.quantize(Decimal("0.01"))),
            "cash_balance": str(cash_before.quantize(Decimal("0.01"))),
            "cash_allocation_percent": _share(cash_before, total_before),
            "holdings": before_rows,
        },
        "after": {
            "total_value": str(total_after.quantize(Decimal("0.01"))),
            "cash_balance": str(cash_after.quantize(Decimal("0.01"))),
            "cash_allocation_percent": _share(cash_after, total_after),
            "holdings": after_rows,
        },
        "cash_before": str(cash_before.quantize(Decimal("0.01"))),
        "cash_after": str(cash_after.quantize(Decimal("0.01"))),
        "sell_proceeds": str(sell_proceeds.quantize(Decimal("0.01"))),
        "buy_cost": str(buy_cost.quantize(Decimal("0.01"))),
        "buys_depend_on_sells": buy_cost > cash_before,
        "largest_before": {
            "symbol": largest_before["symbol"],
            "allocation_percent": largest_before["allocation_percent"],
        },
        "largest_after": {
            "symbol": largest_after["symbol"],
            "allocation_percent": largest_after["allocation_percent"],
        },
        "scenario": {
            "symbol": largest_before["symbol"],
            "price_change_percent": str(shock),
            "price_before": largest_before["price"],
            "price_after": str(shocked.quantize(Decimal("0.01"))),
            "holding_value_before": largest_before["holding_value"],
            "holding_value_after": str(
                (
                    Decimal(largest_before["quantity"]) * shocked
                ).quantize(Decimal("0.01"))
            ),
            "total_value_before": str(total_before.quantize(Decimal("0.01"))),
            "total_value_after": str(
                (
                    total_before
                    + Decimal(largest_before["quantity"]) * shocked
                    - Decimal(largest_before["holding_value"])
                ).quantize(Decimal("0.01"))
            ),
            "change_value": str(
                (
                    Decimal(largest_before["quantity"]) * shocked
                    - Decimal(largest_before["holding_value"])
                ).quantize(Decimal("0.01"))
            ),
            "change_percent": _share(
                Decimal(largest_before["quantity"]) * shocked
                - Decimal(largest_before["holding_value"]),
                total_before,
            ),
        },
        "reconciliation": {
            "reported_total_value": (
                None if portfolio.broker_equity is None else str(portfolio.broker_equity)
            ),
            "summed_total_value": str(total_before.quantize(Decimal("0.01"))),
            "difference": (
                None
                if portfolio.broker_equity is None
                else str((portfolio.broker_equity - total_before).quantize(Decimal("0.01")))
            ),
            "note": "The broker's figure beside the summed one, reported rather than resolved.",
        },
    }


def _a_holding(holding, quantity: Decimal, price: Decimal):
    """A copy of a holding at a new quantity, for the after side of a calculation."""
    return SimpleNamespace(symbol=holding.symbol, quantity=quantity, market_price=price)


def _share(part: Decimal, total: Decimal) -> str | None:
    if not total:
        return None
    return str((part / total * 100).quantize(Decimal("0.01")))


def store_proposal(session: Session, proposal: dict) -> RebalanceProposal:
    """Insert the proposal a decision will be recorded against."""
    row = RebalanceProposal(
        id=str(proposal["proposal_id"]),
        request_id=str(proposal["request_id"]),
        status=str(proposal["status"]),
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW,
        thread_id=f"rebalance-{proposal['proposal_id']}",
        snapshot=proposal["snapshot"],
        snapshot_fingerprint=str(proposal["snapshot_fingerprint"]),
        policy=proposal["policy"],
        targets=proposal.get("targets"),
        evidence=proposal["evidence"],
        calculation=proposal["calculation"],
        assumptions=proposal["assumptions"],
        limitations=proposal["limitations"],
    )
    session.add(row)
    session.flush()
    return row


# --- the broker ---------------------------------------------------------------------------------


class FakeBroker:
    """A paper account that answers from dictionaries and remembers what it was asked to do.

    Every submission is recorded in `submissions`, so a test can assert that one order was sent
    rather than two -- which is the assertion most of these tests exist to make.
    """

    def __init__(
        self,
        *,
        account_id: str = ACCOUNT_ID,
        currency: str = "USD",
        cash: str = "10000.00",
        positions: dict[str, tuple[str, str]] | None = None,
        is_open: bool = True,
        status: str = "ACTIVE",
        trading_blocked: bool = False,
        account_blocked: bool = False,
        trade_suspended_by_user: bool = False,
        next_close: datetime | None = None,
    ) -> None:
        self.account_id = account_id
        self.currency = currency
        self.cash = Decimal(cash)
        # symbol -> (quantity, current price)
        self.positions = positions if positions is not None else {}
        self.is_open = is_open
        self.status = status
        self.trading_blocked = trading_blocked
        self.account_blocked = account_blocked
        self.trade_suspended_by_user = trade_suspended_by_user
        self.next_close = next_close or (NOW + timedelta(hours=1))

        # client_order_id -> BrokerOrder, as the broker would report it.
        self.orders: dict[str, BrokerOrder] = {}
        # What every submission asked for, in order. The duplicate-prevention assertions read this.
        self.submissions: list[dict[str, Any]] = []
        # Working orders that are not this application's -- placed directly in Alpaca.
        self.external_open: list[BrokerOrder] = []
        # Raised instead of answering the next submission, once. `None` means "answer normally".
        self.fail_next_submission: Exception | None = None
        # When set, a submission answers with this order without recording a fill.
        self.submission_status = "accepted"

    # --- the reads ------------------------------------------------------------------------

    def snapshot(self, **_: Any) -> BrokerSnapshot:
        return BrokerSnapshot(
            started_at=NOW,
            account_id=self.account_id,
            account_number="PA3TESTACCOUNT",
            currency=self.currency,
            cash=self.cash,
            equity=self.cash,
            status=self.status,
            trading_blocked=self.trading_blocked,
            account_blocked=self.account_blocked,
            trade_suspended_by_user=self.trade_suspended_by_user,
            positions=tuple(
                BrokerPosition(
                    symbol=symbol,
                    quantity=Decimal(quantity),
                    average_entry_price=Decimal(price),
                    current_price=Decimal(price),
                    market_value=(Decimal(quantity) * Decimal(price)).quantize(Decimal("0.01")),
                )
                for symbol, (quantity, price) in sorted(self.positions.items())
            ),
        )

    def clock(self, **_: Any) -> ClockReading:
        return ClockReading(
            timestamp=NOW,
            is_open=self.is_open,
            next_open=NOW + timedelta(days=1),
            next_close=self.next_close,
        )

    def by_client_id(self, client_order_id: str, **_: Any) -> BrokerOrder | None:
        return self.orders.get(client_order_id)

    def open_orders(self, **_: Any) -> tuple[BrokerOrder, ...]:
        mine = tuple(
            order
            for order in self.orders.values()
            if order.status in ("new", "accepted", "partially_filled")
        )
        return (*mine, *self.external_open)

    # --- the write ------------------------------------------------------------------------

    def submit(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        order_type: str = "market",
        time_in_force: str = "day",
        **_: Any,
    ) -> BrokerOrder:
        self.submissions.append(
            {
                "client_order_id": client_order_id,
                "symbol": symbol,
                "side": side,
                "quantity": str(quantity),
                "type": order_type,
                "time_in_force": time_in_force,
            }
        )

        if self.fail_next_submission is not None:
            failure, self.fail_next_submission = self.fail_next_submission, None
            raise failure

        order = BrokerOrder(
            order_id=f"broker-{len(self.submissions)}",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            filled_quantity=Decimal("0"),
            filled_avg_price=None,
            status=self.submission_status,
            order_type=order_type,
            time_in_force=time_in_force,
            limit_price=None,
            submitted_at=NOW,
        )
        self.orders[client_order_id] = order
        return order

    # --- what a test drives ---------------------------------------------------------------

    def receive(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: str,
        status: str = "accepted",
    ) -> BrokerOrder:
        """Put an order at the broker without this application having just sent one.

        The distinction `submissions` exists to keep. An order can be at the broker because a POST
        arrived and its answer was lost -- that is the case this models -- and recording it as a
        new submission would make a test about duplicate prevention count its own fixture.
        """
        order = BrokerOrder(
            order_id=f"broker-received-{client_order_id}",
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            quantity=Decimal(quantity),
            filled_quantity=Decimal("0"),
            filled_avg_price=None,
            status=status,
            order_type="market",
            time_in_force="day",
            limit_price=None,
            submitted_at=NOW,
        )
        self.orders[client_order_id] = order
        return order

    def fill(self, client_order_id: str, *, quantity: str | None = None, price: str | None = None):
        """Fill a submitted order, in whole or in part, and move the account with it.

        The cash and the position move too, because they are what the buy path revalidates
        against: a broker that reported a fill without moving the money would make every
        affordability check a test of nothing.

        The price defaults to the symbol's own, so a test that does not care about a price move
        does not have to restate it -- and an assertion about cash cannot be thrown off by a
        default that no market would have produced.
        """
        order = self.orders[client_order_id]
        if price is None:
            price = self.positions.get(order.symbol, ("0", "100.00"))[1]
        filled = Decimal(quantity) if quantity is not None else order.quantity
        remaining = order.quantity - filled

        self.orders[client_order_id] = BrokerOrder(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            filled_quantity=filled,
            filled_avg_price=Decimal(price),
            status="filled" if remaining == 0 else "partially_filled",
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            limit_price=None,
            submitted_at=order.submitted_at,
        )

        held, current = self.positions.get(order.symbol, ("0", price))
        if order.side == "sell":
            self.positions[order.symbol] = (str(Decimal(held) - filled), current)
            self.cash += filled * Decimal(price)
        else:
            self.positions[order.symbol] = (str(Decimal(held) + filled), current)
            self.cash -= filled * Decimal(price)

    def reject(self, client_order_id: str, *, status: str = "rejected"):
        order = self.orders[client_order_id]
        self.orders[client_order_id] = BrokerOrder(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            filled_avg_price=None,
            status=status,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            limit_price=None,
            submitted_at=order.submitted_at,
        )

    def forget(self, client_order_id: str) -> None:
        """Make the broker deny all knowledge of an order, as an unanswered POST would."""
        self.orders.pop(client_order_id, None)

    def set_price(self, symbol: str, price: str) -> None:
        held, _ = self.positions.get(symbol, ("0", price))
        self.positions[symbol] = (held, price)


def an_external_order(
    client_order_id: str,
    symbol: str,
    side: str,
    quantity: str,
    *,
    limit_price: str | None = None,
    filled: str = "0",
    status: str = "new",
) -> BrokerOrder:
    """A working order this application did not place -- one made in Alpaca's own interface."""
    return BrokerOrder(
        order_id=f"external-{client_order_id}",
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        quantity=Decimal(quantity),
        filled_quantity=Decimal(filled),
        filled_avg_price=None,
        status=status,
        order_type="limit" if limit_price is not None else "market",
        time_in_force="day",
        limit_price=None if limit_price is None else Decimal(limit_price),
        submitted_at=NOW,
    )


# --- redirection ------------------------------------------------------------------------------


@contextlib.contextmanager
def execution_sessions(session: Session) -> Iterator[None]:
    """Point every execution store at the test's rolled-back transaction.

    Four modules, because four of them open their own session: the store, the approval workflow,
    the executor and the routes. A single missed one would write into the development database
    and the failure would look like a logic error rather than a test-setup one.
    """
    from tests.conversation_doubles import SessionProxy

    proxy = SessionProxy(session)
    with contextlib.ExitStack() as stack:
        for target in (
            "app.orders.SessionLocal",
            "app.order_execution.SessionLocal",
            "app.order_executor.SessionLocal",
            "app.execution_api.SessionLocal",
        ):
            stack.enter_context(mock.patch(target, return_value=proxy))
        yield


# What each module actually imports, and therefore what can be replaced in it.
#
# `patch.multiple` refuses to invent an attribute that is not there, and that is the right
# behaviour: silently *adding* `submit_order` to `app.order_execution` would replace nothing and
# hide the fact that this module never submits. It reads three things -- the account, the clock
# and the working orders -- because validating a plan needs all three and sending is not its job.
#
# Getting this wrong is not a small mistake. A setUp that raises is a setUp whose tearDown never
# runs, so the transaction it opened stays open and holds the portfolio row's unique keys; the
# next test's setUp then blocks on that row for ever. That is how a one-line AttributeError here
# became a suite that hung on its own lock.
_BROKER_METHODS = {
    "fetch_snapshot": "snapshot",
    "fetch_clock": "clock",
    "fetch_open_orders": "open_orders",
    "fetch_order_by_client_id": "by_client_id",
    "submit_order": "submit",
}

_READS = ("fetch_snapshot", "fetch_clock", "fetch_open_orders")
_READS_AND_SUBMISSION = (*_READS, "fetch_order_by_client_id", "submit_order")

_BROKER_TARGETS = {
    "app.order_execution": _READS,
    "app.order_executor": _READS_AND_SUBMISSION,
    # `app.broker_sync` is here for safety rather than for the tests' benefit. The executor's
    # portfolio sync reads the account through this name, and a test that forgot to replace it
    # would not fail -- it would reach the real paper account. Replacing it means the worst a
    # forgotten mock can do is answer with the fake's figures.
    "app.broker_sync": ("fetch_snapshot",),
}


@contextlib.contextmanager
def fake_broker(broker: FakeBroker) -> Iterator[FakeBroker]:
    """Answer every Alpaca call from `broker`, in both modules that make them.

    Two modules, because they import the functions directly: the approval workflow reads the
    account, the clock and the working orders to validate a plan, and the executor reads them
    again and is the only thing that submits.
    """
    with contextlib.ExitStack() as stack:
        for module, names in _BROKER_TARGETS.items():
            stack.enter_context(
                mock.patch.multiple(
                    module, **{name: getattr(broker, _BROKER_METHODS[name]) for name in names}
                )
            )
        yield broker


class FailureSafeTransaction:
    """A `ToolTestCase` whose transaction cannot outlive a `setUp` that raised.

    unittest does not call `tearDown` when `setUp` fails, and `ToolTestCase` releases its
    transaction there. So an exception anywhere in a subclass's `setUp` leaves a connection open
    on an open transaction -- and because these tests insert a portfolio whose name and broker
    account are unique, the *next* test's insert waits on that row rather than failing. The suite
    does not report an error; it stops, indefinitely.

    `addCleanup` is what closes that gap: cleanups run whether the test passed, failed or never
    started. On the happy path `tearDown` has already rolled back, so this is a no-op.
    """

    def guard_transaction(self) -> None:
        self.addCleanup(self._release_transaction)

    def _release_transaction(self) -> None:
        transaction = getattr(self, "transaction", None)
        if transaction is not None and transaction.is_active:
            transaction.rollback()



@contextlib.contextmanager
def fake_portfolio_sync(
    broker: FakeBroker, *, failure: Exception | None = None
) -> Iterator[list[dict]]:
    """Replace the executor's portfolio sync with the real write, minus its own transaction.

    `broker_sync.sync` opens a `session.begin()` of its own, and a test session already has a
    transaction open, so the real entry point cannot be called from inside one. What is replaced
    is only that boundary: the write below is `apply_snapshot`, which is the same validation and
    the same upsert the application performs, so the portfolio a test reads back afterwards was
    written by the code that writes it in production.

    `failure` makes the sync raise instead, which is how a sync failure is told apart from an
    execution failure. The yielded list records one entry per attempt: how often a retry happens
    is not something the batch records, so it can only be counted.
    """
    from app import broker_sync

    attempts: list[dict] = []

    def sync(session, *, now=None):
        attempts.append({"now": now})
        if failure is not None:
            raise failure
        return broker_sync.apply_snapshot(session, broker.snapshot())

    with mock.patch("app.broker_sync.sync", side_effect=sync):
        yield attempts
