"""Deciding a proposal: validate, interrupt, record. The one entry point the decision route calls.

**The route owns no logic beyond choosing a status code.** Everything that decides whether an
approval may happen is here, so the HTTP surface cannot become a second implementation of the
rules -- the same split `app.rebalance_api` has from `app.proposal_run`.

**The order of the three steps is the safety property.** Validate against the broker *before* the
decision is recorded, interrupt so the validated orders are what a confirmation binds to, and only
then write the decision and the batch. A recorded approval whose orders were never checked would
be an approval of nothing in particular.

**A second click after a renewal resumes the interrupted thread** rather than validating again.
The person is confirming the orders they were shown, and re-resolving them would mean they
confirmed one set and got another -- the thing "never silently substitute trades under an existing
approval" forbids. What *is* re-checked on that path is the portfolio's shape, because that check
is cheap and a portfolio that moved underneath the plan is a refusal rather than a confirmation.

**Nothing here submits an order.** The batch this writes is what `app.order_executor` picks up.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from app import orders, portfolio_identity, proposals
from app.agent import checkpointing, execution_graph
from app.alpaca import (
    AlpacaError,
    fetch_clock,
    fetch_open_orders,
    fetch_snapshot,
)
from app.db import SessionLocal
from app.execution_plan import (
    REASON_FRESHNESS_UNKNOWN,
    REASON_PORTFOLIO_CHANGED,
    PlanRefusal,
    ResolvedPlan,
    resolve,
)
from app.rebalance_snapshot import Freshness, compare

logger = logging.getLogger(__name__)

# What a decision call produced. One value per answer the route has to distinguish, because they
# map to different status codes and different sentences.
OUTCOME_APPROVED = "approved"
OUTCOME_REJECTED = "rejected"
OUTCOME_RENEWAL_REQUIRED = "renewal_required"
OUTCOME_ALREADY_DECIDED = "already_decided"
OUTCOME_REFUSED = "refused"

REASON_PROPOSAL_NOT_FOUND = "proposal_not_found"
REASON_CHECKPOINTING_UNAVAILABLE = "checkpointing_unavailable"
REASON_BROKER_UNREACHABLE = "broker_unreachable"
REASON_NOT_CONFIRMED = "not_confirmed"


@dataclass(frozen=True)
class DecisionOutcome:
    """What a decision call did, and everything a caller needs to say so."""

    outcome: str
    detail: str
    # The recorded decision, when there is one. Carries the resolved orders it bound to.
    decision: dict | None = None
    # The batch an approval opened, which is what the Orders page is focused on afterwards.
    batch_id: str | None = None
    # The validated orders. Present on an approval and on a renewal, because both are showing the
    # person what would be sent.
    plan: list[dict] | None = None
    price_drift_percent: str | None = None
    # The machine-readable cause, for a refusal. None for the outcomes that are answers.
    reason: str | None = None


def decide(
    *,
    proposal_id: str,
    decision: str,
    idempotency_key: str,
    confirm: bool = False,
    reason: str | None = None,
    now: datetime | None = None,
) -> DecisionOutcome:
    """Approve or reject one proposal, exactly once.

    `confirm` is what a second click on a refreshed plan carries. It is not a permission: an
    approval that needed no renewal is confirmed by the click that made it, and the flag only
    decides whether a thread that is *already* waiting on a renewed plan proceeds or is shown
    again.
    """
    if decision not in (orders.DECISION_APPROVED, orders.DECISION_REJECTED):
        # Anything that is not exactly one of the two recorded words is a caller's mistake, and
        # it is refused loudly rather than read as an approval. "reject" is not "rejected", and a
        # function that treated the difference as a synonym would approve what somebody declined.
        raise ValueError(
            f"{decision!r} is not a decision. It is {orders.DECISION_APPROVED!r} or "
            f"{orders.DECISION_REJECTED!r}; mapping a request's word onto one of them is the "
            "route's job."
        )

    proposal = proposals.get(proposal_id)
    if proposal is None:
        return DecisionOutcome(
            outcome=OUTCOME_REFUSED,
            reason=REASON_PROPOSAL_NOT_FOUND,
            detail=f"No proposal {proposal_id} is stored, so there is nothing to decide.",
        )

    existing = orders.decision_for(proposal_id)
    if existing is not None:
        return _already_decided(existing, decision)

    if decision == orders.DECISION_REJECTED:
        # No validation, no broker, no graph. Declining is always available and always means the
        # same thing, whatever the market happens to be doing.
        recorded = orders.record_rejection(
            proposal_id=proposal_id,
            idempotency_key=idempotency_key,
            # The person's own words, kept only when they gave any. A rejection needs no reason
            # to be complete, and one is never invented for it.
            reason=reason,
            now=now,
        )
        return DecisionOutcome(
            outcome=OUTCOME_REJECTED,
            detail=(
                "This proposal was rejected. No orders were created and nothing was sent to "
                "Alpaca."
            ),
            decision=recorded.decision,
        )

    return _approve(
        proposal=proposal,
        proposal_id=proposal_id,
        idempotency_key=idempotency_key,
        confirm=confirm,
        now=now,
    )


def _already_decided(existing: Mapping[str, object], requested: str) -> DecisionOutcome:
    """The decision that is already recorded, whether it agrees with this request or not.

    Agreeing is not an error: a double click, a browser retry and a resumed request all land here
    and all get the same answer. Disagreeing is a conflict the caller reports as one, because a
    proposal that was rejected cannot then be approved by asking a second time.
    """
    decided = str(existing["decision"])
    stored = dict(existing)

    if decided == requested:
        detail = (
            "This proposal was already "
            f"{decided}; this request is the same decision and changed nothing."
        )
    else:
        detail = (
            f"This proposal was already {decided}, so it cannot now be {requested}. A proposal "
            "takes one decision; generate a new proposal to decide again."
        )

    batch_id = None
    if decided == orders.DECISION_APPROVED:
        found = orders.batch_for_proposal(str(existing["proposal_id"]))
        batch_id = None if found is None else found["batch_id"]

    return DecisionOutcome(
        outcome=OUTCOME_ALREADY_DECIDED,
        detail=detail,
        decision=stored,
        batch_id=batch_id,
        plan=stored.get("order_plan") or None,
        price_drift_percent=stored.get("price_drift_percent"),
        reason=None if decided == requested else "decision_conflict",
    )


def _approve(
    *,
    proposal: Mapping[str, object],
    proposal_id: str,
    idempotency_key: str,
    confirm: bool,
    now: datetime | None,
) -> DecisionOutcome:
    """Run the approval workflow: prepare, interrupt, resume, record.

    Everything that can go wrong before a decision exists -- an unreachable broker, a closed
    market, a portfolio that moved -- comes back as a refusal with nothing recorded, so the person
    can try again once whatever it was has changed.
    """
    saver = checkpointing.get_checkpointer()
    if saver is None:
        return DecisionOutcome(
            outcome=OUTCOME_REFUSED,
            reason=REASON_CHECKPOINTING_UNAVAILABLE,
            detail=(
                "The approval workflow needs the checkpoint store to hold the plan a confirmation "
                "binds to, and it is not available. Nothing was approved and nothing was sent. "
                "Check the database connection and try again."
            ),
        )

    # **The caller's clock, bound to both nodes that need one.** `prepare` validates the session
    # and the price drift against it and `record` stamps the decision and the batch with it, so an
    # approval is decided, checked and dated at one instant rather than at two. Before this the
    # parameter was accepted and then dropped: `prepare` read the wall clock regardless, which is
    # two answers to "what time is it" inside one operation -- and, because a session check is a
    # comparison against that instant, an approval that could not be reproduced at all.
    #
    # A resume builds the graph afresh, so the clock is the *confirming* request's. That is the
    # instant a person actually confirmed at, and the one the decision should carry.
    graph = execution_graph.build_execution_graph(
        prepare=lambda state: prepare(state, now=now),
        record=lambda state: record(state, now=now),
        checkpointer=saver,
    )

    try:
        pending = execution_graph.pending_interrupt(graph, proposal_id)
        if pending is not None:
            # A thread already waiting on a renewed plan. This click is either the confirmation
            # or another look at the same refreshed figures.
            if pending.get("renewal") and not confirm:
                return _renewal(pending)
            state = execution_graph.resume(graph, proposal_id, confirmed=True)
        else:
            state = execution_graph.start(
                graph,
                {
                    "proposal_id": proposal_id,
                    "idempotency_key": idempotency_key,
                    "renewal": False,
                    "confirmed": False,
                },
            )
            paused = execution_graph.interrupted(state)
            if paused is None:
                # The graph stopped at `prepare`: there is nothing to ask anybody about.
                return _refused(state.get("refusal"))
            if paused.get("renewal") and not confirm:
                return _renewal(paused)
            state = execution_graph.resume(graph, proposal_id, confirmed=True)
    except (orders.BatchConflictError, orders.DecisionTakenError) as exc:
        return DecisionOutcome(
            outcome=OUTCOME_REFUSED,
            reason="decision_conflict",
            detail=str(exc),
        )
    except Exception:  # noqa: BLE001 - recorded, never swallowed silently
        logger.exception("the approval workflow failed for %s", proposal_id)
        return DecisionOutcome(
            outcome=OUTCOME_REFUSED,
            reason="approval_failed",
            detail=(
                "The approval workflow failed unexpectedly, so nothing was approved and nothing "
                "was sent to Alpaca. This is a fault in the application, not a statement about "
                "the portfolio."
            ),
        )

    if state.get("refusal"):
        return _refused(state.get("refusal"))

    decision = orders.decision_for(proposal_id)
    if decision is None:  # pragma: no cover - `record` writes it before the graph ends
        return DecisionOutcome(
            outcome=OUTCOME_REFUSED,
            reason="approval_failed",
            detail="The approval did not produce a recorded decision. Nothing was sent.",
        )

    return DecisionOutcome(
        outcome=OUTCOME_APPROVED,
        detail=(
            "This proposal was approved for its exact trades. The orders are being submitted to "
            "the Alpaca paper account; nothing has filled yet."
        ),
        decision=decision,
        batch_id=state.get("batch_id"),
        plan=decision.get("order_plan") or None,
        price_drift_percent=decision.get("price_drift_percent"),
    )


def _renewal(pending: Mapping[str, object]) -> DecisionOutcome:
    plan = pending.get("plan") or {}
    return DecisionOutcome(
        outcome=OUTCOME_RENEWAL_REQUIRED,
        reason="renewal_required",
        detail=(
            (pending.get("refusal") or {}).get("detail")
            or "The prices this plan was validated at have moved. Approve again to confirm the "
            "refreshed estimates; nothing has been sent."
        ),
        plan=list(plan.get("orders") or []),
        price_drift_percent=plan.get("price_drift_percent"),
    )


def _refused(refusal: object) -> DecisionOutcome:
    payload = refusal if isinstance(refusal, Mapping) else {}
    return DecisionOutcome(
        outcome=OUTCOME_REFUSED,
        reason=str(payload.get("reason") or "refused"),
        detail=str(
            payload.get("detail")
            or "These orders may not be sent at the moment. Nothing was approved and nothing "
            "was sent to Alpaca."
        ),
    )


# --- the graph's two halves ---------------------------------------------------------------------


def prepare(state: Mapping[str, object], *, now: datetime | None = None) -> dict:
    """Resolve the proposal's trades into the exact orders that may be sent, or refuse.

    Every read the broker is asked for happens here, in one pass: the account and its positions,
    the clock, and the orders already working. Four requests, and no submission -- this is the
    part that decides *whether*, and `app.order_executor` is the part that does.

    `now` is the instant the session and the price drift are judged against. It is supplied by
    `_approve` from the caller's own clock, so a decision is validated at the time that was asked
    for; `None` means the wall clock, which is what the route passes in production.
    """
    moment = now or datetime.now(timezone.utc)
    proposal_id = str(state.get("proposal_id") or "")
    proposal = proposals.get(proposal_id)
    if proposal is None:
        return {
            "plan": None,
            "renewal": False,
            "refusal": {
                "reason": REASON_PROPOSAL_NOT_FOUND,
                "detail": f"No proposal {proposal_id} is stored, so its orders cannot be resolved.",
            },
        }

    try:
        with SessionLocal() as session:
            portfolio = portfolio_identity.find(session)
            if portfolio is None:
                return {
                    "plan": None,
                    "renewal": False,
                    "refusal": {
                        "reason": REASON_PROPOSAL_NOT_FOUND,
                        "detail": (
                            "No portfolio is stored, so there is no account this proposal could "
                            "be executed against."
                        ),
                    },
                }
            result = resolve(
                proposal=proposal,
                portfolio=portfolio,
                broker=fetch_snapshot(),
                clock=fetch_clock(),
                open_orders=fetch_open_orders(),
                now=moment,
            )
    except AlpacaError as exc:
        # A broker that cannot be read is a refusal, not a fault of this application: without
        # fresh positions there is nothing to validate against, and approving anyway would be
        # approving against a memory.
        logger.warning("the broker could not be read to validate an approval: %s", type(exc).__name__)
        return {
            "plan": None,
            "renewal": False,
            "refusal": {"reason": REASON_BROKER_UNREACHABLE, "detail": str(exc)},
        }

    if isinstance(result, PlanRefusal):
        return _refusal_state(result)

    return {"plan": result.payload(), "refusal": None, "renewal": False}


def _refusal_state(refusal: PlanRefusal) -> dict:
    """A refusal as graph state, with the refreshed plan when there is one to show.

    A renewal keeps its plan: the orders are sound and it is the *estimates* that moved, so the
    interrupt carries them and the next confirmation binds to them.
    """
    return {
        "plan": None if refusal.plan is None else refusal.plan.payload(),
        "renewal": refusal.is_renewal,
        "refusal": {"reason": refusal.reason, "detail": refusal.detail},
    }


def record(state: Mapping[str, object], *, now: datetime | None = None) -> dict:
    """Write the decision and open the batch, once the plan has been confirmed.

    Two things are re-checked here rather than trusted from `prepare`, and both are cheap: that
    the plan was actually confirmed, and that the portfolio is still the same portfolio. The
    second is what closes the gap between the interrupt and the confirmation -- a position that
    moved in between means the arithmetic no longer describes this account.

    `now` is what the decision and the batch are dated at, for the reader that later asks when
    this was agreed to. `None` means the wall clock, as in `prepare`.
    """
    if not state.get("confirmed"):
        return {
            "refusal": {
                "reason": REASON_NOT_CONFIRMED,
                "detail": (
                    "The plan was not confirmed, so no decision was recorded and nothing was "
                    "sent."
                ),
            }
        }

    proposal_id = str(state.get("proposal_id") or "")
    payload = state.get("plan")
    if not isinstance(payload, Mapping):  # pragma: no cover - `prepare` always sets one
        return {
            "refusal": {
                "reason": REASON_FRESHNESS_UNKNOWN,
                "detail": "The validated plan was lost, so nothing was approved.",
            }
        }

    proposal = proposals.get(proposal_id)
    if proposal is None:  # pragma: no cover - it existed when `prepare` read it
        return {
            "refusal": {
                "reason": REASON_PROPOSAL_NOT_FOUND,
                "detail": "The proposal disappeared before the approval was recorded.",
            }
        }

    with SessionLocal() as session:
        portfolio = portfolio_identity.find(session)
        if portfolio is None:  # pragma: no cover
            return {
                "refusal": {
                    "reason": REASON_FRESHNESS_UNKNOWN,
                    "detail": "No portfolio is stored, so the approval cannot be bound to one.",
                }
            }
        stored_snapshot = proposal.get("snapshot") or {}
        comparison = compare(stored_snapshot, portfolio)
        account_id = portfolio.broker_account_id

    if comparison.state is Freshness.PORTFOLIO_CHANGED:
        return {
            "refusal": {
                "reason": REASON_PORTFOLIO_CHANGED,
                "detail": (
                    f"{comparison.reason} That happened between validating this plan and "
                    "confirming it, so nothing was approved and nothing was sent. Regenerate the "
                    "proposal."
                ),
            }
        }

    if account_id is None:  # pragma: no cover - `prepare` refuses an unlinked portfolio
        return {
            "refusal": {
                "reason": REASON_FRESHNESS_UNKNOWN,
                "detail": "This portfolio is not linked to an Alpaca account.",
            }
        }

    plan = ResolvedPlan.from_payload(payload)
    recorded = orders.approve(
        proposal_id=proposal_id,
        idempotency_key=str(state.get("idempotency_key") or ""),
        plan=plan,
        broker_account_id=account_id,
        now=now,
    )

    return {
        "decision_id": recorded.decision["decision_id"],
        "batch_id": recorded.batch_id,
        "refusal": None,
    }


__all__ = [
    "DecisionOutcome",
    "OUTCOME_ALREADY_DECIDED",
    "OUTCOME_APPROVED",
    "OUTCOME_REFUSED",
    "OUTCOME_REJECTED",
    "OUTCOME_RENEWAL_REQUIRED",
    "REASON_CHECKPOINTING_UNAVAILABLE",
    "REASON_PROPOSAL_NOT_FOUND",
    "decide",
    "prepare",
    "record",
]
