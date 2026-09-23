"""One proposal generation, start to finish -- and one resumed.

This is Module 2's counterpart to `app.agent.run`: the whole application-facing surface of a
proposal run, with the HTTP layer kept out of it. `app.rebalance_api` claims a slot and calls
`generate`; `app.agent.recovery` calls `resume`. Both take the same road, which is the point --
a resumed run must not be a second implementation of a proposal run that happens to share a
checkpointer.

**The dispatch goes through the Supervisor.** A proposal is not a question, so nothing classifies
it: `generate` states the intent outright (`Destination.REBALANCE_PROPOSAL`) and the Supervisor's
routing step applies it in code rather than asking a model what a button click meant. What the
Supervisor contributes is the same entry everything else in this application goes through, so
there is one place where "what kind of work is this" is answered.

**A stage that completed is not repeated; a model call is not exactly-once.** The checkpoint is
written after a node returns, so a stage that finished is not run again on resume. A stage that
died *during* its work had no checkpoint and does run again -- which for the propose stage means
a second model call. That is a real cost of resuming, it is why resuming is bounded by the same
budget as any other run, and it is stated rather than papered over.
"""

import logging
from datetime import date

from app import alpaca, broker_sync, portfolio_identity, proposals
from app.agent import checkpointing, module2
from app.agent.budget import BudgetExhausted, RunBudget
from app.agent.context import ExplicitArguments, RequestInputs
from app.agent.evidence import EvidenceMap
from app.agent.llm import (
    ProviderAuthError,
    ProviderResponseError,
    ProviderUnavailableError,
    build_client,
)
from app.agent.module2 import (
    STATUS_UNAVAILABLE,
    NewsEvidence,
    ProposalOutcome,
    policy as proposal_policy,
    retrieve_news,
    run_proposal,
)
from app.agent.progress import KIND_STAGE, NULL_PROGRESS, Progress, ProgressEvent
from app.agent.supervisor import Destination, run_supervisor
from app.config import settings
from app.db import SessionLocal
from app.embeddings import get_embedder
from app.news_api import news_store
from app.rebalance_snapshot import Snapshot, capture
from app.valuation import MissingPriceError

logger = logging.getLogger(__name__)

# Why no proposal was produced, at the layer that knows about the portfolio and the providers
# rather than about the arithmetic. The workflow's own reasons come back through the outcome.
REASON_SYNC_FAILED = "sync_failed"
REASON_PORTFOLIO_UNAVAILABLE = "portfolio_unavailable"
REASON_PROVIDER_FAILED = "provider_failed"
REASON_CONFIGURATION_ERROR = "configuration_error"
REASON_SERVICE_FAILED = "service_failed"
REASON_NOT_RESUMABLE = "not_resumable"
REASON_CHECKPOINTING_UNAVAILABLE = "checkpointing_unavailable"

# The first stage, and the only one this layer names itself: the workflow's own stages are
# declared where the work is done, in `app.agent.module2`.
STAGE_SYNCING = "syncing"

# What the stated intent says the question is. It is never shown to a person and never reaches a
# prompt: the routing step is skipped entirely when the intent is stated.
STATED_QUESTION = "Propose a rebalance for this portfolio."


def proposal_budget() -> RunBudget:
    """The budget for one proposal run.

    Built fresh per run, because the counters are mutable and shared ceilings would let one run's
    spending count against the next. Three model requests for a flow that makes one -- a retry
    and a margin -- and no tool calls at all, which is the real difference from Module 1: this
    workflow has no tools for a model to choose between.

    A *resumed* run gets a fresh budget too. The original run's counters lived in the process
    that died, and there is nowhere to have kept them; a resumed run is therefore bounded like
    any other, which means a run that is resumed repeatedly can spend more than one budget's
    worth in total. That is a limitation of resuming, and it is why resuming happens once per
    abandoned run rather than in a loop.
    """
    return RunBudget(
        max_model_requests=3,
        max_tool_calls=0,
        max_output_tokens=1400,
        max_findings_tokens=1400,
        deadline_seconds=120.0,
    )


_DEADLINE_SECONDS = proposal_budget().deadline_seconds
# How long a recovered run is given before another process may consider it abandoned again.
LEASE_SECONDS = _DEADLINE_SECONDS + 30.0


# --- generating ----------------------------------------------------------------------------


def generate(
    *,
    proposal_id: str,
    thread_id: str,
    progress: Progress = NULL_PROGRESS,
) -> None:
    """Run the workflow once and record what it produced.

    Never raises for a condition of the data, the broker or the model: every one of those becomes
    a recorded proposal with a status of `unavailable` and a sentence saying why. That is what
    keeps the run's slot from being held by a row nothing will ever finish -- the failure path
    writes its row exactly as the success path does.
    """
    budget = proposal_budget()

    try:
        progress(
            ProgressEvent(
                KIND_STAGE,
                {"stage": STAGE_SYNCING, "detail": "Reading the broker account"},
            )
        )
        snapshot = synchronised_snapshot()
    except SyncFailed as failed:
        finalize(
            proposal_id,
            outcome=ProposalOutcome(status=STATUS_UNAVAILABLE),
            status=STATUS_UNAVAILABLE,
            failure=failed.detail,
            failure_reason=failed.reason,
            budget=budget,
        )
        return

    try:
        client = build_client(settings, budget)
    except ProviderAuthError as exc:
        finalize(
            proposal_id,
            outcome=ProposalOutcome(status=STATUS_UNAVAILABLE),
            status=STATUS_UNAVAILABLE,
            failure=str(exc),
            failure_reason=REASON_CONFIGURATION_ERROR,
            snapshot=snapshot,
            budget=budget,
        )
        return

    outcome, failure, reason = dispatch(
        client=client,
        snapshot=snapshot,
        budget=budget,
        thread_id=thread_id,
        progress=progress,
        resume=False,
    )
    if outcome is None:
        # The run never reached Module 2, so there is no outcome to record -- but the row is
        # still written, with the same sentence a proposal-level failure would carry.
        finalize(
            proposal_id,
            outcome=ProposalOutcome(status=STATUS_UNAVAILABLE),
            status=STATUS_UNAVAILABLE,
            failure=failure,
            failure_reason=reason,
            snapshot=snapshot,
            budget=budget,
        )
        return

    finalize(
        proposal_id,
        outcome=outcome,
        status=outcome.status,
        snapshot=snapshot,
        budget=budget,
    )


def dispatch(
    *,
    client,
    snapshot: Snapshot,
    budget: RunBudget,
    thread_id: str,
    progress: Progress = NULL_PROGRESS,
    resume: bool = False,
) -> tuple[ProposalOutcome | None, str | None, str | None]:
    """Hand a stated intent to the Supervisor, and Module 2's work to Module 2.

    Returns the outcome, or -- when the run could not get that far -- a sentence and a reason
    code for why. The Supervisor is not told the portfolio, the snapshot or the provider: it
    dispatches, and `run_module2_call` closes over the run's own state, which is the same
    boundary `run_module1_call` draws on the other branch.
    """

    def run_module2() -> ProposalOutcome:
        return run_proposal(
            client=client,
            snapshot=snapshot,
            budget=budget,
            retrieve=retrieve_evidence,
            progress=progress,
            # None when checkpointing is unavailable, and the graph then behaves as it did
            # before it existed. A run without a checkpoint cannot be resumed, which
            # `app.agent.recovery` reports rather than hiding.
            checkpointer=checkpointing.get_checkpointer(),
            thread_id=thread_id,
            resume=resume,
        )

    try:
        supervised = run_supervisor(
            client=client,
            inputs=RequestInputs(
                question=STATED_QUESTION,
                reference_date=date.today(),
                explicit=ExplicitArguments(),
                explicit_intent=Destination.REBALANCE_PROPOSAL,
            ),
            budget=budget,
            evidence=EvidenceMap(),
            run_module1_call=_refuse_module1,
            run_module2_call=run_module2,
            progress=progress,
        )
    except (ProviderUnavailableError, ProviderResponseError) as exc:
        logger.warning("the proposal run could not use the model: %s", type(exc).__name__)
        return (
            None,
            f"The proposal model could not be used: {exc}. No proposal was produced, and "
            "nothing was sent to a broker.",
            REASON_PROVIDER_FAILED,
        )
    except BudgetExhausted as exc:
        return None, exc.detail, "budget_exhausted"

    return supervised.module2, None, None


def _refuse_module1(context=None) -> None:  # pragma: no cover - unreachable by construction
    """A guard, not a path.

    `run_module1_call` is required by the Supervisor's signature, and a stated intent never
    routes to Module 1 -- `after_route` sends `rebalance_proposal` to the rebalance node before
    the analysis branch is considered. If that ever stopped being true, the run should fail
    loudly here rather than quietly answer a portfolio question with a company analysis.
    """
    raise AssertionError(
        "a stated rebalance intent reached the Module 1 analysis branch, which it cannot do"
    )


class SyncFailed(Exception):
    """The portfolio could not be synchronised, so there is nothing to propose against."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


def synchronised_snapshot() -> Snapshot:
    """Sync the portfolio, then freeze it.

    Synchronous on purpose. The stored snapshot may be an hour old, and a proposal computed from
    it would carry a "portfolio snapshot" timestamp that makes it look current. If the sync
    fails, the run stops with the broker's own reason -- silently proposing against older
    holdings is the one outcome worse than not proposing.
    """
    try:
        with SessionLocal() as session:
            broker_sync.sync(session)
    except broker_sync.SyncInProgressError as exc:
        raise SyncFailed(REASON_SYNC_FAILED, str(exc)) from exc
    except (broker_sync.SyncError, alpaca.AlpacaError) as exc:
        logger.warning("the portfolio could not be synchronised: %s", type(exc).__name__)
        raise SyncFailed(
            REASON_SYNC_FAILED,
            f"The portfolio could not be synchronised with the broker, so no proposal was "
            f"produced from the stored holdings: {exc}",
        ) from exc

    with SessionLocal() as session:
        portfolio = portfolio_identity.find(session)
        if portfolio is None:
            raise SyncFailed(
                REASON_PORTFOLIO_UNAVAILABLE,
                f"No portfolio named {portfolio_identity.DEMO_PORTFOLIO_NAME!r} is stored. "
                "Run: docker compose exec backend python -m app.seed",
            )
        try:
            return capture(portfolio)
        except MissingPriceError as exc:
            # The valuation's refusal to price a portfolio it cannot price in full, surfaced
            # here rather than as a partial proposal. Trades computed from a snapshot that
            # quietly omitted a holding would be trades for a portfolio nobody owns.
            raise SyncFailed(
                REASON_PORTFOLIO_UNAVAILABLE,
                "The portfolio cannot be valued, so no trades can be priced against it: "
                + ", ".join(exc.symbols)
                + " has no price under the basis this portfolio is valued at. Re-run the sync, "
                "or check the stored prices.",
            ) from exc


def retrieve_evidence(symbols) -> NewsEvidence:
    """Retrieve evidence over its own short-lived session and index connection.

    Opened here rather than held for the run: the retrieval finishes before the model is called,
    and a Qdrant client held open across a model request would be a connection pinned for a
    minute to do nothing.
    """
    store = news_store()
    try:
        with SessionLocal() as session:
            return retrieve_news(session, symbols, store=store, embedder=get_embedder())
    finally:
        store.close()


def finalize(
    proposal_id: str,
    *,
    outcome: ProposalOutcome,
    status: str,
    snapshot: Snapshot | None = None,
    failure: str | None = None,
    failure_reason: str | None = None,
    budget: RunBudget | None = None,
) -> None:
    """Write the row, whichever way the run went.

    One place, so a run that produced nothing and a run that produced a proposal cannot be
    recorded differently -- in particular, `evidence`, `assumptions` and `limitations` are always
    written, because those are what make a proposal readable even when it says `unavailable`.
    """
    proposals.finalize(
        proposal_id=proposal_id,
        status=status,
        snapshot=snapshot.as_dict() if snapshot is not None else None,
        snapshot_fingerprint=snapshot.fingerprint if snapshot is not None else None,
        policy=proposal_policy(),
        targets=outcome.targets,
        rationale=outcome.rationale,
        evidence=outcome.evidence,
        calculation=outcome.calculation,
        assumptions=outcome.assumptions,
        limitations=outcome.limitations,
        usage=budget.as_dict() if budget is not None else None,
        failure=failure or outcome.failure,
        failure_reason=failure_reason or outcome.failure_reason,
    )


# --- resuming ------------------------------------------------------------------------------


def resume(*, proposal_id: str, progress: Progress = NULL_PROGRESS) -> str:
    """Continue an interrupted run, and return the status it ended at.

    The decisions, in order, and each of them is a refusal to guess:

    * **Not an abandoned run?** Nothing to do. A row that finished while this was being decided,
      or one that is genuinely still running somewhere, is left alone.
    * **No snapshot on the row?** The run died before it froze one, so there is nothing to
      resume *against* -- and re-synchronising here would compute trades for a portfolio the
      original run never saw. Marked interrupted, with that as the reason.
    * **No checkpoint?** Either checkpointing was unavailable when the run started, or the run
      died before its first stage completed. Both mean the work cannot be continued, and both
      are reported as such rather than as a fresh run.
    * **Otherwise, continue.** `run_proposal(resume=True)` invokes the graph with no input, so it
      picks up at the stage after the last one that completed. A graph that had already finished
      before the process died runs *nothing* -- its state is read and recorded, which is what
      makes finalization after a crash idempotent rather than a second proposal.

    The lease is extended before any of that, atomically, so two processes that both noticed the
    same abandoned run cannot both resume it.
    """
    stored = proposals.get(proposal_id)
    if stored is None or stored["status"] != proposals.STATUS_GENERATING:
        return "not_applicable"

    if not proposals.renew(proposal_id, seconds=LEASE_SECONDS):
        # Somebody else has it, or it is still inside its lease. Either way, not ours.
        return "leased"

    payload = stored.get("snapshot") or {}
    thread_id = stored.get("thread_id")
    if not payload or not thread_id:
        return _abandon(
            proposal_id,
            "This proposal run was interrupted before it recorded the portfolio it was working "
            "from, so there is nothing to continue from. It was not resumed, and no proposal "
            "was produced.",
            REASON_NOT_RESUMABLE,
        )

    checkpointer = checkpointing.get_checkpointer()
    if checkpointer is None:
        return _abandon(
            proposal_id,
            "This proposal run was interrupted, and checkpointing is not available in this "
            "process -- so the run's stages were never recorded and the work cannot be "
            "continued. Nothing was resumed and no proposal was produced.",
            REASON_CHECKPOINTING_UNAVAILABLE,
        )

    snapshot = Snapshot.from_dict(payload)
    budget = proposal_budget()
    try:
        client = build_client(settings, budget)
    except ProviderAuthError as exc:
        return _abandon(proposal_id, str(exc), REASON_CONFIGURATION_ERROR)

    # Built once and handed to `run_proposal`, so the state read here and the state continued
    # there are the same thread rather than two graphs over it.
    graph = module2.build_module2_graph(
        client=client,
        snapshot=snapshot,
        budget=budget,
        retrieve=retrieve_evidence,
        progress=progress,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": thread_id}}
    state = graph.get_state(config)

    if not state.values:
        return _abandon(
            proposal_id,
            "This proposal run was interrupted before any of its stages completed, so there is "
            "no recorded progress to continue from. It was not resumed, and no proposal was "
            "produced.",
            REASON_NOT_RESUMABLE,
        )

    logger.info(
        "resuming proposal %s from its checkpoint (next: %s)",
        proposal_id,
        state.next or "nothing -- the graph had already finished",
    )
    progress(
        ProgressEvent(
            KIND_STAGE,
            {
                "stage": "resuming",
                "detail": "Continuing an interrupted run from its last completed stage",
            },
        )
    )

    outcome, failure, reason = dispatch(
        client=client,
        snapshot=snapshot,
        budget=budget,
        thread_id=thread_id,
        progress=progress,
        resume=True,
    )
    if outcome is None:
        return _abandon(proposal_id, failure, reason)

    # Said on the record, because a resumed proposal is a different article from one that ran
    # straight through: it finished against a snapshot taken before the interruption, and the
    # portfolio may have moved since. The freshness comparison says what moved; this says why a
    # proposal whose snapshot is older than its completion time exists at all.
    outcome.limitations = [
        *outcome.limitations,
        "This proposal was resumed after its run was interrupted, and completed against the "
        f"portfolio snapshot recorded at {snapshot.read_at.isoformat()}. It is the proposal the "
        "interrupted run was producing, not a new one.",
    ]
    if failure is None:
        finalize(
            proposal_id,
            outcome=outcome,
            status=outcome.status,
            snapshot=snapshot,
            budget=budget,
        )
    return outcome.status


def _abandon(proposal_id: str, detail: str | None, reason: str | None) -> str:
    """Close a run that cannot be continued, saying why rather than pretending it can."""
    proposals.finalize(
        proposal_id=proposal_id,
        status=proposals.STATUS_INTERRUPTED,
        failure=detail or "This proposal run could not be continued.",
        failure_reason=reason or REASON_NOT_RESUMABLE,
    )
    return proposals.STATUS_INTERRUPTED


__all__ = [
    "LEASE_SECONDS",
    "REASON_CHECKPOINTING_UNAVAILABLE",
    "REASON_CONFIGURATION_ERROR",
    "REASON_NOT_RESUMABLE",
    "REASON_PORTFOLIO_UNAVAILABLE",
    "REASON_PROVIDER_FAILED",
    "REASON_SERVICE_FAILED",
    "REASON_SYNC_FAILED",
    "STAGE_SYNCING",
    "STATED_QUESTION",
    "SyncFailed",
    "dispatch",
    "expired_runs",
    "finalize",
    "generate",
    "proposal_budget",
    "resume",
    "retrieve_evidence",
    "synchronised_snapshot",
]
