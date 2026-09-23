"""The proposal API: generate one, watch one being generated, and read the last one back.

Three routes and one rule about what they claim. `POST /rebalance/proposal` generates and returns
what it produced; `POST /rebalance/proposal/stream` does the same work and reports which stage it
is in while it does it; `GET /rebalance/proposal` returns the latest stored proposal, which is
what a page restoring itself on reload reads.

**Nothing here submits an order, and there is no route that could.** This milestone ends at
generation: no approval, no execution, no simulated fill, and no broker write of any kind. The
one write this module makes is to its own proposal table.

**The portfolio is synchronised first, and a sync that fails is reported.** Reading the stored
snapshot without trying is how a proposal comes to describe an account that moved an hour ago,
so the run begins with a sync and ends with `unavailable` if it does not succeed -- with the
broker's own reason, never a quiet fall back to older holdings.

**Handlers are `def`, not `async def`,** for the same reason `app.analysis_api`'s are: the work
blocks on httpx and psycopg2, and FastAPI runs a plain `def` path operation in its threadpool, so
`/health` keeps answering while a proposal is being generated.

**One run at a time, enforced by the database.** `app.proposals` owns a single-slot claim backed
by a partial unique index, so a second request while one is running is refused rather than
queued, and a retry of the *same* request is answered from the row it already has.
"""

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app import portfolio_identity, proposal_run, proposals
from app.agent.module2 import STATUS_UNAVAILABLE
from app.agent.progress import (
    KIND_DONE,
    KIND_ERROR,
    NULL_PROGRESS,
    Progress,
    ProgressEvent,
    QueueProgress,
)
from app.agent.recovery import recover_expired
from app.db import SessionLocal
from app.rebalance_snapshot import compare
from app.schemas import (
    RebalanceProposalOut,
    RebalanceProposalRequest,
    RebalanceProposalView,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rebalance", tags=["rebalance"])

# How long a client should wait before retrying a refused request. Matches the analysis routes:
# there is no queue, so the answer is "not now, shortly".
RETRY_AFTER_SECONDS = 15

# How many progress events a slow client may fall behind by before the oldest are dropped.
STREAM_QUEUE_SIZE = 32

# The run's own ceiling, used to set a claim's processing deadline. Read from the run's budget
# rather than repeated, so the two cannot drift apart.
_DEADLINE_SECONDS = proposal_run._DEADLINE_SECONDS


# What a run does, as a callable. A dependency rather than a direct call so tests can drive the
# whole HTTP stack -- duplicates, replays, failures -- without a provider, a broker or a model.
ProposalRunner = Callable[..., None]


def get_proposal_runner() -> ProposalRunner:
    """The function that generates a proposal. The real one, unless a test says otherwise."""
    return proposal_run.generate


# --- claiming ------------------------------------------------------------------------------


@dataclass
class Admission:
    """The claim on the run slot, or the response to send instead of doing any work."""

    proposal_id: str | None = None
    thread_id: str | None = None
    response: Response | None = None


def _claim(payload: RebalanceProposalRequest) -> Admission:
    """Take the run slot, or produce the response that answers a duplicate.

    Shared by both POST routes, so `/rebalance/proposal` and its stream cannot answer the same
    request differently -- neither is a second implementation of "may this run?".

    **Recovery runs first.** A run abandoned by a restart holds the only slot, and resuming it is
    both cheaper than starting over and the only way the work it already did is not thrown away.
    It has to happen before the claim because the claim would otherwise be refused by the very
    row it is about to recover -- and because the alternative, marking it interrupted and
    starting again, would silently discard a completed retrieval and any tokens it spent.
    """
    recover_expired()

    match proposals.claim(
        request_id=payload.request_id, budget_seconds=_DEADLINE_SECONDS
    ):
        case proposals.Replay(proposal=stored):
            # Already asked. Whatever it produced -- including a failure, or a run still in
            # flight -- is what comes back. Asking again must not silently start a second
            # workflow: the first one was asked once and paid for once.
            return Admission(response=_replay_response(stored))
        case proposals.Busy(proposal_id=active, deadline=deadline):
            return Admission(
                response=JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            "A proposal is already being generated "
                            f"({active}, deadline "
                            f"{deadline.isoformat() if deadline else 'unknown'}). One runs at "
                            "a time. Read it back with GET /rebalance/proposal, or retry "
                            "shortly."
                        )
                    },
                    headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
                )
            )
        case proposals.Claimed(proposal_id=proposal_id, thread_id=thread_id):
            return Admission(proposal_id=proposal_id, thread_id=thread_id)
        case _:  # pragma: no cover - the union is closed
            raise HTTPException(status_code=500, detail="unrecognised claim outcome")


def _replay_response(stored: dict) -> JSONResponse:
    body = _bodies(stored)
    if stored["status"] == proposals.STATUS_GENERATING:
        # Still running. A 202 rather than a 200: there is nothing to read yet, and a client
        # that treated this as a finished proposal would show an empty one.
        return JSONResponse(
            status_code=202,
            content=body,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    return JSONResponse(status_code=200, content=body)


# --- routes --------------------------------------------------------------------------------


@router.post(
    "/proposal",
    response_model=RebalanceProposalView,
    summary="Generate a rebalance proposal",
    responses={
        200: {"description": "The proposal this request produced, or the one it already had."},
        202: {"description": "This request is already being processed."},
        409: {"description": "Another proposal is already being generated."},
        422: {"description": "The body was not acceptable."},
    },
)
def post_proposal(
    payload: RebalanceProposalRequest,
    runner: ProposalRunner = Depends(get_proposal_runner),
) -> Response:
    """Synchronise the portfolio, propose target weights, and price the trades.

    The work is done on this request's thread -- FastAPI's threadpool, because this handler is a
    plain `def` -- and the response is the stored proposal rather than a value built beside it,
    so what a client reads is exactly what was recorded.
    """
    admission = _claim(payload)
    if admission.proposal_id is None:
        assert admission.response is not None  # noqa: S101 - the union is closed
        return admission.response

    try:
        runner(
            proposal_id=admission.proposal_id,
            thread_id=admission.thread_id,
            progress=NULL_PROGRESS,
        )
    except Exception:  # noqa: BLE001 - recorded, never swallowed silently
        # A runner that raised rather than recording its own outcome. The row is still there and
        # still says `generating`, and leaving it that way would hold the only slot for ever --
        # so it is closed out here, with a sentence that says what happened and does not claim
        # anything about the portfolio.
        logger.exception("the proposal run raised for %s", admission.proposal_id)
        _abandon(admission.proposal_id)

    return _proposal_response(admission.proposal_id)


@router.post(
    "/proposal/stream",
    summary="Generate a rebalance proposal and watch the stages",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "text/event-stream. Events: stage, then done or error. Every event names a "
                "stage the run actually has."
            ),
            "content": {"text/event-stream": {}},
        },
        202: {"description": "This request is already being processed."},
        409: {"description": "Another proposal is already being generated."},
        422: {"description": "The body was not acceptable."},
    },
)
def post_proposal_stream(
    payload: RebalanceProposalRequest,
    runner: ProposalRunner = Depends(get_proposal_runner),
) -> Response:
    """The same run as `/rebalance/proposal`, with its stages reported as they happen.

    Everything that decides whether a run may start is identical to the JSON route, because both
    call the same `_claim`: a duplicate, a conflict and a refusal all produce the same JSON
    here as there, and the stream only begins once a run has genuinely been claimed. That is what
    stops an error arriving as a half-open event stream.

    The work does not run on this request's thread. It runs on a worker of its own and this
    response only reads a queue, so closing the browser stops the reading and the run finishes
    and records itself regardless.
    """
    admission = _claim(payload)
    if admission.proposal_id is None:
        assert admission.response is not None  # noqa: S101 - the union is closed
        return admission.response

    return _stream_run(admission, runner)


def _stream_run(admission: Admission, runner: ProposalRunner) -> StreamingResponse:
    events: "queue.Queue[ProgressEvent]" = queue.Queue(maxsize=STREAM_QUEUE_SIZE)
    emitter = QueueProgress(events, maxsize=STREAM_QUEUE_SIZE)
    proposal_id = admission.proposal_id
    assert proposal_id is not None  # noqa: S101 - the union is closed

    def work() -> None:
        try:
            try:
                runner(
                    proposal_id=proposal_id,
                    thread_id=admission.thread_id,
                    progress=emitter,
                )
            except Exception:  # noqa: BLE001 - recorded, never swallowed silently
                logger.exception("the proposal run raised for %s", proposal_id)
                _abandon(proposal_id)

            # The outcome is also the last event, not only the stored row: a client whose stream
            # broke can still read what happened from GET /rebalance/proposal. The event's data
            # *is* the response body, spread rather than nested, so the same payload a client
            # gets from the JSON route is the one it gets here.
            emitter(ProgressEvent(KIND_DONE, _bodies(_require(proposal_id))))
        except Exception as exc:  # noqa: BLE001
            logger.exception("the proposal stream worker failed for %s", proposal_id)
            emitter(
                ProgressEvent(
                    KIND_ERROR,
                    {
                        "detail": (
                            "The proposal could not be generated: "
                            f"{type(exc).__name__}."
                        )
                    },
                )
            )

    threading.Thread(target=work, name=f"proposal-{proposal_id}", daemon=True).start()

    return StreamingResponse(
        _drain(events),
        media_type="text/event-stream",
        headers={
            # Nothing between here and the browser may buffer this.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _drain(events: "queue.Queue[ProgressEvent]") -> Iterator[str]:
    """Yield queued events as SSE frames until a terminal one."""
    while True:
        event = events.get()
        yield _sse(event)
        if event.terminal:
            return


def _sse(event: ProgressEvent) -> str:
    """One server-sent event, in the same shape the analysis stream writes."""
    return f"event: {event.kind}\ndata: {json.dumps(event.as_dict())}\n\n"


@router.get(
    "/proposal",
    response_model=RebalanceProposalView,
    summary="Read the latest proposal",
)
def get_proposal() -> RebalanceProposalView:
    """The most recently generated proposal, or an explicit null.

    This is what the page reads on load. `outdated` is computed here rather than stored, because
    it is a fact about *now*: the portfolio's current quantities, prices, cash and price source
    are compared against the ones frozen on the record, and a proposal whose basis has not moved
    is current however long ago it was generated.
    """
    stored = proposals.latest()
    if stored is None:
        return RebalanceProposalView(proposal=None)
    return RebalanceProposalView(proposal=_to_out(stored))


# --- responses ------------------------------------------------------------------------------


def _proposal_response(proposal_id: str) -> JSONResponse:
    stored = _require(proposal_id)
    if stored["status"] == proposals.STATUS_GENERATING:  # pragma: no cover - finalized above
        return JSONResponse(
            status_code=202,
            content=_bodies(stored),
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    return JSONResponse(status_code=200, content=_bodies(stored))


def _require(proposal_id: str) -> dict:
    stored = proposals.get(proposal_id)
    if stored is None:  # pragma: no cover - the row was inserted by the claim
        raise HTTPException(status_code=500, detail="the proposal row disappeared")
    return stored


def _bodies(stored: dict) -> dict:
    """The response body: the proposal under `proposal`, exactly as stored and as read back.

    One shape for the POST response, the replay, the stream's final event and the GET, so a
    client has one thing to render rather than four that agree today.
    """
    return {"proposal": _to_out(stored).model_dump(mode="json")}


def _to_out(stored: dict) -> RebalanceProposalOut:
    """The stored row as the API returns it, with `outdated` decided now.

    The stored values are passed straight through to the schema, which parses the decimal
    strings back into `Decimal` and re-serialises them. Nothing is recomputed: a proposal is a
    record of what was calculated, and recalculating it here would show today's arithmetic under
    yesterday's evidence.
    """
    state, reason, changed = _freshness(stored)
    return RebalanceProposalOut.model_validate(
        {
            **stored,
            # An empty JSONB object is what a row carries before the run has written anything --
            # a refused or interrupted run, or one still in flight. It means "nothing here", and
            # passing it through as an empty *object* would fail validation on fields that a
            # value-less block genuinely has none of.
            "snapshot": stored.get("snapshot") or None,
            "policy": stored.get("policy") or None,
            "calculation": stored.get("calculation") or None,
            "targets": stored.get("targets") or None,
            "freshness": state,
            "freshness_reason": reason,
            "freshness_changed": changed,
        }
    )


def _freshness(stored: dict) -> tuple[str, str | None, list[str]]:
    """How the stored proposal stands against the portfolio now.

    Asked only of a finished proposal with a snapshot on it. A run still in flight has nothing to
    compare, and an `unavailable` proposal with no snapshot has no basis to be compared against.

    A portfolio that cannot be read at all is reported as `unknown` rather than as changed -- an
    outage is not a fact about the holdings, and saying "the portfolio changed" on the strength of
    not being able to look at it is the mistake the whole comparison is written to avoid.
    """
    from app.rebalance_snapshot import Freshness

    snapshot = stored.get("snapshot") or {}
    if not snapshot or stored.get("status") == proposals.STATUS_GENERATING:
        return str(Freshness.UNKNOWN), None, []

    try:
        with SessionLocal() as session:
            portfolio = portfolio_identity.find(session)
            if portfolio is None:
                return (
                    str(Freshness.UNKNOWN),
                    f"No portfolio named {portfolio_identity.DEMO_PORTFOLIO_NAME!r} is stored, "
                    "so this proposal cannot be checked against one.",
                    [],
                )
            comparison = compare(snapshot, portfolio)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.warning("the portfolio could not be read to check a proposal's freshness")
        return (
            str(Freshness.UNKNOWN),
            "The portfolio could not be read, so this proposal's currency cannot be checked.",
            [],
        )

    return str(comparison.state), comparison.reason, list(comparison.changed)


def _abandon(proposal_id: str) -> None:
    """Close out a run that raised, so the single slot is not held for ever.

    Only if the row still says `generating`. If the run recorded its own outcome before raising,
    that outcome is what stands -- overwriting a real result with a service failure would turn a
    proposal into an outage after the fact.
    """
    stored = proposals.get(proposal_id)
    if stored is None or stored["status"] != proposals.STATUS_GENERATING:
        return

    proposals.finalize(
        proposal_id=proposal_id,
        status=STATUS_UNAVAILABLE,
        failure=(
            "The proposal run failed unexpectedly and recorded no outcome. This is a fault in "
            "the application, not a statement about the portfolio, and nothing was sent to a "
            "broker."
        ),
        failure_reason=proposal_run.REASON_SERVICE_FAILED,
    )


__all__ = [
    "RETRY_AFTER_SECONDS",
    "get_proposal_runner",
    "router",
]
