"""Storing a conversation: creating it, claiming a turn, and settling one.

Every function here opens its own short-lived session and closes it again. That is not a
style preference. The analysis between the claim and the finalize takes as long as a model
takes, and a session or a row lock held across it would pin a connection for three minutes,
block anyone else touching the conversation, and turn a slow provider into a database problem.

The shape of a turn's life is therefore two short transactions with a long gap between them:

1. **Claim** -- insert the turn and take the conversation's lease, committed immediately. A
   retry arriving while the analysis runs can now see that the work is already happening,
   which is what stops it paying for a second one.
2. *...the analysis, with nothing open...*
3. **Finalize** -- write the answer, the citations and the resolved context, and release the
   lease, all in one transaction so a turn can never be recorded without the context it
   produced.

**The lease is what makes overlapping turns detectable.** It is a turn id and a deadline on
the conversation row, not a lock: nothing is held, and a turn that overran is *recoverable*
rather than permanently blocking. Recovery is lazy -- the next request that touches the
conversation past the deadline marks the abandoned turn interrupted and frees it. There is no
background sweeper, and nothing is ever replayed automatically: a paid request that died is
a paid request that died, and replaying it is the user's decision, with a new request id.
"""

import contextlib
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.context import (
    MAX_CONTEXT_TURNS,
    ConversationContext,
    PendingClarification,
    PriorTurn,
    SettledContext,
)
from app.agent.run import STATUS_CLARIFICATION_NEEDED, RunResult
from app.db import SessionLocal
from app.models import Conversation, ConversationTurn

logger = logging.getLogger(__name__)

# Turn statuses that belong to the store rather than to a run.
TURN_PROCESSING = "processing"
TURN_INTERRUPTED = "interrupted"

# How long after a run's own deadline a turn stops being believed. The run is bounded by its
# budget; this is the margin for the work either side of it -- persisting the result, a slow
# database, a garbage collection pause. Past it, the turn is assumed dead rather than slow.
PROCESSING_GRACE_SECONDS = 30

# Default and maximum page size for history.
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100


# --- outcomes of trying to claim a turn ------------------------------------------------


@dataclass(frozen=True)
class Claimed:
    """The turn is ours to run."""

    turn_id: str
    conversation_id: str
    sequence: int
    reference_date: date
    context: ConversationContext


@dataclass(frozen=True)
class Replay:
    """This exact request has been made before, and its outcome is already recorded.

    Returned rather than re-run, whatever that recorded outcome was -- including a failure.
    An interrupted or failed request must not silently rerun on a retry: the user asked once,
    it went wrong once, and doing it again without being asked would spend their money to
    produce a second thing to be confused about.

    `turn` is a plain mapping, not the ORM object, and that is deliberate. The session that
    read it closes before the caller uses it, and an ORM instance outliving its session raises
    the moment any attribute is touched -- which is precisely what happened the first time
    this path ran against the live server. Everything crossing this boundary is a value.
    """

    turn: dict


@dataclass(frozen=True)
class Busy:
    """Another turn in this conversation is running, and it is not this one."""

    turn_id: str
    deadline: datetime | None


@dataclass(frozen=True)
class Conflicted:
    """This request id was used before for something else."""

    detail: str


@dataclass(frozen=True)
class Missing:
    """No such conversation."""

    conversation_id: str


ClaimOutcome = Claimed | Replay | Busy | Conflicted | Missing


def new_id() -> str:
    """A server-generated identifier. Not a sequence: a small integer would let anyone
    holding one id enumerate the conversations beside it."""
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextlib.contextmanager
def _transaction(session: Session) -> Iterator[None]:
    """One atomic unit of work, inside a caller's transaction if there is one.

    Normally this opens and commits its own. If the session is *already* in a transaction --
    because a caller opened one and handed the session in -- this joins it and leaves the
    commit to whoever owns it. Committing a transaction somebody else began would end work
    they were still assembling, and rolling it back would discard work they had already done.
    """
    if session.in_transaction():
        yield
    else:
        with session.begin():
            yield


# --- creating and reading ---------------------------------------------------------------


def create_conversation() -> dict:
    """A conversation with nothing in it. Deliberately calls no model.

    Creating a conversation is opening a place to talk, not asking a question, and it should
    not cost anything or fail because a provider is down.
    """
    now = _now()
    conversation = Conversation(
        id=new_id(), created_at=now, updated_at=now
    )
    with SessionLocal() as session:
        with _transaction(session):
            session.add(conversation)
        return _conversation_summary(conversation)


def get_conversation(conversation_id: str) -> dict | None:
    """Metadata and a page of history, or None if there is no such conversation."""
    with SessionLocal() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return None
        with _transaction(session):
            _recover_expired(session, conversation)
        return _conversation_summary(conversation)


def history(
    conversation_id: str,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
    offset: int = 0,
    include_results: bool = True,
) -> dict | None:
    """One page of turns, oldest first, with the total so a client can page.

    Unbounded history is available here even though the *prompt* sees only a few turns: the
    context window is a cost control, and a client reading its own conversation is not paying
    for tokens.

    `include_results` carries each turn's citations, evidence and limitations. On by default,
    because a page that restores the answers without the things that make them checkable is a
    page that has quietly become less trustworthy than the one before the reload.
    """
    with SessionLocal() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return None
        with _transaction(session):
            _recover_expired(session, conversation)

        total = session.scalar(
            select(func.count())
            .select_from(ConversationTurn)
            .where(ConversationTurn.conversation_id == conversation_id)
        )
        turns = session.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.conversation_id == conversation_id)
            .order_by(ConversationTurn.sequence)
            .limit(limit)
            .offset(offset)
        ).all()

        # The conversation's state travels with the page: whether a clarification is
        # waiting and whether a turn is running is what a client needs to decide what to do
        # next, and making it fetch that separately would be two round trips for one answer.
        return {
            **_conversation_summary(conversation),
            "total_turns": total or 0,
            "limit": limit,
            "offset": offset,
            "turns": [
                _turn_summary(turn, include_result=include_results) for turn in turns
            ],
        }


# --- claiming ---------------------------------------------------------------------------


def claim_turn(
    *,
    conversation_id: str,
    request_id: str,
    message: str,
    symbol: str | None,
    start_date: date | None,
    end_date: date | None,
    as_of: date | None,
    budget_seconds: float,
) -> ClaimOutcome:
    """Take the conversation's lease for this turn, or explain why not.

    One transaction, and the database is the arbiter throughout -- the unique constraint on
    `(conversation_id, request_id)` and a row lock on the conversation, not an in-process lock.
    A dictionary of in-flight requests would work perfectly until the second worker process
    existed, and then it would silently stop working.

    The date this turn resolves relative periods against is decided here, because it is a fact
    about the conversation rather than about the request: a reply to a pending clarification
    resolves against the date that clarification was asked on, not against today. The caller
    is told which date was chosen and hands the same one to the run.
    """
    with SessionLocal() as session:
        with _transaction(session):
            # Locked for the duration of the claim only. Nothing downstream holds it.
            conversation = session.scalar(
                select(Conversation)
                .where(Conversation.id == conversation_id)
                .with_for_update()
            )
            if conversation is None:
                return Missing(conversation_id)

            _recover_expired(session, conversation)

            existing = session.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.conversation_id == conversation_id,
                    ConversationTurn.request_id == request_id,
                )
            )
            if existing is not None:
                return _classify_repeat(existing, message, symbol, start_date, end_date, as_of)

            if conversation.processing_turn_id is not None:
                # Some other turn holds the lease and has not expired. Running a second one
                # against the same history would let the two settle context on top of each
                # other, and whichever finished last would win for no reason.
                return Busy(
                    turn_id=conversation.processing_turn_id,
                    deadline=conversation.processing_deadline,
                )

            now = _now()
            deadline = now + timedelta(seconds=budget_seconds + PROCESSING_GRACE_SECONDS)
            # A resumed clarification keeps the date it was asked on. Resolving a reply of
            # "August 6 to September 17" against today would move the window the user was
            # originally asking about, silently and by however long they took to reply.
            reference_date = conversation.pending_reference_date or date.today()
            sequence = (
                session.scalar(
                    select(func.coalesce(func.max(ConversationTurn.sequence), 0)).where(
                        ConversationTurn.conversation_id == conversation_id
                    )
                )
                or 0
            ) + 1

            turn = ConversationTurn(
                id=new_id(),
                conversation_id=conversation_id,
                request_id=request_id,
                sequence=sequence,
                user_message=message,
                request_symbol=symbol,
                request_start_date=start_date,
                request_end_date=end_date,
                request_as_of=as_of,
                reference_date=reference_date,
                status=TURN_PROCESSING,
                created_at=now,
                started_at=now,
                processing_deadline=deadline,
            )
            session.add(turn)

            conversation.processing_turn_id = turn.id
            conversation.processing_deadline = deadline
            conversation.updated_at = now
            session.flush()

            context = _load_context(session, conversation, before_sequence=sequence)
            return Claimed(
                turn_id=turn.id,
                conversation_id=conversation_id,
                sequence=sequence,
                reference_date=reference_date,
                context=context,
            )


def _classify_repeat(
    existing: ConversationTurn,
    message: str,
    symbol: str | None,
    start_date: date | None,
    end_date: date | None,
    as_of: date | None,
) -> ClaimOutcome:
    """What to do about a request id that has been seen before.

    The payload is compared, not just the id. Reusing an id for *different* content is a
    client bug, and answering it with the earlier result would be answering a question nobody
    asked -- so it is refused rather than served.
    """
    same = (
        existing.user_message == message
        and (existing.request_symbol or None) == (symbol or None)
        and existing.request_start_date == start_date
        and existing.request_end_date == end_date
        and existing.request_as_of == as_of
    )
    if not same:
        return Conflicted(
            detail=(
                f"request id {existing.request_id!r} was already used in this conversation "
                "with different content or arguments. A request id identifies one request; "
                "send a new one for a new request."
            )
        )
    return Replay(turn=turn_summary(existing))


# --- finishing --------------------------------------------------------------------------


def finalize_turn(
    *,
    conversation_id: str,
    turn_id: str,
    result: RunResult | None,
    failure: str | None = None,
    status: str | None = None,
    original_question: str | None = None,
) -> str:
    """Record the outcome and update the conversation's context, in one transaction.

    The context update is **leased**: it applies only while this turn still holds the
    conversation. A run that overran its deadline has had its lease recovered and possibly
    handed to a newer turn, and letting the late one write context then would move the
    conversation backwards on the strength of an answer to a question already superseded.

    The turn's own row is written either way. The work was done and paid for; the user asked;
    the answer belongs to that turn whether or not the conversation has moved on.
    """
    now = _now()
    # Where the status comes from, in order: the run's own outcome, then the caller's
    # description of why there is no result, then `interrupted` -- a turn with no result and
    # no explanation is one that stopped, and saying "processing" forever would leave the
    # conversation believing work is still happening.
    settled_status = (
        result.status if result is not None else (status or TURN_INTERRUPTED)
    )

    with SessionLocal() as session:
        with _transaction(session):
            turn = session.get(ConversationTurn, turn_id)
            if turn is None:  # pragma: no cover - the turn was inserted by claim_turn
                logger.warning("finalizing a turn that does not exist: %s", turn_id)
                return TURN_INTERRUPTED

            turn.status = settled_status
            turn.completed_at = now
            turn.processing_deadline = None
            turn.failure = failure
            if result is not None:
                turn.run_id = result.run_id
                turn.answer = result.answer
                turn.result = result.as_json()
                resolved = result.resolved or {}
                turn.resolved_symbol = resolved.get("symbol")
                turn.resolved_start_date = _as_date(resolved.get("start_date"))
                turn.resolved_end_date = _as_date(resolved.get("end_date"))
                turn.resolved_as_of = _as_date(resolved.get("as_of"))

            conversation = session.scalar(
                select(Conversation)
                .where(Conversation.id == conversation_id)
                .with_for_update()
            )
            if conversation is None:  # pragma: no cover - cascades with the turn
                return turn.status

            if conversation.processing_turn_id != turn_id:
                logger.warning(
                    "turn %s finished after losing the lease on %s; its answer is recorded "
                    "but the conversation context is left to the turn that holds it",
                    turn_id,
                    conversation_id,
                )
                return turn.status

            _apply_context(
                conversation,
                result=result,
                now=now,
                original_question=original_question or turn.user_message,
            )
            return turn.status


def _apply_context(
    conversation: Conversation,
    *,
    result: RunResult | None,
    now: datetime,
    original_question: str,
) -> None:
    """Move the conversation's carried context on by one turn, and release the lease.

    Three cases, and the middle one is the whole reason conversations exist:

    * **A clarification** -- keep the *original* question, not the reply. A reply of "August 6
      to September 17" is meaningless on its own, so what has to be resumable is the question
      it was answering. If a clarification is asked twice, the original survives both times.
    * **A settled run** -- record what it settled, and clear the pending question, because it
      has now been answered.
    * **Anything else** -- clear the pending question and leave the settled context alone. A
      turn that resolved nothing has nothing to say about what the conversation is about.
    """
    conversation.processing_turn_id = None
    conversation.processing_deadline = None
    conversation.updated_at = now

    if result is None:
        return

    if result.status == STATUS_CLARIFICATION_NEEDED:
        conversation.pending_question = (
            conversation.pending_question or original_question
        )
        conversation.pending_reference_date = result.reference_date
        conversation.pending_asked_for = result.answer
        return

    conversation.pending_question = None
    conversation.pending_reference_date = None
    conversation.pending_asked_for = None

    if result.resolved:
        conversation.settled_symbol = result.resolved.get("symbol")
        conversation.settled_start_date = _as_date(result.resolved.get("start_date"))
        conversation.settled_end_date = _as_date(result.resolved.get("end_date"))
        conversation.settled_as_of = _as_date(result.resolved.get("as_of"))


# --- recovery -----------------------------------------------------------------------------


def _recover_expired(session: Session, conversation: Conversation) -> str | None:
    """Free a lease whose deadline has passed, marking its turn interrupted.

    Called while the conversation row is already locked, so two requests cannot both recover
    it. Recovery marks the turn; it does not replay it. Whatever the provider was asked and
    whatever it charged for is already spent, and doing it again without being asked would
    spend it twice.
    """
    if conversation.processing_turn_id is None or conversation.processing_deadline is None:
        return None
    if conversation.processing_deadline > _now():
        return None

    abandoned = conversation.processing_turn_id
    logger.warning(
        "turn %s passed its processing deadline without finishing; marking it interrupted",
        abandoned,
    )

    turn = session.get(ConversationTurn, abandoned)
    if turn is not None and turn.status == TURN_PROCESSING:
        turn.status = TURN_INTERRUPTED
        turn.completed_at = _now()
        turn.processing_deadline = None
        turn.failure = (
            "This turn did not finish within its processing deadline. It was interrupted, "
            "not completed. Its analysis was not retried and may have been billed."
        )

    conversation.processing_turn_id = None
    conversation.processing_deadline = None
    conversation.updated_at = _now()
    return abandoned


# --- reading context ----------------------------------------------------------------------


def _load_context(
    session: Session, conversation: Conversation, *, before_sequence: int | None
) -> ConversationContext:
    """The bounded context this turn runs with.

    Only what is needed: what the conversation settled, the question it is waiting on an
    answer to, and a few recent exchanges as digests. The full transcript is available
    through the history endpoint, which a prompt is not paying for.
    """
    settled = None
    if conversation.settled_symbol or conversation.settled_as_of:
        settled = SettledContext(
            symbol=conversation.settled_symbol,
            start_date=conversation.settled_start_date,
            end_date=conversation.settled_end_date,
            as_of=conversation.settled_as_of,
        )

    pending = None
    if conversation.pending_question and conversation.pending_reference_date:
        pending = PendingClarification(
            original_question=conversation.pending_question,
            reference_date=conversation.pending_reference_date,
            asked_for=conversation.pending_asked_for,
        )

    query = select(ConversationTurn).where(
        ConversationTurn.conversation_id == conversation.id,
        ConversationTurn.status != TURN_PROCESSING,
    )
    if before_sequence is not None:
        query = query.where(ConversationTurn.sequence < before_sequence)

    recent = session.scalars(
        query.order_by(ConversationTurn.sequence.desc()).limit(MAX_CONTEXT_TURNS)
    ).all()

    return ConversationContext(
        settled=settled,
        pending=pending,
        recent=tuple(
            PriorTurn(
                user_message=turn.user_message,
                answer=turn.answer,
                status=turn.status,
            )
            for turn in reversed(recent)
        ),
    )


def context_for_new_turn(conversation_id: str) -> ConversationContext | None:
    """The context the *next* turn would run with, for tests and inspection."""
    with SessionLocal() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            return None
        return _load_context(session, conversation, before_sequence=None)


# --- shapes -----------------------------------------------------------------------------


def _conversation_summary(conversation: Conversation) -> dict:
    return {
        "conversation_id": conversation.id,
        "created_at": _iso(conversation.created_at),
        "updated_at": _iso(conversation.updated_at),
        "settled": {
            "symbol": conversation.settled_symbol,
            "start_date": _iso(conversation.settled_start_date),
            "end_date": _iso(conversation.settled_end_date),
            "as_of": _iso(conversation.settled_as_of),
        },
        "pending_clarification": (
            None
            if not conversation.pending_question
            else {
                "question": conversation.pending_question,
                "reference_date": _iso(conversation.pending_reference_date),
                "asked_for": conversation.pending_asked_for,
            }
        ),
        "processing": (
            None
            if conversation.processing_turn_id is None
            else {
                "turn_id": conversation.processing_turn_id,
                "deadline": _iso(conversation.processing_deadline),
            }
        ),
    }


def turn_summary(turn: ConversationTurn) -> dict:
    return _turn_summary(turn, include_result=True)


# The fields a stored run result contributes to a turn. Flattened rather than nested under
# `result`, so a turn read back from history has the *same shape* as one just asked for. A
# client that had to render two shapes for one thing would eventually render one of them wrong.
_RESULT_FIELDS = (
    "destination",
    "route_reason",
    "question",
    "reference_date",
    "model",
    "symbol",
    "information_cutoff",
    "citations",
    "evidence",
    "limitations",
    "next_steps",
    "findings",
    "tool_executions",
    "usage",
    "warnings",
)


def _turn_summary(turn: ConversationTurn, *, include_result: bool) -> dict:
    """One turn as the API returns it.

    `include_result` is what makes a restored conversation as useful as a live one. Without it
    a turn read back from history is an answer with no citations, no evidence and no
    limitations -- everything the answer's qualifications live in -- which would make a
    browser reload quietly degrade the page into something that cannot be checked.
    """
    summary = {
        "turn_id": turn.id,
        "sequence": turn.sequence,
        "request_id": turn.request_id,
        "user_message": turn.user_message,
        "status": turn.status,
        "created_at": _iso(turn.created_at),
        "completed_at": _iso(turn.completed_at),
        "run_id": turn.run_id,
        "answer": turn.answer,
        "resolved": {
            "symbol": turn.resolved_symbol,
            "start_date": _iso(turn.resolved_start_date),
            "end_date": _iso(turn.resolved_end_date),
            "as_of": _iso(turn.resolved_as_of),
        },
        "failure": turn.failure,
    }

    stored = turn.result if include_result else None
    for field in _RESULT_FIELDS:
        # The run's own copy wins where it has one, because it is the authoritative record of
        # what that run produced; the promoted columns are what survive when it does not.
        summary[field] = (stored or {}).get(field)
    return summary


def _iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def _as_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


__all__ = [
    "Busy",
    "ClaimOutcome",
    "Claimed",
    "Conflicted",
    "DEFAULT_HISTORY_LIMIT",
    "MAX_HISTORY_LIMIT",
    "Missing",
    "PROCESSING_GRACE_SECONDS",
    "Replay",
    "TURN_INTERRUPTED",
    "TURN_PROCESSING",
    "claim_turn",
    "context_for_new_turn",
    "create_conversation",
    "finalize_turn",
    "get_conversation",
    "history",
    "new_id",
    "turn_summary",
]
