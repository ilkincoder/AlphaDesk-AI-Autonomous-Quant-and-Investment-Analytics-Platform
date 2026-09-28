"""Carrying out an approved batch: submit, reconcile, sync. Server-side, whatever the browser is doing.

One unit of work is `step`: take the one active batch, find out what the broker has done with
everything already sent, and move the batch forward by at most one decision. `run_once` is the
same thing for a caller that wants to watch it happen. A daemon thread started by the application's
lifespan calls it on a timer, so execution continues whether or not anybody has the Orders page
open -- and a second thread cannot pile on, because `step` takes a lock and every submission is
claimed in the database.

**Sells before buys, and buys only when every sell has *filled*.** Not "accepted", not "mostly
filled": the buys may be funded by the proceeds, and a sale that has not settled has not produced
any. A sell that ends rejected, canceled or expired stops the whole remainder, because the money
the buys were counting on is not coming.

**Nothing is ever resized and nothing is ever undone.** A buy the account can no longer afford is
halted, not shrunk. A fill that already happened stays. A batch that could do only part of what was
approved says so and stops -- it does not go back and rearrange what is left to look tidier.

**A submission with no answer is reconciled, not retried.** Every order carries a client order id
derived from its position in the batch, so the broker can be asked what became of it. A single
"no such order" is not an answer -- the request may still be in flight -- so it takes
`UNCERTAIN_CHECKS_REQUIRED` consecutive misses after `UNCERTAIN_GRACE_SECONDS` before the order is
recorded as never confirmed. It is still not resent: an order that may exist must not be created
twice, and a batch that is honestly partial is a better outcome than a duplicate trade.

**The portfolio sync is attempted once the batch is over, and its failure is its own fact.** An
execution that filled and a portfolio that could not be updated are two different problems with
two different fixes, and `execution_batches.sync_*` is where the second one is recorded.
"""

import logging
import threading
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

from app import broker_sync, orders
from app.alpaca import (
    AlpacaError,
    BrokerOrder,
    BrokerSnapshot,
    SubmissionUncertainError,
    fetch_clock,
    fetch_open_orders,
    fetch_order_by_client_id,
    fetch_snapshot,
    submit_order,
)
from app.db import SessionLocal
from app.execution_plan import CASH_BUFFER_MINIMUM, CASH_BUFFER_PERCENT

logger = logging.getLogger(__name__)

# How often the loop looks. Short enough that a fill is noticed while somebody is watching the
# page, long enough that an idle process is not hammering Alpaca.
POLL_INTERVAL_SECONDS = 4.0

# How many consecutive reconciliations must find no such order, and how long the submission must
# have been unanswered, before it is recorded as never confirmed. Both, because either alone is
# wrong: a fast second check is not patience, and a slow single check is not evidence.
UNCERTAIN_CHECKS_REQUIRED = 2
UNCERTAIN_GRACE_SECONDS = 60

# A ceiling on the broker requests one pass may spend. A batch of a dozen orders needs a handful;
# a bug that looped would otherwise spend the account's whole rate limit in one tick.
MAX_REQUESTS_PER_STEP = 40

# How long after a batch finishes its portfolio sync is still retried. The broker being briefly
# unreachable should not leave a filled order reflected nowhere, and a sync that is still failing
# an hour later is not going to be fixed by asking again.
SYNC_RETRY_WINDOW_SECONDS = 900

# One pass at a time per process, exactly as `app.broker_sync` holds one sync at a time. Two
# passes would each read the same intents and each decide they were theirs to submit; the
# database claims are the real guard, and this is what stops the second pass being attempted.
_STEP_LOCK = threading.Lock()


class StepInProgressError(Exception):
    """Another pass is running in this process. Skipped, not queued."""


@contextmanager
def _exclusive():
    if not _STEP_LOCK.acquire(blocking=False):
        raise StepInProgressError("an execution pass is already running")
    try:
        yield
    finally:
        _STEP_LOCK.release()


def run_once(batch_id: str | None = None, *, now: datetime | None = None) -> dict | None:
    """One pass, for a caller that wants to watch it. Returns the batch as it stands afterwards.

    Used by the tests, and by nothing in the request path: an HTTP request must not wait on a
    broker round trip that the background loop is already doing.
    """
    with _exclusive():
        return _step(batch_id, now=now)


def step(*, now: datetime | None = None) -> dict | None:
    """One pass over whatever is outstanding. Silent when nothing is."""
    try:
        with _exclusive():
            return _step(None, now=now)
    except StepInProgressError:
        return None


def _step(batch_id: str | None, *, now: datetime | None) -> dict | None:
    moment = now or datetime.now(timezone.utc)

    # A batch nobody can finish must not hold the account's only slot for ever.
    orders.expire_stale_batches(now=moment)

    if batch_id is None:
        active = orders.active_batch()
        if active is None:
            _retry_pending_syncs(moment)
            return None
        batch_id = active["batch_id"]

    batch = orders.batch(batch_id)
    if batch is None:
        return None

    if batch["status"] in orders.TERMINAL_BATCH_STATUSES:
        _maybe_sync(batch, moment)
        return orders.batch(batch_id)

    try:
        _advance(batch, moment)
    except AlpacaError as exc:
        # The broker could not be read. Nothing is decided and nothing is submitted; the next
        # pass tries again, and the batch's own record says nothing has changed.
        logger.warning("an execution pass could not read the broker: %s", type(exc).__name__)
        orders.add_note(
            batch["batch_id"],
            f"The Alpaca paper account could not be read ({type(exc).__name__}); nothing was "
            "submitted and the batch was left as it was. It is retried on the next pass.",
        )

    return orders.batch(batch_id)


def _advance(batch: Mapping, moment: datetime) -> None:
    """Everything one pass does with one active batch."""
    batch_id = str(batch["batch_id"])

    broker = fetch_snapshot()
    clock = fetch_clock()

    _reconcile(batch_id, moment)

    intents = orders.batch(batch_id)["orders"]
    sells = [item for item in intents if item["side"] == "sell"]
    buys = [item for item in intents if item["side"] == "buy"]

    # A sale that has *not been sent yet* is not a sale in flight. `OPEN_STATUSES` answers "may
    # the broker still change this", which `planned` belongs to, but the question here is "is
    # there a sale I am waiting on" -- and waiting on one nobody has sent is waiting for ever.
    in_flight = [
        item
        for item in sells
        if item["status"] in orders.OPEN_STATUSES and item["status"] != orders.INTENT_PLANNED
    ]
    if in_flight:
        # Not an error and not a stall: a market sell takes a moment, and a partial fill is not a
        # finished one. The buys wait, because the money has not arrived.
        orders.add_note(
            batch_id,
            "Waiting for every sale to fill before any purchase is submitted: the purchases are "
            "funded by the proceeds.",
        )
        orders.set_batch_status(batch_id, status=orders.BATCH_SUBMITTING, now=moment)
        return

    dead = [item for item in sells if item["status"] in orders.DEAD_STATUSES]
    if dead:
        symbols = ", ".join(sorted(item["symbol"] for item in dead))
        _halt_remaining(
            batch_id,
            reason=(
                f"{symbols} did not sell: "
                + "; ".join(
                    f"{item['symbol']} is {item['status']}" for item in sorted(dead, key=_symbol)
                )
                + ". The purchases depended on those proceeds, so the rest of this batch was "
                "stopped rather than funded from money that is not there. What already filled "
                "stands, and nothing was undone or resized."
            ),
            now=moment,
        )
        _finish(batch_id, moment)
        return

    pending_sells = [item for item in sells if item["status"] == orders.INTENT_PLANNED]
    if pending_sells:
        # The sales go out now and the purchases wait for them to *fill*, which is a later pass's
        # question. This pass ends here whether or not the sale has been answered: assuming it
        # filled is exactly the assumption the sequencing exists to avoid.
        orders.set_batch_status(batch_id, status=orders.BATCH_SUBMITTING, now=moment)
        for item in sorted(pending_sells, key=lambda entry: int(entry["sequence"])):
            _submit_one(item, moment)
        return

    planned_buys = [item for item in buys if item["status"] == orders.INTENT_PLANNED]
    if planned_buys:
        if not clock.is_open:
            # A DAY order cannot be sent when the market is shut, and nothing here queues one for
            # the next open: a decision made against this morning's prices should not be executed
            # against tomorrow's. Whatever the broker is still holding is left to it.
            _halt_remaining(
                batch_id,
                reason=(
                    "The US market is closed, so the orders that had not been submitted yet were "
                    "stopped rather than queued overnight. Approve a fresh proposal during "
                    "regular trading hours to act on it; what already filled stands."
                ),
                now=moment,
            )
            _finish(batch_id, moment)
            return

        orders.set_batch_status(batch_id, status=orders.BATCH_SUBMITTING, now=moment)
        _submit_buys(batch_id, broker, planned_buys, moment)

    _finish_if_done(batch_id, moment)


def _symbol(item: Mapping) -> str:
    return str(item["symbol"])


# --- reconciliation -----------------------------------------------------------------------------


def _reconcile(batch_id: str, moment: datetime) -> None:
    """Ask the broker about everything this batch has in play, and record what it says.

    A request per order is the honest cost of knowing: the alternative is listing the account's
    orders and matching them up, which would still be one request but would answer a different
    question -- what the account has done -- rather than this one, which is what became of *this*
    submission.
    """
    intents = [
        item
        for item in orders.batch(batch_id)["orders"]
        if item["status"] in orders.OPEN_STATUSES and item["status"] != orders.INTENT_PLANNED
    ]

    for item in intents[:MAX_REQUESTS_PER_STEP]:
        order = fetch_order_by_client_id(str(item["client_order_id"]))
        if order is not None:
            orders.apply_reconciliation(str(item["intent_id"]), order, checked_at=moment)
            continue

        # No such order. That is not proof yet: a submission can be answered late, so it counts
        # rather than concludes.
        orders.apply_reconciliation(
            str(item["intent_id"]),
            None,
            checked_at=moment,
            settle=(
                int(item["uncertain_checks"]) + 1 >= UNCERTAIN_CHECKS_REQUIRED
                and _age_seconds(item.get("submitted_at"), moment) >= UNCERTAIN_GRACE_SECONDS
            ),
        )


def _age_seconds(submitted_at: object, moment: datetime) -> float:
    if not isinstance(submitted_at, str) or not submitted_at:
        return 0.0
    try:
        parsed = datetime.fromisoformat(submitted_at)
    except ValueError:  # pragma: no cover - written by this application
        return 0.0
    if parsed.tzinfo is None:  # pragma: no cover
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (moment - parsed).total_seconds()


# --- submission ---------------------------------------------------------------------------------


def _submit_buys(
    batch_id: str, broker: BrokerSnapshot, planned: Sequence[Mapping], moment: datetime
) -> None:
    """Submit the purchases that are still owed, one at a time, re-checking the cash each time.

    The cash is the account's, read moments ago, minus everything else that has a claim on it:
    other working buy orders, this batch's own purchases that are already in flight, and the
    disclosed buffer. The account is re-read once per pass rather than once per order -- a request
    per order would be a rate limit spent on a number that a single market order cannot move fast
    enough to matter.
    """
    prices = {position.symbol: position.current_price for position in broker.positions}
    available = broker.cash - _committed_cash(batch_id, broker, prices) - _buffer(planned, prices)

    moved = _portfolio_moved(batch_id, broker)
    if moved is not None:
        _halt_remaining(batch_id, reason=moved, now=moment)
        _finish(batch_id, moment)
        return

    for item in sorted(planned, key=lambda entry: int(entry["sequence"])):
        price = prices.get(str(item["symbol"])) or Decimal(str(item["estimated_price"]))
        cost = Decimal(str(item["quantity"])) * price

        if cost > available:
            _halt_remaining(
                batch_id,
                reason=(
                    f"{item['symbol']}: buying {item['quantity']} shares would cost about "
                    f"{cost.quantize(Decimal('0.01'))}, and only "
                    f"{available.quantize(Decimal('0.01'))} of the account's cash is free for it "
                    "after everything else already committed and the buffer held back. The order "
                    "was not resized to fit -- a smaller order is not the order that was "
                    "approved. It and everything after it were stopped; the purchases that "
                    "already filled stand."
                ),
                now=moment,
            )
            _finish(batch_id, moment)
            return

        _submit_one(item, moment)
        available -= cost


def _submit_one(item: Mapping, moment: datetime) -> None:
    """Send one order, under a claim on its intent, and record exactly what came back.

    The claim is what makes this safe to attempt twice: `claim_intent` moves the row from
    `planned` to `submitting` in one statement, and a second worker, a retry or a resumed pass
    finds nothing to take and returns. Everything after the claim records what the broker said --
    including that it said nothing, which is a state rather than a failure.
    """
    claimed = orders.claim_intent(str(item["intent_id"]), now=moment)
    if claimed is None:
        return

    try:
        order = submit_order(
            client_order_id=str(claimed["client_order_id"]),
            symbol=str(claimed["symbol"]),
            side=str(claimed["side"]),
            quantity=Decimal(str(claimed["quantity"])),
            order_type=str(claimed["order_type"]),
            time_in_force=str(claimed["time_in_force"]),
        )
    except SubmissionUncertainError as exc:
        # No answer. The order may exist, so it is neither failed nor retried: it is recorded as
        # unknown and reconciled by its client order id on the next pass.
        logger.warning("a submission for intent %s got no answer", claimed["intent_id"])
        orders.record_uncertain(str(claimed["intent_id"]), str(exc), now=moment)
        return
    except AlpacaError as exc:
        # The broker answered, and the answer was no. Nothing was created.
        logger.warning(
            "Alpaca refused an order: intent %s (%s)",
            claimed["intent_id"],
            type(exc).__name__,
        )
        orders.record_refusal(str(claimed["intent_id"]), str(exc), now=moment)
        return

    orders.record_submission(str(claimed["intent_id"]), order, now=moment)


def _committed_cash(
    batch_id: str, broker: BrokerSnapshot, prices: Mapping[str, Decimal]
) -> Decimal:
    """Cash that something other than a not-yet-sent order of this batch has a claim on.

    Working orders this application did not place count, because the account does not distinguish
    between them. This batch's own in-flight buys count too: their cost is committed even though
    their row says `submitting`.
    """
    ours = {
        str(item["client_order_id"]): item
        for item in orders.batch(batch_id)["orders"]
    }

    committed = Decimal("0")
    for order in _external_open_orders(ours):
        price = order.limit_price if order.limit_price is not None else prices.get(order.symbol)
        if price is None:
            # Cannot be bounded. The approval refused this case; if it has appeared since, the
            # safe reading is that the cash is not known to be free.
            committed += Decimal("0")
            continue
        committed += order.remaining_quantity * price

    for item in ours.values():
        if item["side"] != "buy" or item["status"] not in {
            orders.INTENT_SUBMITTING,
            orders.INTENT_UNKNOWN,
            orders.INTENT_ACCEPTED,
            orders.INTENT_PARTIALLY_FILLED,
        }:
            continue
        price = prices.get(str(item["symbol"])) or Decimal(str(item["estimated_price"]))
        remaining = Decimal(str(item["quantity"])) - Decimal(str(item["filled_quantity"]))
        committed += max(remaining, Decimal("0")) * price

    return committed.quantize(Decimal("0.01"))


def _external_open_orders(ours: Mapping[str, Mapping]) -> tuple[BrokerOrder, ...]:
    """The account's working orders that are not this batch's own."""
    try:
        open_orders = fetch_open_orders()
    except AlpacaError:
        # A read that failed leaves the commitment unknown rather than zero. The buys below are
        # still checked against the cash the account reported, and the buffer absorbs the rest.
        logger.warning("the account's open orders could not be read while revalidating a buy")
        return ()
    return tuple(order for order in open_orders if order.client_order_id not in ours)


def _buffer(planned: Sequence[Mapping], prices: Mapping[str, Decimal]) -> Decimal:
    """The disclosed amount held back so a market order cannot overdraw the account."""
    cost = sum(
        (
            Decimal(str(item["quantity"]))
            * (prices.get(str(item["symbol"])) or Decimal(str(item["estimated_price"])))
            for item in planned
        ),
        Decimal("0"),
    )
    return max((cost * CASH_BUFFER_PERCENT).quantize(Decimal("0.01")), CASH_BUFFER_MINIMUM)


def _portfolio_moved(batch_id: str, broker: BrokerSnapshot) -> str | None:
    """Whether anything other than this batch's own fills has touched the account.

    Every order in the batch carries the quantity the broker reported when the plan was validated.
    So the position the broker should be showing now is that quantity, plus what this batch's own
    orders have filled in that symbol. Anything else is a trade this application did not make --
    placed in Alpaca's own interface, or by something else altogether -- and a plan built on a
    portfolio that has moved underneath it is not a plan any more.
    """
    held = {position.symbol: position.quantity for position in broker.positions}
    expected: dict[str, Decimal] = {}

    for item in orders.batch(batch_id)["orders"]:
        symbol = str(item["symbol"])
        base = expected.setdefault(symbol, Decimal(str(item["quantity_before"])))
        filled = Decimal(str(item["filled_quantity"]))
        expected[symbol] = base + (filled if item["side"] == "buy" else -filled)

    for symbol, wanted in sorted(expected.items()):
        actual = held.get(symbol, Decimal("0"))
        if actual != wanted:
            return (
                f"{symbol}: this plan was validated against a holding of "
                f"{_text(wanted)}, and the account now shows {_text(actual)}. Something other "
                "than this batch's own fills moved it, so the rest of the batch was stopped "
                "rather than executed against a portfolio the plan does not describe. What "
                "already filled stands."
            )
    return None


# --- finishing ----------------------------------------------------------------------------------


def _halt_remaining(batch_id: str, *, reason: str, now: datetime) -> None:
    """Halt every order in this batch that has not been sent, and say why."""
    halted = orders.halt_planned(batch_id, reason=reason, now=now)
    if halted:
        orders.add_note(batch_id, f"{halted} order(s) were stopped before being submitted.")
    orders.set_batch_status(
        batch_id, status=orders.BATCH_HALTED, halt_reason=reason, now=now
    )


def _finish_if_done(batch_id: str, moment: datetime) -> None:
    intents = orders.batch(batch_id)["orders"]
    if any(item["status"] in orders.OPEN_STATUSES for item in intents):
        return
    _finish(batch_id, moment)


def _finish(batch_id: str, moment: datetime) -> None:
    """Settle the batch's status from its orders, then sync the portfolio."""
    batch = orders.batch(batch_id)
    intents = batch["orders"]

    filled = [item for item in intents if item["status"] == orders.INTENT_FILLED]
    unfilled = [item for item in intents if item["status"] != orders.INTENT_FILLED]

    if batch["halt_reason"]:
        status = orders.BATCH_HALTED
    elif not unfilled:
        status = orders.BATCH_COMPLETED
    else:
        status = orders.BATCH_PARTIAL

    if status == orders.BATCH_COMPLETED:
        orders.add_note(
            batch_id,
            f"All {len(filled)} order(s) filled. The portfolio is synchronised from the "
            "broker's own report of the fills.",
        )
    elif unfilled:
        orders.add_note(
            batch_id,
            f"{len(filled)} of {len(intents)} order(s) filled. "
            + "Not filled: "
            + "; ".join(
                f"{item['symbol']} ({item['side']}) is {item['status']}" for item in unfilled
            )
            + ". Nothing was resized or undone; the portfolio is synchronised from what the "
            "broker reports actually filled.",
        )

    orders.set_batch_status(batch_id, status=status, now=moment)
    _maybe_sync(orders.batch(batch_id), moment)


def _maybe_sync(batch: Mapping, moment: datetime) -> None:
    """Synchronise the portfolio from the broker, once the batch is over.

    The portfolio is updated from what the broker says it holds, never by subtracting this
    application's own estimates from it -- the broker's report is the only account of what
    actually filled. A sync that fails is recorded as a *sync* failure, which is a different
    statement from an order that failed, and the batch keeps saying which one it was.
    """
    if batch["sync_status"] == orders.SYNC_OK:
        return

    completed = batch.get("completed_at")
    if completed is not None and _age_seconds(completed, moment) > SYNC_RETRY_WINDOW_SECONDS:
        return

    try:
        with SessionLocal() as session:
            broker_sync.sync(session, now=moment)
    except broker_sync.SyncError as exc:
        orders.record_sync(batch["batch_id"], status=orders.SYNC_FAILED, error=str(exc), now=moment)
        return
    except AlpacaError as exc:
        orders.record_sync(
            batch["batch_id"], status=orders.SYNC_FAILED, error=str(exc), now=moment
        )
        return

    orders.record_sync(batch["batch_id"], status=orders.SYNC_OK, now=moment)


def _retry_pending_syncs(moment: datetime) -> None:
    """Keep trying the portfolio sync for batches that finished but could not be written.

    Bounded by time rather than by a count: the broker being briefly unreachable should not leave
    a filled order reflected nowhere, and a sync still failing fifteen minutes later needs a
    person, not another attempt.
    """
    for batch in orders.batches(limit=10):
        if batch["status"] in orders.TERMINAL_BATCH_STATUSES and batch["sync_status"] != orders.SYNC_OK:
            _maybe_sync(batch, moment)


# --- the loop ------------------------------------------------------------------------------------


class _Loop:
    """The daemon thread that calls `step` on a timer, and the switch that stops it."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="order-executor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            # Joined with a bound: a pass in the middle of a broker request must not hold up a
            # shutdown, and the thread is a daemon, so the process can leave without it.
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(POLL_INTERVAL_SECONDS):
            try:
                step()
            except Exception:  # noqa: BLE001 - a failed pass must not kill the loop
                logger.exception("an execution pass failed")


_loop = _Loop()


def start_background() -> None:
    """Begin following batches. Called once, from the application's lifespan."""
    _loop.start()


def stop_background() -> None:
    """Stop following batches. Called from the lifespan's shutdown."""
    _loop.stop()


def _text(value: Decimal) -> str:
    """A quantity inside a sentence, in one spelling -- the convention `app.rebalance` uses."""
    return format(value.normalize(), "f")


__all__ = [
    "MAX_REQUESTS_PER_STEP",
    "POLL_INTERVAL_SECONDS",
    "SYNC_RETRY_WINDOW_SECONDS",
    "StepInProgressError",
    "UNCERTAIN_CHECKS_REQUIRED",
    "UNCERTAIN_GRACE_SECONDS",
    "run_once",
    "start_background",
    "step",
    "stop_background",
]
