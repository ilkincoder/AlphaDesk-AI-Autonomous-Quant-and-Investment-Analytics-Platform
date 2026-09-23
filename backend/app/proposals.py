"""Storing a rebalance proposal: claiming the one active run, and settling it.

The same shape as `app.conversations`, for the same reasons, over a single-slot resource rather
than a conversation. Every function opens its own short-lived session: the workflow between the
claim and the finalize calls a model and a provider and can take a minute, and a session or a
lock held across it would pin a connection and turn a slow provider into a database problem.

1. **Claim** -- recover any expired run, refuse if one is live, insert this one, commit. A retry
   arriving while the first run is in flight can now see it, which is what stops it paying for a
   second one.
2. *...the workflow, with nothing open...*
3. **Finalize** -- write the snapshot, the evidence, the calculation and the status, in one
   transaction.

**There is exactly one slot.** The database enforces it with a partial unique index rather than
this module checking first, because a check followed by an insert is a race two requests can
both win. What this module does is decide which *answer* a losing request gets -- a replay of
what already exists, or "one is already running" -- rather than letting it hit an integrity
error.

**Nothing is ever replayed automatically.** A run that overran its deadline is marked
interrupted, and that is all: whatever the provider was asked and charged for is already spent.
Producing the proposal again is a decision with a new request id, and it is the user's.

**A stored row is read as values, never as an ORM instance.** The session that read it closes
before the caller uses it, and an instance outliving its session raises the moment an attribute
is touched. Everything crossing this module's boundary is a plain dict.
"""

import contextlib
import logging
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.conversations import PROCESSING_GRACE_SECONDS
from app.db import SessionLocal
from app.models import RebalanceProposal

logger = logging.getLogger(__name__)

# Proposal statuses that belong to the store rather than to a run.
STATUS_GENERATING = "generating"
STATUS_INTERRUPTED = "interrupted"

# The statuses that mean the run is over, whichever way it went.
TERMINAL_STATUSES = frozenset({"proposed", "no_change", "unavailable", "interrupted"})


def new_id() -> str:
    """A server-generated identifier, not a sequence -- see `app.conversations.new_id`."""
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextlib.contextmanager
def _transaction(session: Session) -> Iterator[None]:
    """One atomic unit of work, inside a caller's transaction if there is one."""
    if session.in_transaction():
        yield
    else:
        with session.begin():
            yield


# --- outcomes of trying to claim the slot ------------------------------------------------


@dataclass(frozen=True)
class Claimed:
    """The run is ours to perform."""

    proposal_id: str
    request_id: str
    thread_id: str


@dataclass(frozen=True)
class Replay:
    """This request id has been seen before, and its outcome is already recorded.

    Whatever that outcome was, including a failure or a run still in flight. A retry must not
    silently start a second workflow: the first one was asked once, and its answer -- or its
    failure -- belongs to the request that asked for it.
    """

    proposal: dict


@dataclass(frozen=True)
class Busy:
    """Another proposal run is in flight, and it is not this one."""

    proposal_id: str
    deadline: datetime | None


ClaimOutcome = Claimed | Replay | Busy


# --- claiming -----------------------------------------------------------------------------


def claim(*, request_id: str, budget_seconds: float) -> ClaimOutcome:
    """Take the single run slot for this request, or explain why not.

    One transaction, and the database is the arbiter: the unique constraint on `request_id` and
    the partial unique index on the active status, not an in-process lock. A dictionary of
    in-flight requests would work perfectly until the second worker process existed.
    """
    with SessionLocal() as session:
        with _transaction(session):
            _recover_expired(session)

            existing = session.scalar(
                select(RebalanceProposal).where(
                    RebalanceProposal.request_id == request_id
                )
            )
            if existing is not None:
                return Replay(proposal=_summary(existing))

            active = session.scalar(
                select(RebalanceProposal).where(
                    RebalanceProposal.status == STATUS_GENERATING
                )
            )
            if active is not None:
                # Refused rather than queued. A queue would turn a slow provider into an
                # ever-growing backlog, and both runs would be reasoning about the same
                # portfolio at the same time.
                return Busy(
                    proposal_id=active.id, deadline=active.processing_deadline
                )

            now = _now()
            identifier = new_id()
            proposal = RebalanceProposal(
                id=identifier,
                request_id=request_id,
                # One run, one thread. Written here, at the claim, because the identity has to
                # exist before the graph does: a run that failed half way through still has a
                # thread to name, and a thread derived at finalize time would be absent from
                # exactly the rows that most need one.
                thread_id=f"rebalance-{identifier}",
                status=STATUS_GENERATING,
                created_at=now,
                started_at=now,
                processing_deadline=now
                + timedelta(seconds=budget_seconds + PROCESSING_GRACE_SECONDS),
                snapshot={},
                snapshot_fingerprint="",
                policy={},
                evidence=[],
                assumptions=[],
                limitations=[],
            )
            session.add(proposal)
            session.flush()

            return Claimed(
                proposal_id=proposal.id,
                request_id=request_id,
                thread_id=proposal.thread_id,
            )


def renew(proposal_id: str, *, seconds: float) -> bool:
    """Extend a run's lease, if it still holds one that has expired.

    This is how a recovery takes ownership: it is a compare-and-set on the deadline, inside one
    transaction, so two processes that both noticed the same abandoned run cannot both decide to
    resume it. The winner gets a fresh deadline and the run; the loser is told no and leaves the
    row alone.

    Returns False for a row that is missing, already finished, or still leased by somebody else
    -- all three of which mean "not yours to resume", and none of which is an error.
    """
    now = _now()
    with SessionLocal() as session:
        with _transaction(session):
            proposal = session.get(RebalanceProposal, proposal_id, with_for_update=True)
            if proposal is None or proposal.status != STATUS_GENERATING:
                return False

            deadline = proposal.processing_deadline
            if deadline is not None and _aware(deadline) > now:
                return False

            proposal.processing_deadline = now + timedelta(seconds=seconds)
            return True


def _aware(value: datetime) -> datetime:
    """A stored time as an aware one. A naive value is read as UTC rather than guessed at."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# --- finishing ----------------------------------------------------------------------------


def finalize(
    *,
    proposal_id: str,
    status: str,
    snapshot: Mapping[str, object] | None = None,
    snapshot_fingerprint: str | None = None,
    policy: Mapping[str, object] | None = None,
    targets: Mapping[str, object] | None = None,
    rationale: str | None = None,
    evidence: Sequence[Mapping[str, object]] | None = None,
    calculation: Mapping[str, object] | None = None,
    assumptions: Sequence[str] | None = None,
    limitations: Sequence[str] | None = None,
    usage: Mapping[str, object] | None = None,
    failure: str | None = None,
    failure_reason: str | None = None,
    run_id: str | None = None,
) -> str:
    """Write what the run produced, and release the slot.

    Every field is written together with the status, so a proposal can never be recorded as
    finished without the evidence and the calculation that justify it. That is the same reason
    `app.conversations.finalize_turn` writes its context and its lease in one transaction.

    Written even if the run produced nothing: the work was done and paid for, and a row that
    says *why* there is no proposal is more useful than one that was never written.
    """
    with SessionLocal() as session:
        with _transaction(session):
            proposal = session.get(RebalanceProposal, proposal_id)
            if proposal is None:  # pragma: no cover - inserted by `claim`
                logger.warning("finalizing a proposal that does not exist: %s", proposal_id)
                return STATUS_INTERRUPTED

            proposal.status = status
            proposal.completed_at = _now()
            proposal.processing_deadline = None
            proposal.failure = failure
            proposal.failure_reason = failure_reason
            if run_id is not None:
                proposal.run_id = run_id

            if snapshot is not None:
                proposal.snapshot = dict(snapshot)
            if snapshot_fingerprint is not None:
                proposal.snapshot_fingerprint = snapshot_fingerprint
            if policy is not None:
                proposal.policy = dict(policy)
            if targets is not None:
                proposal.targets = dict(targets)
            if rationale is not None:
                proposal.rationale = rationale
            if evidence is not None:
                proposal.evidence = [dict(item) for item in evidence]
            if calculation is not None:
                proposal.calculation = dict(calculation)
            if assumptions is not None:
                proposal.assumptions = list(assumptions)
            if limitations is not None:
                proposal.limitations = list(limitations)
            if usage is not None:
                proposal.usage = dict(usage)

            return proposal.status


# --- reading ------------------------------------------------------------------------------


def latest() -> dict | None:
    """The most recently created proposal, or None if none has been generated.

    The *latest*, not the latest successful one: a proposal that could not be produced is still
    the most recent thing that happened, and hiding it would leave a reader looking at an older
    proposal as though it were current.
    """
    with SessionLocal() as session:
        with _transaction(session):
            _recover_expired(session)

        proposal = session.scalars(
            select(RebalanceProposal)
            .order_by(RebalanceProposal.created_at.desc(), RebalanceProposal.id.desc())
            .limit(1)
        ).first()
        return None if proposal is None else _summary(proposal)


def running() -> list[dict]:
    """Every proposal still marked as running, as plain values.

    Read without locking anything. Whoever acts on an id still has to win the lease in `renew`,
    so noticing a row twice is harmless -- which is what lets a startup sweep and a request-path
    sweep both ask this without coordinating.
    """
    with SessionLocal() as session:
        rows = session.scalars(
            select(RebalanceProposal).where(
                RebalanceProposal.status == STATUS_GENERATING
            )
        ).all()
        return [_summary(row) for row in rows]


def get(proposal_id: str) -> dict | None:
    """One proposal by id, for a caller that already knows which one it wants."""
    with SessionLocal() as session:
        proposal = session.get(RebalanceProposal, proposal_id)
        return None if proposal is None else _summary(proposal)


# --- recovery -----------------------------------------------------------------------------


def _recover_expired(session: Session) -> str | None:
    """Free the slot if the run holding it has passed its deadline.

    Called inside `claim` and `latest`, both of which take the same short transaction. Recovery
    marks the run interrupted; it never replays it. Whatever the provider was asked and charged
    for is already spent, and doing it again without being asked would spend it twice.
    """
    active = session.scalars(
        select(RebalanceProposal).where(
            RebalanceProposal.status == STATUS_GENERATING
        )
    ).all()
    if not active:
        return None

    recovered: str | None = None
    for proposal in active:
        deadline = proposal.processing_deadline
        if deadline is None:
            continue
        if deadline.tzinfo is None:
            # A naive value can only come from a hand-written row or a driver that dropped the
            # offset. Treated as UTC rather than guessed at, because the alternative is
            # comparing it against an aware value and raising here.
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline > _now():
            continue

        logger.warning(
            "proposal %s passed its deadline without finishing; marking it interrupted",
            proposal.id,
        )
        proposal.status = STATUS_INTERRUPTED
        proposal.completed_at = _now()
        proposal.processing_deadline = None
        proposal.failure = (
            "This proposal did not finish within its processing deadline. It was interrupted, "
            "not completed, and its generation was not retried."
        )
        proposal.failure_reason = STATUS_INTERRUPTED
        recovered = proposal.id

    return recovered


# --- shapes -------------------------------------------------------------------------------


def _summary(proposal: RebalanceProposal) -> dict:
    """One proposal as the API returns it. Plain values throughout; see the module docstring."""
    return {
        "proposal_id": proposal.id,
        "request_id": proposal.request_id,
        "status": proposal.status,
        "created_at": _iso(proposal.created_at),
        "completed_at": _iso(proposal.completed_at),
        "processing_deadline": _iso(proposal.processing_deadline),
        "run_id": proposal.run_id,
        "thread_id": proposal.thread_id,
        "snapshot": proposal.snapshot or {},
        "snapshot_fingerprint": proposal.snapshot_fingerprint,
        "policy": proposal.policy or {},
        "targets": proposal.targets,
        "rationale": proposal.rationale,
        "evidence": list(proposal.evidence or []),
        "calculation": proposal.calculation,
        "assumptions": list(proposal.assumptions or []),
        "limitations": list(proposal.limitations or []),
        "usage": proposal.usage,
        "failure": proposal.failure,
        "failure_reason": proposal.failure_reason,
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = [
    "Busy",
    "ClaimOutcome",
    "Claimed",
    "Replay",
    "STATUS_GENERATING",
    "STATUS_INTERRUPTED",
    "TERMINAL_STATUSES",
    "claim",
    "finalize",
    "get",
    "latest",
    "new_id",
    "renew",
    "running",
]
