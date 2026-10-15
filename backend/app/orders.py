"""Storing decisions, execution batches and order intents. Every write to the execution record.

The same shape as `app.proposals` and `app.conversations`, over a different subject: every
function opens its own short-lived session, because the work between them talks to a broker and a
session held across a broker round trip turns a slow provider into a database problem.

**The database is the arbiter of every "only once".** One decision per proposal, one live batch
per account, one intent per client order id -- each is a unique constraint rather than a check
this module performs first, because a check followed by an insert is a race two requests can both
win. What this module does is decide which *answer* a losing request gets.

**An intent is written before the request that could create it.** `plan_intents` records every
order as `planned` while the batch is created, and the submission path moves one from `planned`
to `submitting` with a compare-and-set -- `UPDATE ... WHERE status = 'planned'` -- so two workers,
a retry and a restart cannot both decide it is theirs to send.

**`client_order_id` is derived rather than generated.** It is built from the batch id and the
order's sequence in it, so the same order carries the same id after a crash, a replay or a second
process. That is what makes a submission whose answer was lost recoverable at all: the id does not
have to have been stored anywhere to be reconstructed.

**Nothing here talks to Alpaca.** This module records what happened; `app.order_executor` is what
makes it happen.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.alpaca import BrokerOrder
from app.db import SessionLocal, new_id, transaction, utc_now
from app.execution_plan import ResolvedPlan
from app.models import ExecutionBatch, OrderIntent, RebalanceDecision

logger = logging.getLogger(__name__)

# --- statuses ---------------------------------------------------------------------------------

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

BATCH_PENDING = "pending"
BATCH_SUBMITTING = "submitting"
BATCH_COMPLETED = "completed"
BATCH_PARTIAL = "partial"
BATCH_HALTED = "halted"

ACTIVE_BATCH_STATUSES = frozenset({BATCH_PENDING, BATCH_SUBMITTING})
TERMINAL_BATCH_STATUSES = frozenset({BATCH_COMPLETED, BATCH_PARTIAL, BATCH_HALTED})

INTENT_PLANNED = "planned"
INTENT_SUBMITTING = "submitting"
INTENT_ACCEPTED = "accepted"
INTENT_PARTIALLY_FILLED = "partially_filled"
INTENT_FILLED = "filled"
INTENT_REJECTED = "rejected"
INTENT_CANCELED = "canceled"
INTENT_EXPIRED = "expired"
# The submission got no answer. The outcome is genuinely not known.
INTENT_UNKNOWN = "unknown"
# Reconciled against the broker and no such order was found. Not the same as `unknown`: this is
# what `unknown` becomes once the broker has been asked and had nothing to report.
INTENT_NOT_FOUND = "not_found"
# Never sent, because something before it stopped the batch. Distinct from `rejected`, which is
# the broker's word: this one is this application's.
INTENT_HALTED = "halted"

# The broker's own statuses, mapped onto the ones above. Alpaca documents more than this, and an
# unrecognised one is handled conservatively rather than guessed at -- see `map_broker_status`.
_BROKER_STATUSES = {
    "new": INTENT_ACCEPTED,
    "accepted": INTENT_ACCEPTED,
    "pending_new": INTENT_ACCEPTED,
    "accepted_for_bidding": INTENT_ACCEPTED,
    "calculated": INTENT_ACCEPTED,
    "held": INTENT_ACCEPTED,
    "stopped": INTENT_ACCEPTED,
    "partially_filled": INTENT_PARTIALLY_FILLED,
    "filled": INTENT_FILLED,
    "rejected": INTENT_REJECTED,
    "canceled": INTENT_CANCELED,
    "pending_cancel": INTENT_CANCELED,
    "expired": INTENT_EXPIRED,
    "done_for_day": INTENT_EXPIRED,
    "replaced": INTENT_EXPIRED,
    "pending_replace": INTENT_CANCELED,
    "suspended": INTENT_HALTED,
}

# What an intent is doing, in three groups, because the sequencing rules are stated in them.
# `OPEN` means the broker may still change it; `DEAD` means it will never fill.
FILLED_STATUSES = frozenset({INTENT_FILLED})
DEAD_STATUSES = frozenset(
    {INTENT_REJECTED, INTENT_CANCELED, INTENT_EXPIRED, INTENT_NOT_FOUND, INTENT_HALTED}
)
OPEN_STATUSES = frozenset(
    {INTENT_PLANNED, INTENT_SUBMITTING, INTENT_ACCEPTED, INTENT_PARTIALLY_FILLED, INTENT_UNKNOWN}
)
TERMINAL_INTENT_STATUSES = FILLED_STATUSES | DEAD_STATUSES

SYNC_PENDING = "pending"
SYNC_OK = "ok"
SYNC_FAILED = "failed"

# How long a batch may stay active before it is halted. One session's worth: a DAY order cannot
# outlive the session it was sent in, so a batch still working after that is one whose orders the
# broker has already resolved and this application has not caught up with -- or one that is
# waiting on a submission that is never going to be confirmed. Either way it stops being chased.
BATCH_MAX_AGE_HOURS = 8


class BatchConflictError(Exception):
    """An approval that cannot become a batch, because one is already running."""


class DecisionTakenError(Exception):
    """A decision already exists for this proposal, and it was a different one."""

    def __init__(self, decision: dict) -> None:
        self.decision = decision
        super().__init__(
            f"This proposal was already {decision['decision']}. One proposal takes one decision."
        )




def client_order_id(batch_id: str, sequence: int) -> str:
    """The id an order is submitted under, derived from where it sits in its batch.

    Deterministic on purpose. A generated id would have to be stored before the request that uses
    it -- and a crash in the wrong place would lose the only record of what was sent. This one can
    be recomputed from the batch row.
    """
    return f"ad-{batch_id[:16]}-{sequence:03d}"


def map_broker_status(status: str) -> str:
    """Alpaca's status, as one of this application's.

    An unrecognised status maps to `unknown` rather than to `accepted`. The distinction matters:
    `accepted` would let a batch treat the order as progressing, and an order in a state this
    build does not understand is exactly the one that should not be assumed to be progressing.
    """
    return _BROKER_STATUSES.get(status, INTENT_UNKNOWN)


# --- decisions --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Recorded:
    """A decision that is now the record, whether this call made it or found it."""

    decision: dict
    created: bool
    # The batch an approval opened, when this call is the one that opened it. None on a
    # rejection, which opens nothing, and on a replay, where the caller already has the row.
    batch_id: str | None = None


def record_rejection(
    *,
    proposal_id: str,
    idempotency_key: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> Recorded:
    """Record that a person rejected this proposal. No orders, no validation, no broker.

    A rejection is deliberately **not** validated against the market, the portfolio or the clock.
    Declining a proposal is always available and always means the same thing, and a rejection that
    could be refused because the market happened to be shut would be a decision the application
    would not let somebody make.
    """
    return _record(
        proposal_id=proposal_id,
        idempotency_key=idempotency_key,
        decision=DECISION_REJECTED,
        proposal_fingerprint="",
        plan_fingerprint="",
        order_plan=[],
        reason=reason,
        now=now,
    )


def _record(
    *,
    proposal_id: str,
    idempotency_key: str,
    decision: str,
    proposal_fingerprint: str,
    plan_fingerprint: str,
    order_plan: Sequence[Mapping[str, object]],
    reason: str | None,
    now: datetime | None,
) -> Recorded:
    """Insert the one decision for this proposal, or return the one that is already there.

    Three ways to lose this race, and all three end at the same row: another click inserted first
    (the unique constraint on `proposal_id`), this same click arrived twice (the unique constraint
    on `idempotency_key`), or a different decision got there first -- which raises rather than
    returning, because silently answering an approval with somebody's rejection would be worse
    than an error.
    """
    with SessionLocal() as session:
        with transaction(session):
            existing = _decision_row(session, proposal_id)
            if existing is not None:
                return _resolve_existing(existing, decision)

            try:
                # A savepoint, so that losing the race rolls back *this* insert and nothing else.
                # Without it a unique violation leaves the session needing a rollback of its own,
                # and the read below -- the one that decides which answer the loser gets -- is the
                # first thing to fail.
                with session.begin_nested():
                    row = RebalanceDecision(
                        id=new_id(),
                        proposal_id=proposal_id,
                        decision=decision,
                        decided_at=now or utc_now(),
                        idempotency_key=idempotency_key,
                        proposal_fingerprint=proposal_fingerprint,
                        plan_fingerprint=plan_fingerprint,
                        order_plan=[dict(order) for order in order_plan],
                        reason=reason,
                    )
                    session.add(row)
                    session.flush()
                return Recorded(decision=_decision_dict(row), created=True)
            except IntegrityError:
                # The row appeared between the read and the insert -- a second click, a retry, or
                # a request that lost the race. Answered from what is stored, never retried.
                logger.info(
                    "a decision for proposal %s already existed at insert time", proposal_id
                )

            stored = _decision_row(session, proposal_id)
            if stored is None:  # pragma: no cover - the constraint fired, so a row exists
                raise DecisionTakenError({"decision": decision})
            return _resolve_existing(stored, decision)


def approve(
    *,
    proposal_id: str,
    idempotency_key: str,
    plan: ResolvedPlan,
    broker_account_id: str,
    now: datetime | None = None,
) -> Recorded:
    """Record the approval, and open the batch that will carry it out.

    The decision and the batch are written in one transaction. A decision without a batch would
    be an approval nothing acts on; a batch without a decision would be orders with nothing behind
    them.
    """
    moment = now or utc_now()

    with SessionLocal() as session:
        with transaction(session):
            existing = _decision_row(session, proposal_id)
            if existing is not None:
                return _resolve_existing(existing, DECISION_APPROVED)

            try:
                # One savepoint over the decision and the batch together: they are one fact, and
                # a violation of either constraint must leave nothing of the other behind. It
                # also keeps the session usable for the read below, which is what decides whether
                # this was a lost race or a batch already running.
                with session.begin_nested():
                    decision_row = RebalanceDecision(
                        id=new_id(),
                        proposal_id=proposal_id,
                        decision=DECISION_APPROVED,
                        decided_at=moment,
                        idempotency_key=idempotency_key,
                        proposal_fingerprint=plan.proposal_fingerprint,
                        plan_fingerprint=plan.fingerprint,
                        order_plan=plan.as_dict(),
                        broker_account_id=broker_account_id,
                        cash_at_approval=plan.cash_at_approval,
                        reserved_cash=plan.reserved_cash,
                        price_drift_percent=plan.price_drift_percent,
                    )
                    session.add(decision_row)
                    session.flush()

                    batch = _open_batch(session, decision_row, plan, broker_account_id, moment)
                return Recorded(
                    decision=_decision_dict(decision_row),
                    created=True,
                    batch_id=batch.id,
                )
            except IntegrityError as exc:
                # Two constraints can fire here, and they mean different things. `proposal_id`
                # means a decision appeared while this one was being written -- answered from the
                # row that won. `broker_account_id` means another batch is already running for
                # this account, which is a refusal: two live batches would spend the same cash
                # twice.
                logger.info("an approval for proposal %s lost a race: %s", proposal_id, exc.orig)

            stored = _decision_row(session, proposal_id)
            if stored is not None:
                return _resolve_existing(stored, DECISION_APPROVED)

        raise BatchConflictError(
            "This approval could not be recorded: another execution batch is already running for "
            "this account. Nothing was sent to Alpaca. Wait for it to finish, then approve again."
        )


def _open_batch(
    session: Session,
    decision_row: RebalanceDecision,
    plan: ResolvedPlan,
    broker_account_id: str,
    moment: datetime,
) -> ExecutionBatch:
    """The batch and all of its intents, written together, before anything is submitted."""
    batch = ExecutionBatch(
        id=new_id(),
        proposal_id=decision_row.proposal_id,
        decision_id=decision_row.id,
        portfolio_id=_portfolio_id(session),
        broker_account_id=broker_account_id,
        currency=_portfolio_currency(session),
        status=BATCH_PENDING,
        created_at=moment,
        updated_at=moment,
        plan_fingerprint=plan.fingerprint,
        cash_at_approval=plan.cash_at_approval,
        reserved_cash=plan.reserved_cash,
        price_drift_percent=plan.price_drift_percent,
        notes=list(plan.notes),
        sync_status=SYNC_PENDING,
    )
    session.add(batch)
    session.flush()

    for order in plan.orders:
        session.add(
            OrderIntent(
                id=new_id(),
                batch_id=batch.id,
                sequence=order.sequence,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                order_type="market",
                time_in_force="day",
                # Derived here, at the only moment the batch id and the sequence are both known.
                client_order_id=client_order_id(batch.id, order.sequence),
                status=INTENT_PLANNED,
                estimated_price=order.estimated_price,
                quantity_before=order.quantity_before,
                filled_quantity=Decimal("0"),
                uncertain_checks=0,
            )
        )

    session.flush()
    return batch


def _portfolio_id(session: Session) -> int:
    from app import portfolio_identity

    portfolio = portfolio_identity.find(session)
    if portfolio is None:  # pragma: no cover - an approval without a portfolio is impossible
        raise BatchConflictError("There is no portfolio for this batch to belong to.")
    return portfolio.id


def _portfolio_currency(session: Session) -> str:
    from app import portfolio_identity

    portfolio = portfolio_identity.find(session)
    return "USD" if portfolio is None else portfolio.currency


def _resolve_existing(row: RebalanceDecision, requested: str) -> Recorded:
    """The decision that already exists, when it agrees with the one that was asked for."""
    if row.decision != requested:
        raise DecisionTakenError(_decision_dict(row))
    return Recorded(decision=_decision_dict(row), created=False)


def _decision_row(session: Session, proposal_id: str) -> RebalanceDecision | None:
    return session.scalars(
        select(RebalanceDecision).where(RebalanceDecision.proposal_id == proposal_id)
    ).first()


def decision_for(proposal_id: str) -> dict | None:
    """This proposal's decision, or None if nobody has made one."""
    with SessionLocal() as session:
        row = _decision_row(session, proposal_id)
        return None if row is None else _decision_dict(row)


def _decision_dict(row: RebalanceDecision) -> dict:
    return {
        "decision_id": row.id,
        "proposal_id": row.proposal_id,
        "decision": row.decision,
        "decided_at": row.decided_at.isoformat(),
        "plan_fingerprint": row.plan_fingerprint,
        "price_drift_percent": (
            None if row.price_drift_percent is None else str(row.price_drift_percent)
        ),
        "reason": row.reason,
        "order_plan": list(row.order_plan or []),
    }


# --- batches and intents ----------------------------------------------------------------------


def active_batch() -> dict | None:
    """The batch currently working, if there is one. There is at most one per account."""
    with SessionLocal() as session:
        row = session.scalars(
            select(ExecutionBatch).where(
                ExecutionBatch.status.in_(sorted(ACTIVE_BATCH_STATUSES))
            )
        ).first()
        return None if row is None else _batch_dict(row)


def expire_stale_batches(*, now: datetime | None = None) -> list[str]:
    """Halt any batch that has been active for longer than a session could explain.

    Lazy, exactly as `app.proposals` recovers an overrun run: the next read or step clears it,
    and nothing sweeps in the background. Without this a batch whose submission is never
    confirmed would hold the account's only slot for ever.
    """
    moment = now or utc_now()
    cutoff = moment - timedelta(hours=BATCH_MAX_AGE_HOURS)
    halted: list[str] = []

    with SessionLocal() as session:
        with transaction(session):
            rows = session.scalars(
                select(ExecutionBatch).where(
                    ExecutionBatch.status.in_(sorted(ACTIVE_BATCH_STATUSES)),
                    ExecutionBatch.created_at < cutoff,
                )
            ).all()
            for batch in rows:
                batch.status = BATCH_HALTED
                batch.halt_reason = (
                    "This batch was still working more than "
                    f"{BATCH_MAX_AGE_HOURS} hours after it was approved, which is longer than the "
                    "trading session its DAY orders could have lived through. It was stopped "
                    "rather than left holding the account's only execution slot. Any order that "
                    "did fill has been recorded; nothing was resubmitted."
                )
                batch.completed_at = moment
                batch.updated_at = moment
                _halt_open_intents(session, batch.id, moment)
                halted.append(batch.id)

    return halted


def _halt_open_intents(session: Session, batch_id: str, moment: datetime) -> None:
    """Everything still in play becomes `halted`: never sent, and never going to be."""
    session.execute(
        update(OrderIntent)
        .where(OrderIntent.batch_id == batch_id, OrderIntent.status.in_(sorted(OPEN_STATUSES)))
        .values(status=INTENT_HALTED, resolved_at=moment, last_checked_at=moment)
    )


def batch(batch_id: str) -> dict | None:
    """One batch with its intents, or None."""
    with SessionLocal() as session:
        row = session.get(ExecutionBatch, batch_id)
        if row is None:
            return None
        return _batch_dict(row, _intents(session, batch_id))


def batch_for_proposal(proposal_id: str) -> dict | None:
    """The batch an approved proposal opened, or None if it was never approved.

    Read by the proposal API, so a page restoring itself can show that its proposal was approved
    and which batch to open -- without holding the batch id anywhere in the browser.
    """
    with SessionLocal() as session:
        row = session.scalars(
            select(ExecutionBatch).where(ExecutionBatch.proposal_id == proposal_id)
        ).first()
        if row is None:
            return None
        return _batch_dict(row, _intents(session, row.id))


def batches(limit: int = 50) -> list[dict]:
    """Every batch, newest first, with its intents. History is kept rather than replaced."""
    with SessionLocal() as session:
        rows = session.scalars(
            select(ExecutionBatch)
            .order_by(ExecutionBatch.created_at.desc(), ExecutionBatch.id.desc())
            .limit(limit)
        ).all()
        return [_batch_dict(row, _intents(session, row.id)) for row in rows]


def _intents(session: Session, batch_id: str) -> list[OrderIntent]:
    return list(
        session.scalars(
            select(OrderIntent)
            .where(OrderIntent.batch_id == batch_id)
            .order_by(OrderIntent.sequence)
        )
    )


def _batch_dict(row: ExecutionBatch, intents: Sequence[OrderIntent] | None = None) -> dict:
    return {
        "batch_id": row.id,
        "proposal_id": row.proposal_id,
        "decision_id": row.decision_id,
        # Carried so a route can check that a batch addressed by id belongs to this
        # application's portfolio before serving it.
        "portfolio_id": row.portfolio_id,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "started_at": None if row.started_at is None else row.started_at.isoformat(),
        "completed_at": None if row.completed_at is None else row.completed_at.isoformat(),
        "broker_account_id": row.broker_account_id,
        "currency": row.currency,
        "cash_at_approval": str(row.cash_at_approval),
        "reserved_cash": str(row.reserved_cash),
        "price_drift_percent": (
            None if row.price_drift_percent is None else str(row.price_drift_percent)
        ),
        "notes": list(row.notes or []),
        "halt_reason": row.halt_reason,
        "sync_status": row.sync_status,
        "sync_error": row.sync_error,
        "sync_at": None if row.sync_at is None else row.sync_at.isoformat(),
        "orders": [] if intents is None else [_intent_dict(item) for item in intents],
    }


def _intent_dict(row: OrderIntent) -> dict:
    return {
        "intent_id": row.id,
        "sequence": row.sequence,
        "symbol": row.symbol,
        "side": row.side,
        "quantity": str(row.quantity),
        "order_type": row.order_type,
        "time_in_force": row.time_in_force,
        "status": row.status,
        "broker_status": row.broker_status,
        # The estimate the plan was approved at. Labelled as an estimate wherever it is shown;
        # `filled_avg_price` below is the only actual price on this row.
        "estimated_price": str(row.estimated_price),
        "quantity_before": str(row.quantity_before),
        "filled_quantity": str(row.filled_quantity),
        "filled_avg_price": (
            None if row.filled_avg_price is None else str(row.filled_avg_price)
        ),
        "client_order_id": row.client_order_id,
        "broker_order_id": row.broker_order_id,
        "submitted_at": None if row.submitted_at is None else row.submitted_at.isoformat(),
        "resolved_at": None if row.resolved_at is None else row.resolved_at.isoformat(),
        "uncertain_checks": row.uncertain_checks,
        "error": row.error,
    }


# --- the submission path ----------------------------------------------------------------------
#
# Four functions, in the order `app.order_executor` calls them. The first is a compare-and-set,
# and everything after it records what the broker said.


def claim_intent(intent_id: str, *, now: datetime | None = None) -> dict | None:
    """Take ownership of one intent for submission, or return None if it is not available.

    A single `UPDATE ... WHERE status = 'planned'` is the claim. Two workers, a retry and a
    resumed run all contend for the same row, and exactly one wins -- which is what makes "one
    order per intent" a property of the database rather than of the code that calls it.

    **`submitted_at` is stamped here, before the request is made.** It is the moment the
    submission was *attempted*, which is the only moment that exists if the process dies between
    this claim and the POST. Without it a claimed-but-never-sent order has no age, so the grace
    period that lets reconciliation settle it never elapses and the batch waits out its whole
    stale window instead of reporting that the order was never confirmed.

    Returns the intent as a plain dict, or None when somebody else holds it, when it has already
    been submitted, or when it was halted before it got its turn.
    """
    moment = now or utc_now()

    with SessionLocal() as session:
        with transaction(session):
            claimed = session.execute(
                update(OrderIntent)
                .where(OrderIntent.id == intent_id, OrderIntent.status == INTENT_PLANNED)
                .values(
                    status=INTENT_SUBMITTING,
                    last_checked_at=moment,
                    submitted_at=moment,
                )
                .returning(OrderIntent.id)
            ).scalar_one_or_none()

            if claimed is None:
                return None

            row = session.get(OrderIntent, intent_id)
            return None if row is None else _intent_dict(row)


def record_submission(intent_id: str, order: BrokerOrder, *, now: datetime | None = None) -> None:
    """Record what the broker said about the order that was just sent.

    The broker's own id, its status and its fill are all stored as it reported them. A submission
    that came back accepted is *accepted*, not filled, and the difference is kept.
    """
    moment = now or utc_now()
    with SessionLocal() as session:
        with transaction(session):
            row = session.get(OrderIntent, intent_id, with_for_update=True)
            if row is None:  # pragma: no cover - the caller just claimed it
                return
            _apply_broker_order(row, order, moment)


def record_uncertain(intent_id: str, error: str, *, now: datetime | None = None) -> None:
    """Record that a submission got no answer, so its outcome is not known.

    Deliberately **not** a failure and deliberately not retried. The intent stays in play, holds
    up whatever depended on it, and is reconciled by its client order id on the next pass.
    """
    moment = now or utc_now()
    with SessionLocal() as session:
        with transaction(session):
            row = session.get(OrderIntent, intent_id, with_for_update=True)
            if row is None:  # pragma: no cover
                return
            row.status = INTENT_UNKNOWN
            row.error = error
            row.submitted_at = moment
            row.last_checked_at = moment


def record_refusal(intent_id: str, error: str, *, now: datetime | None = None) -> None:
    """Record that the broker refused the order outright, so nothing was created.

    The one case where a `POST` returning an error really does mean no order exists: a bad symbol,
    insufficient buying power, a closed account. Distinct from `record_uncertain`, and the
    distinction is the whole reason the client raises two different errors.
    """
    moment = now or utc_now()
    with SessionLocal() as session:
        with transaction(session):
            row = session.get(OrderIntent, intent_id, with_for_update=True)
            if row is None:  # pragma: no cover
                return
            row.status = INTENT_REJECTED
            row.error = error
            row.submitted_at = moment
            row.last_checked_at = moment
            row.resolved_at = moment


def apply_reconciliation(
    intent_id: str,
    order: BrokerOrder | None,
    *,
    checked_at: datetime | None = None,
    settle: bool = False,
) -> None:
    """Record what reconciliation found, including that it found nothing.

    `order=None` means the broker has no record of this client order id. That increments a count
    rather than settling anything: a submission can be answered late, and a single not-found is
    not evidence that a timed-out submission failed. `settle=True` is the caller saying its own
    threshold has been met -- `app.order_executor` is where that threshold and its reasoning live,
    because how much patience a submission gets is a policy rather than a fact about the row.
    """
    moment = checked_at or utc_now()
    with SessionLocal() as session:
        with transaction(session):
            row = session.get(OrderIntent, intent_id, with_for_update=True)
            if row is None:  # pragma: no cover
                return
            if order is not None:
                _apply_broker_order(row, order, moment)
                row.uncertain_checks = 0
                return
            row.uncertain_checks += 1
            row.last_checked_at = moment
            if settle:
                _mark_missing(row, moment)


def _mark_missing(row: OrderIntent, moment: datetime) -> None:
    """Settle an order the broker has no record of, with the sentence that says what that means."""
    row.status = INTENT_NOT_FOUND
    row.resolved_at = moment
    row.last_checked_at = moment
    row.error = (
        "Alpaca has no order with this client order id. The submission was never confirmed and "
        "has not been resent: sending it again could create a second order for a decision that "
        "was made once."
    )


def _apply_broker_order(row: OrderIntent, order: BrokerOrder, moment: datetime) -> None:
    """Copy the broker's report onto an intent, keeping its own word beside the mapped one."""
    status = map_broker_status(order.status)
    row.broker_order_id = order.order_id
    row.broker_status = order.status
    row.filled_quantity = order.filled_quantity
    row.filled_avg_price = order.filled_avg_price
    row.status = status
    row.last_checked_at = moment
    if row.submitted_at is None:
        row.submitted_at = order.submitted_at or moment
    if status in TERMINAL_INTENT_STATUSES:
        row.resolved_at = moment


def halt_planned(batch_id: str, *, reason: str, now: datetime | None = None) -> int:
    """Halt every order in this batch that has not been sent yet. Returns how many.

    Used when something has made the rest of the batch wrong to send -- a rejected sell, a session
    that has ended, a portfolio that moved underneath it. Nothing already sent is touched.
    """
    moment = now or utc_now()
    with SessionLocal() as session:
        with transaction(session):
            result = session.execute(
                update(OrderIntent)
                .where(
                    OrderIntent.batch_id == batch_id,
                    OrderIntent.status == INTENT_PLANNED,
                )
                .values(status=INTENT_HALTED, error=reason, resolved_at=moment)
            )
            return int(result.rowcount or 0)


def set_batch_status(
    batch_id: str,
    *,
    status: str,
    halt_reason: str | None = None,
    notes: Sequence[str] | None = None,
    now: datetime | None = None,
) -> None:
    """Move a batch on, and record why if it stopped."""
    moment = now or utc_now()
    with SessionLocal() as session:
        with transaction(session):
            row = session.get(ExecutionBatch, batch_id, with_for_update=True)
            if row is None:  # pragma: no cover
                return
            row.status = status
            row.updated_at = moment
            if row.started_at is None:
                row.started_at = moment
            if status in TERMINAL_BATCH_STATUSES:
                row.completed_at = moment
            if halt_reason is not None:
                row.halt_reason = halt_reason
            if notes is not None:
                row.notes = list(notes)


def add_note(batch_id: str, note: str, *, now: datetime | None = None) -> None:
    """Append a sentence to a batch's notes, once. Repeating a note does not duplicate it."""
    moment = now or utc_now()
    with SessionLocal() as session:
        with transaction(session):
            row = session.get(ExecutionBatch, batch_id, with_for_update=True)
            if row is None:  # pragma: no cover
                return
            notes = list(row.notes or [])
            if note not in notes:
                notes.append(note)
                row.notes = notes
            row.updated_at = moment


def record_sync(
    batch_id: str, *, status: str, error: str | None = None, now: datetime | None = None
) -> None:
    """Record the portfolio sync's outcome, separately from the execution's.

    Two facts that a single status column would blur: an execution can be complete while the
    portfolio it filled into could not be updated, and a reader needs to be able to tell that
    apart from an order that failed.
    """
    moment = now or utc_now()
    with SessionLocal() as session:
        with transaction(session):
            row = session.get(ExecutionBatch, batch_id, with_for_update=True)
            if row is None:  # pragma: no cover
                return
            row.sync_status = status
            row.sync_error = error
            row.sync_at = moment
            row.updated_at = moment


__all__ = [
    "ACTIVE_BATCH_STATUSES",
    "BATCH_COMPLETED",
    "BATCH_HALTED",
    "BATCH_PARTIAL",
    "BATCH_PENDING",
    "BATCH_SUBMITTING",
    "BATCH_MAX_AGE_HOURS",
    "BatchConflictError",
    "DEAD_STATUSES",
    "DECISION_APPROVED",
    "DECISION_REJECTED",
    "DecisionTakenError",
    "FILLED_STATUSES",
    "INTENT_ACCEPTED",
    "INTENT_FILLED",
    "INTENT_HALTED",
    "INTENT_NOT_FOUND",
    "INTENT_PARTIALLY_FILLED",
    "INTENT_PLANNED",
    "INTENT_REJECTED",
    "INTENT_SUBMITTING",
    "INTENT_UNKNOWN",
    "OPEN_STATUSES",
    "Recorded",
    "SYNC_FAILED",
    "SYNC_OK",
    "SYNC_PENDING",
    "TERMINAL_BATCH_STATUSES",
    "TERMINAL_INTENT_STATUSES",
    "add_note",
    "active_batch",
    "apply_reconciliation",
    "approve",
    "batch",
    "batch_for_proposal",
    "batches",
    "claim_intent",
    "client_order_id",
    "decision_for",
    "expire_stale_batches",
    "halt_planned",
    "map_broker_status",
    "new_id",
    "record_refusal",
    "record_rejection",
    "record_submission",
    "record_sync",
    "record_uncertain",
    "set_batch_status",
]
