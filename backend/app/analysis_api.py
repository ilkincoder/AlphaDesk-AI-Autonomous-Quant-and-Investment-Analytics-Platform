"""The analysis chat over HTTP: one conversation, one turn at a time.

Three routes. Creating a conversation calls no model, asking a question runs the whole
Supervisor and Module 1 flow, and reading a conversation returns what was said.

**The handlers are `def`, not `async def`, and that is the point.** `run_analysis` blocks -- on
httpx waiting for DeepSeek, on psycopg2 waiting for PostgreSQL -- and FastAPI's own guidance is
that a blocking path operation declared with plain `def` runs in an external threadpool
instead of on the event loop. That is what lets `/health` answer in milliseconds while a
three-minute analysis is in flight. An `async def` handler calling blocking code would stall
every other request in the process, including the health check, which is exactly the failure
this choice avoids. The cost is one thread per running analysis, which is why there is a
ceiling on how many may run at once.

**Nothing is held across the model call.** The turn is claimed in one short transaction, the
analysis runs with no session and no lock open, and the result is written in a second short
transaction. A three-minute provider call must not be able to look like a database problem.

**HTTP status is derived from the turn's status**, so the same request id always produces the
same status code -- on the first attempt and on every replay. A client can therefore treat a
retry as genuinely idempotent rather than as a coin flip.

This is a local, single-user development application. A conversation id separates conversations
from one another; it is **not** an authorization boundary, and nothing here should be exposed
to more than one user without authentication and ownership checks first. See the README.
"""

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app import conversations
from app.agent.budget import RunBudget
from app.agent.progress import (
    KIND_DONE,
    KIND_ERROR,
    ProgressEvent,
    QueueProgress,
)
from app.agent.run import (
    STATUS_PROVIDER_FAILED,
    STATUS_SERVICE_FAILED,
    RunResult,
    run_analysis,
)
from app.conversations import (
    DEFAULT_HISTORY_LIMIT,
    MAX_HISTORY_LIMIT,
    Busy,
    Claimed,
    Conflicted,
    Missing,
    Replay,
    TURN_PROCESSING,
)
from app.schemas import ChatRequest, ChatTurnOut, ConversationCreatedOut, ConversationHistoryOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])

# How many analyses may run at once **in this process**. A semaphore rather than a queue:
# an unbounded queue turns a slow provider into an ever-growing backlog of work nobody asked
# to wait for, and a caller would rather be told "busy, try again" than be queued behind an
# unknown number of other analyses. Two, so a second browser tab works.
#
# Per process, deliberately and visibly: this is a module-level object, so a second uvicorn
# worker would admit two more. The README says so.
MAX_CONCURRENT_ANALYSES = 2
_ADMISSION = threading.BoundedSemaphore(MAX_CONCURRENT_ANALYSES)

# How long a client should wait before retrying a rejected or still-running request.
RETRY_AFTER_SECONDS = 15

# The run's own ceiling, used to set a turn's processing deadline. Read from the budget rather
# than repeated, so the two cannot drift apart.
_RUN_DEADLINE_SECONDS = RunBudget().deadline_seconds

# The statuses that mean the service could not do the work, as opposed to the run having
# reached a conclusion. These become 503; everything else that is not `processing` is a 200
# with a recorded outcome to read.
_INFRASTRUCTURE_FAILURES = frozenset({STATUS_PROVIDER_FAILED, STATUS_SERVICE_FAILED})

AnalysisRunner = Callable[..., RunResult]


def get_analysis_runner() -> AnalysisRunner:
    """The function that answers a question.

    A dependency rather than a direct call so that tests can drive the whole HTTP stack with a
    scripted model and no provider -- and so that a future caller can substitute a different
    runner without editing the route. The default is the real thing, unchanged.
    """
    return run_analysis


# --- creating and reading ------------------------------------------------------------------


@router.post(
    "/conversations",
    response_model=ConversationCreatedOut,
    status_code=201,
    summary="Start a conversation",
)
def post_conversation() -> ConversationCreatedOut:
    """Open a place to talk. No model is called and nothing is spent."""
    return ConversationCreatedOut(**conversations.create_conversation())


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationHistoryOut,
    summary="Read a conversation",
)
def get_conversation(
    conversation_id: str,
    limit: Annotated[
        int, Query(ge=1, le=MAX_HISTORY_LIMIT)
    ] = DEFAULT_HISTORY_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_results: Annotated[bool, Query()] = True,
) -> ConversationHistoryOut:
    """Ordered history, oldest first, paged.

    This is where the whole transcript lives. The context a *prompt* sees is bounded to a few
    turns, but a client reading its own conversation is not paying for tokens, so nothing is
    hidden from it here.
    """
    page = conversations.history(
        conversation_id, limit=limit, offset=offset, include_results=include_results
    )
    if page is None:
        raise HTTPException(
            status_code=404, detail=f"no conversation {conversation_id!r}"
        )
    return ConversationHistoryOut(**page)


# --- asking --------------------------------------------------------------------------------


@router.post(
    "/chat",
    response_model=ChatTurnOut,
    summary="Ask a question in a conversation",
    responses={
        200: {"description": "The run produced a recorded outcome."},
        202: {"description": "This request is already being processed."},
        404: {"description": "No such conversation."},
        409: {"description": "The request id conflicts, or another turn is running."},
        422: {"description": "The body was not acceptable."},
        503: {
            "description": (
                "The provider or the database failed, or this process is already running its "
                "maximum number of analyses."
            )
        },
    },
)
def post_chat(
    payload: ChatRequest,
    runner: AnalysisRunner = Depends(get_analysis_runner),
) -> Response:
    """Submit one user turn and, normally, wait for the answer.

    Synchronous on purpose for this milestone: the client asks and gets the result. A client
    that times out has not proved the analysis failed -- the run is bounded by its own budget
    and finishes and persists regardless, so the right recovery is to read the conversation
    rather than to ask again.
    """
    admission = _claim(payload)
    try:
        if admission.claimed is None:
            assert admission.response is not None  # noqa: S101 - the union is closed
            return admission.response
        return _run_turn(payload, admission.claimed, runner)
    finally:
        admission.release()


# How many progress events a slow client may fall behind by before the oldest are dropped.
# Small: these are for showing "what is happening now", and a client that is thirty events
# behind wants the newest ones.
STREAM_QUEUE_SIZE = 32


@dataclass
class Admission:
    """Admission and the turn claim, or the response to send instead of running anything.

    The permit is carried rather than released by the helper because the two routes release it
    at different moments. The JSON route releases when its response is built; the streaming
    route hands it to the worker thread, which releases it when the *analysis* finishes --
    not when the client stops reading.
    """

    claimed: Claimed | None = None
    response: Response | None = None
    held: bool = False

    def release(self) -> None:
        if self.held:
            self.held = False
            _ADMISSION.release()


def _claim(payload: ChatRequest) -> Admission:
    """Take admission and claim the turn, or produce the response to send instead.

    Shared by both chat routes. Every outcome a caller has to branch on -- a duplicate request,
    a request id reused for something else, a conversation that is busy, a conversation that
    does not exist -- is decided here once, so `/analysis/chat` and `/analysis/chat/stream`
    cannot answer differently to the same request.
    """
    if not _ADMISSION.acquire(blocking=False):
        return Admission(
            response=_busy_response(
                payload,
                detail=(
                    f"this process is already running {MAX_CONCURRENT_ANALYSES} analyses, "
                    "which is its limit. No queue is kept: retry shortly."
                ),
            )
        )

    admission = Admission(held=True)
    try:
        outcome = conversations.claim_turn(
            conversation_id=payload.conversation_id,
            request_id=payload.request_id,
            message=payload.message,
            symbol=payload.symbol,
            start_date=payload.start_date,
            end_date=payload.end_date,
            as_of=payload.as_of,
            budget_seconds=_RUN_DEADLINE_SECONDS,
        )

        match outcome:
            case Missing():
                raise HTTPException(
                    status_code=404,
                    detail=f"no conversation {payload.conversation_id!r}",
                )
            case Conflicted(detail=detail):
                raise HTTPException(status_code=409, detail=detail)
            case Busy(turn_id=turn_id, deadline=deadline):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"another turn in this conversation is still running (turn "
                        f"{turn_id}, deadline {deadline.isoformat() if deadline else 'unknown'}"
                        "). Asking a second question now would run it against history the "
                        "first is still changing."
                    ),
                )
            case Replay(turn=turn):
                admission.response = _replay_response(payload, turn)
            case Claimed() as claimed:
                admission.claimed = claimed
            case _:  # pragma: no cover - the union is closed
                raise HTTPException(status_code=500, detail="unrecognised claim outcome")
    except Exception:
        # Nothing was claimed, so nothing owns the permit.
        admission.release()
        raise

    if admission.claimed is None:
        admission.release()
    return admission


def _run_turn(payload: ChatRequest, claimed: Claimed, runner: AnalysisRunner) -> Response:
    """Run the analysis and record what it produced.

    Two things worth noticing. The question sent to the run is the user's message, and the
    *conversation context* is what carries the earlier question a bare reply is answering --
    the two are kept apart so that what the user typed is never rewritten. And a run that
    raises rather than returning is recorded as a failure and reported as such: it must not
    leave the turn stuck in `processing` for its whole deadline.
    """
    result: RunResult | None = None
    failure: str | None = None

    try:
        result = runner(
            question=payload.message,
            reference_date=claimed.reference_date,
            symbol=payload.symbol,
            start_date=payload.start_date,
            end_date=payload.end_date,
            as_of=payload.as_of,
            conversation=claimed.context,
            run_id=f"run-{claimed.turn_id}",
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed silently
        # An exception escaping `run_analysis` is a bug in this application rather than
        # something about the question, so it is logged in full and reported as a service
        # failure. The turn is finalized either way, so the conversation is not left blocked.
        logger.exception("the analysis run raised for turn %s", claimed.turn_id)
        failure = (
            f"The analysis could not be completed: {type(exc).__name__}. This is a fault in "
            "the application, not a statement about the data."
        )

    status = conversations.finalize_turn(
        conversation_id=claimed.conversation_id,
        turn_id=claimed.turn_id,
        result=result,
        failure=failure,
        # A run that raised has no result to take a status from. It is this application
        # that failed, not the provider and not the data.
        status=STATUS_SERVICE_FAILED,
    )

    if result is None:
        return JSONResponse(
            status_code=503,
            content=_turn_body(
                conversation_id=claimed.conversation_id,
                turn_id=claimed.turn_id,
                sequence=claimed.sequence,
                request_id=payload.request_id,
                status=status,
                failure=failure,
                retry_after=RETRY_AFTER_SECONDS,
            ),
        )

    return JSONResponse(
        status_code=_http_status(status),
        content=_from_run(
            result,
            conversation_id=claimed.conversation_id,
            turn_id=claimed.turn_id,
            sequence=claimed.sequence,
            request_id=payload.request_id,
        ),
        headers=_headers_for(status),
    )


def _replay_response(payload: ChatRequest, turn: dict) -> Response:
    """A request id that has been seen before: its recorded outcome, and no new work.

    Whatever that outcome was, including a failure. A retry must not silently rerun a request
    that already went wrong -- the money was spent once, and the user asked once. An
    intentional second attempt is a *new* request id, which is a decision only they can make.

    The body is the stored turn itself rather than a `RunResult` rebuilt from it. The stored
    turn already has exactly the shape the live route returns, so rebuilding it would be a
    second definition of one contract -- and one that could reject a stored turn the API itself
    wrote.
    """
    body = {
        **turn,
        "conversation_id": payload.conversation_id,
        "retry_after_seconds": (
            RETRY_AFTER_SECONDS if turn["status"] == TURN_PROCESSING else None
        ),
    }
    return JSONResponse(
        status_code=_http_status(turn["status"]),
        content=body,
        headers=_headers_for(turn["status"]),
    )


# --- streaming -------------------------------------------------------------------------------


@router.post(
    "/chat/stream",
    summary="Ask a question and watch it being answered",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": (
                "text/event-stream. Events: routing, tool, findings, composing, then done or "
                "error. Every event names something that actually happened."
            ),
            "content": {"text/event-stream": {}},
        },
        404: {"description": "No such conversation."},
        409: {"description": "The request id conflicts, or another turn is running."},
        422: {"description": "The body was not acceptable."},
        503: {"description": "The provider or database failed, or this process is at its limit."},
    },
)
def post_chat_stream(
    payload: ChatRequest,
    runner: AnalysisRunner = Depends(get_analysis_runner),
) -> Response:
    """The same turn as `/analysis/chat`, with what is happening reported as it happens.

    Everything that decides *whether* a turn may run is identical to the JSON route, because
    both call the same `_claim`. A duplicate, a conflict, a busy conversation and a missing one
    all produce the same JSON response here as there -- the stream only begins once a turn has
    genuinely been claimed, so an error never arrives as a half-open event stream.

    **The analysis does not run on this request's thread.** It runs on a worker of its own, and
    the response only reads a queue. That is what makes the disconnect rule true rather than
    aspirational: closing the browser stops the *reading*, and the run finishes, finalizes and
    persists regardless. The admission permit belongs to the worker for the same reason.
    """
    admission = _claim(payload)
    if admission.claimed is None:
        admission.release()
        assert admission.response is not None  # noqa: S101 - the union is closed
        return admission.response

    return _stream_turn(payload, admission.claimed, runner, admission)


def _stream_turn(
    payload: ChatRequest, claimed: Claimed, runner: AnalysisRunner, admission: Admission
) -> StreamingResponse:
    events: "queue.Queue[ProgressEvent]" = queue.Queue(maxsize=STREAM_QUEUE_SIZE)
    emitter = QueueProgress(events, maxsize=STREAM_QUEUE_SIZE)

    def work() -> None:
        """Run the analysis and record it, whatever the client is doing.

        Nothing here touches the response. Every exit path ends with a terminal event in the
        queue so a client that is still reading learns the outcome, and releases the permit so
        a client that has gone does not hold a slot for ever.
        """
        try:
            result: RunResult | None = None
            failure: str | None = None
            try:
                result = runner(
                    question=payload.message,
                    reference_date=claimed.reference_date,
                    symbol=payload.symbol,
                    start_date=payload.start_date,
                    end_date=payload.end_date,
                    as_of=payload.as_of,
                    conversation=claimed.context,
                    progress=emitter,
                    run_id=f"run-{claimed.turn_id}",
                )
            except Exception as exc:  # noqa: BLE001 - recorded, never swallowed silently
                logger.exception("the analysis run raised for turn %s", claimed.turn_id)
                failure = (
                    f"The analysis could not be completed: {type(exc).__name__}. This is a "
                    "fault in the application, not a statement about the data."
                )

            status = conversations.finalize_turn(
                conversation_id=claimed.conversation_id,
                turn_id=claimed.turn_id,
                result=result,
                failure=failure,
                status=STATUS_SERVICE_FAILED,
            )

            body = (
                _from_run(
                    result,
                    conversation_id=claimed.conversation_id,
                    turn_id=claimed.turn_id,
                    sequence=claimed.sequence,
                    request_id=payload.request_id,
                )
                if result is not None
                else _turn_body(
                    conversation_id=claimed.conversation_id,
                    turn_id=claimed.turn_id,
                    sequence=claimed.sequence,
                    request_id=payload.request_id,
                    status=status,
                    failure=failure,
                    retry_after=RETRY_AFTER_SECONDS,
                )
            )
            # The outcome is also the response body, not only the last event: a client whose
            # stream broke can still read what happened from the conversation.
            emitter(ProgressEvent(KIND_DONE, {"turn": body, "status": status}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("the streaming worker failed for turn %s", claimed.turn_id)
            emitter(
                ProgressEvent(
                    KIND_ERROR,
                    {"detail": f"The analysis could not be completed: {type(exc).__name__}."},
                )
            )
        finally:
            admission.release()

    threading.Thread(
        target=work, name=f"analysis-{claimed.turn_id}", daemon=True
    ).start()

    return StreamingResponse(
        _drain(events),
        media_type="text/event-stream",
        headers={
            # Nothing between here and the browser may cache or buffer this: a buffered
            # event stream is a stream that arrives all at once at the end, which is the
            # behaviour this route exists to avoid.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _drain(events: "queue.Queue[ProgressEvent]") -> Iterator[str]:
    """Yield the queued events as SSE frames until a terminal one.

    Returns as soon as an outcome arrives, so a handler thread is never left waiting on a queue
    nothing will write to again.
    """
    while True:
        event = events.get()
        yield _sse(event)
        if event.terminal:
            return


def _sse(event: ProgressEvent) -> str:
    """One server-sent event.

    The kind is on the `event:` line and repeated inside the data. The repetition is
    deliberate: a client that reads only `data` -- which is most of them, and how the frontend
    here is written -- gets a self-describing payload rather than having to parse two lines in
    step.
    """
    return f"event: {event.kind}\ndata: {json.dumps(event.as_dict())}\n\n"


# --- status mapping -------------------------------------------------------------------------


def _http_status(status: str) -> int:
    """One mapping, used for the first response and for every replay.

    That is the property worth having: the same request id always produces the same status
    code, so a client can retry without having to reason about whether it might get a
    different answer the second time.
    """
    if status == TURN_PROCESSING:
        return 202
    if status in _INFRASTRUCTURE_FAILURES:
        return 503
    return 200


def _headers_for(status: str) -> dict[str, str]:
    if status == TURN_PROCESSING or status in _INFRASTRUCTURE_FAILURES:
        return {"Retry-After": str(RETRY_AFTER_SECONDS)}
    return {}


def _busy_response(payload: ChatRequest, *, detail: str) -> JSONResponse:
    body = _turn_body(
        conversation_id=payload.conversation_id,
        turn_id="",
        sequence=None,
        request_id=payload.request_id,
        status="service_busy",
        failure=detail,
        retry_after=RETRY_AFTER_SECONDS,
    )
    return JSONResponse(
        status_code=503, content=body, headers={"Retry-After": str(RETRY_AFTER_SECONDS)}
    )


# --- response bodies ------------------------------------------------------------------------


def _from_run(
    result: RunResult,
    *,
    conversation_id: str,
    turn_id: str,
    sequence: int | None,
    request_id: str,
) -> dict[str, Any]:
    """A turn response built from the run itself.

    Built from `as_json()` rather than from the model's attributes, and that is not incidental.
    A tool execution records the arguments it ran with, which include real `date` objects; those
    are perfectly good Python and completely unserialisable, so reading the attributes directly
    produced a response body that could not be encoded. `as_json()` is the same conversion the
    CLI prints and the same one stored in the turn's row, so all three agree by construction.
    """
    payload = result.as_json()
    return {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "sequence": sequence,
        "request_id": request_id,
        **{
            key: payload[key]
            for key in (
                "status",
                "run_id",
                "destination",
                "route_reason",
                "question",
                "reference_date",
                "model",
                "answer",
                "symbol",
                "resolved",
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
        },
        "failure": None,
        "retry_after_seconds": (
            RETRY_AFTER_SECONDS if result.status in _INFRASTRUCTURE_FAILURES else None
        ),
    }


def _turn_body(
    *,
    conversation_id: str,
    turn_id: str,
    sequence: int | None,
    request_id: str,
    status: str,
    failure: str | None,
    retry_after: int | None,
) -> dict[str, Any]:
    """A turn response for a turn that has no run result: still processing, or failed."""
    return {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "sequence": sequence,
        "request_id": request_id,
        "status": status,
        "run_id": None,
        "answer": None,
        "symbol": None,
        "resolved": None,
        "citations": [],
        "evidence": [],
        "limitations": [],
        "next_steps": [],
        "findings": [],
        "tool_executions": [],
        "usage": None,
        "warnings": [],
        "failure": failure,
        "retry_after_seconds": retry_after,
    }


__all__ = [
    "MAX_CONCURRENT_ANALYSES",
    "RETRY_AFTER_SECONDS",
    "get_analysis_runner",
    "router",
]
