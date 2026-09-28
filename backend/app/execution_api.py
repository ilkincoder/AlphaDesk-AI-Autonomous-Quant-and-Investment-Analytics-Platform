"""The decision and Orders routes.

Two routes and one rule about what they claim. `POST /rebalance/proposal/{id}/decision` records
what a person decided and, on an approval, opens the execution batch; `GET /orders` returns every
batch with its orders, and `GET /orders/{id}` returns one.

**Nothing here submits an order, and there is no route that could.** The decision route validates
against the broker and records; `app.order_executor` is what sends, from its own thread, whether
or not this API is being called. A request that waited on a fill would be a request held open for
as long as the market took.

**Handlers are `def`, not `async def`,** for the same reason the proposal routes are: the work
blocks on httpx and psycopg2, and FastAPI runs a plain `def` path operation in its threadpool.

**Ownership is the portfolio, and it is checked on every read.** This application has one
portfolio and no per-user accounts, so "belongs to the caller" means "belongs to the portfolio
this application is linked to" -- a batch addressed by an id that belongs to another account is
answered as not found rather than shown. The browser is never trusted to say which account it is
looking at, and no route takes an account, a portfolio or a symbol from the client.
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app import orders, portfolio_identity
from app.db import SessionLocal
from app.order_execution import (
    OUTCOME_ALREADY_DECIDED,
    OUTCOME_REFUSED,
    REASON_BROKER_UNREACHABLE,
    REASON_CHECKPOINTING_UNAVAILABLE,
    REASON_PROPOSAL_NOT_FOUND,
    decide,
)
from app.schemas import (
    DecisionOutcomeOut,
    ExecutionBatchOut,
    OrderIntentOut,
    OrdersView,
    ProposalDecisionOut,
    ProposalDecisionRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["orders"])

# Why a decision was refused, as a status code. Two of these are about the world and one is about
# this process: an unreachable broker and an unavailable checkpointer are both "not now, and here
# is what would fix it", which is what 503 says.
_REFUSAL_STATUS = {
    REASON_BROKER_UNREACHABLE: 503,
    REASON_CHECKPOINTING_UNAVAILABLE: 503,
    REASON_PROPOSAL_NOT_FOUND: 404,
    "decision_conflict": 409,
}


@router.post(
    "/rebalance/proposal/{proposal_id}/decision",
    response_model=DecisionOutcomeOut,
    summary="Approve or reject a proposal",
    responses={
        200: {"description": "The decision that was recorded, or that already existed."},
        404: {"description": "No such proposal is stored."},
        409: {"description": "This proposal already has a different decision."},
        503: {"description": "The broker or the checkpoint store is not available."},
    },
)
def post_decision(
    proposal_id: str,
    payload: ProposalDecisionRequest,
) -> JSONResponse:
    """Record a decision about one proposal.

    Everything that decides whether the decision may happen lives in `app.order_execution`, so
    this handler stays what it looks like: a body in, a status code and a sentence out. The body
    carries no order data and the proposal is addressed by id, so there is nothing here a client
    could send that would change what gets executed.
    """
    decision = "approved" if payload.decision == "approve" else "rejected"

    outcome = decide(
        proposal_id=proposal_id,
        decision=decision,
        idempotency_key=payload.idempotency_key,
        confirm=payload.confirm,
        reason=payload.reason,
    )

    body = _outcome_body(outcome)

    if outcome.outcome == OUTCOME_REFUSED:
        status = _REFUSAL_STATUS.get(str(outcome.reason), 400)
        return JSONResponse(status_code=status, content=body)

    if outcome.outcome == OUTCOME_ALREADY_DECIDED and outcome.reason == "decision_conflict":
        return JSONResponse(status_code=409, content=body)

    if outcome.outcome == OUTCOME_ALREADY_DECIDED:
        # A double click or a retry, answered from the decision that already exists. 200, because
        # nothing went wrong: the request asked for what is already true.
        return JSONResponse(status_code=200, content=body)

    return JSONResponse(status_code=200, content=body)


def _outcome_body(outcome) -> dict:
    return DecisionOutcomeOut(
        outcome=outcome.outcome,
        detail=outcome.detail,
        reason=outcome.reason,
        decision=None if outcome.decision is None else ProposalDecisionOut.model_validate(outcome.decision),
        batch_id=outcome.batch_id,
        plan=outcome.plan or [],
        price_drift_percent=outcome.price_drift_percent,
    ).model_dump(mode="json")


@router.get("/orders", response_model=OrdersView, summary="Read the execution batches")
def get_orders() -> OrdersView:
    """Every batch this application has executed, newest first, and which one is running.

    A read. The statuses are updated by the execution loop rather than by this endpoint being
    called, so a page that polls sees the batch move without the browser driving anything.
    """
    _owned_portfolio()

    stored = orders.batches()
    active = orders.active_batch()

    return OrdersView(
        active_batch_id=None if active is None else active["batch_id"],
        batches=[_batch_out(item) for item in stored],
    )


@router.get(
    "/orders/{batch_id}",
    response_model=ExecutionBatchOut,
    summary="Read one execution batch",
    responses={404: {"description": "No such batch belongs to this portfolio."}},
)
def get_batch(batch_id: str) -> ExecutionBatchOut:
    """One batch, with its orders.

    404 rather than 403 for a batch that belongs to another account: this application has one
    portfolio, and an id that is not its own is not a thing it can confirm exists.
    """
    portfolio = _owned_portfolio()

    stored = orders.batch(batch_id)
    if stored is None or stored["portfolio_id"] != portfolio.id:
        raise HTTPException(status_code=404, detail=f"No execution batch {batch_id} is stored.")

    return _batch_out(stored)


def _owned_portfolio():
    """The portfolio everything here belongs to, or a 404 naming the command that creates it.

    The same lookup the portfolio routes make, and the same answer. It is how "this batch is
    yours" is decided: there is one portfolio, so an id that belongs to a different one -- a row
    left by another installation sharing the database, say -- is not served here.
    """
    with SessionLocal() as session:
        portfolio = portfolio_identity.find(session)
        if portfolio is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Portfolio {portfolio_identity.DEMO_PORTFOLIO_NAME!r} has not been seeded, "
                    "so there are no orders to show. Run: docker compose exec backend python -m "
                    "app.seed"
                ),
            )
        return portfolio


def _batch_out(stored: dict) -> ExecutionBatchOut:
    return ExecutionBatchOut(
        batch_id=stored["batch_id"],
        proposal_id=stored["proposal_id"],
        status=stored["status"],
        created_at=stored["created_at"],
        started_at=stored["started_at"],
        completed_at=stored["completed_at"],
        currency=stored["currency"],
        cash_at_approval=stored["cash_at_approval"],
        reserved_cash=stored["reserved_cash"],
        price_drift_percent=stored["price_drift_percent"],
        notes=stored["notes"],
        halt_reason=stored["halt_reason"],
        sync_status=stored["sync_status"],
        sync_error=stored["sync_error"],
        sync_at=stored["sync_at"],
        orders=[OrderIntentOut.model_validate(item) for item in stored["orders"]],
    )


def proposal_decision(proposal_id: str) -> tuple[dict | None, str | None]:
    """A proposal's decision and the batch it opened, for the proposal API to attach.

    Imported by `app.rebalance_api` rather than written there, so the two endpoints cannot
    disagree about what a proposal's decision is.
    """
    decision = orders.decision_for(proposal_id)
    if decision is None:
        return None, None
    batch = orders.batch_for_proposal(proposal_id)
    return decision, None if batch is None else batch["batch_id"]


__all__ = [
    "proposal_decision",
    "router",
]
