"""One analysis run, start to finish.

`run_analysis` is the whole application-facing surface: give it a question, a reference date
and whatever the caller stated outright, and it returns a typed result. The CLI is a thin
wrapper over it, and a future `/analysis/chat` endpoint can call the same function without a
subprocess.

**One run is one run.** A fresh budget, a fresh evidence map and a fresh message history are
created here and thrown away at the end. Nothing is persisted and nothing is shared, so a
second question cannot inherit the first one's citations, its spending, or its conversation.
That is also why there is no checkpointer: persistence is a decision for the chat integration,
where there is a conversation worth persisting.

**The status says what kind of outcome this is, not how the model felt about it.** Tool
unavailability and partial analysis are not statuses -- they live in the evidence and the tool
log, exactly as they do in Step 7A. What is a status is the things a caller has to branch on:
did it answer, does it need something from the user, was the request out of scope, did it run
out of budget, did the provider fail, or was it never configured.

**A failed run still returns what it gathered.** If the provider dies half way through, or the
answer cannot be composed, the evidence and the tool log come back with it. Throwing those
away would leave a caller unable to tell a network problem from a question about a company
this system holds no data for.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from app.agent.budget import BudgetExhausted, RunBudget
from app.analysis import information_cutoff
from app.agent.context import (
    ConversationContext,
    ExplicitArguments,
    KnownSymbols,
    RequestInputs,
    RunContext,
    known_symbols as read_known_symbols,
)
from app.agent.evidence import EvidenceMap
from app.agent.llm import (
    ModelClient,
    ProviderAuthError,
    ProviderResponseError,
    ProviderUnavailableError,
    build_client,
)
from app.agent.progress import Progress, safe
from app.agent.module1 import (
    REJECTION_NO_MARKET_WINDOW,
    Module1Outcome,
    run_module1,
)
from app.agent.supervisor import (
    Destination,
    SupervisorOutcome,
    SupervisorRoute,
    collect_limitations,
    company_not_stored_answer,
    looks_like_a_ticker,
    run_supervisor,
)
from app.db import SessionLocal

logger = logging.getLogger(__name__)

# Run statuses. Each is something a caller does something different about.
STATUS_COMPLETED = "completed"
STATUS_CLARIFICATION_NEEDED = "clarification_needed"
STATUS_UNSUPPORTED_CAPABILITY = "unsupported_capability"
# A definite ticker this system holds nothing for. Distinct from `unsupported_capability`
# because the request was not out of scope -- the data simply is not here.
STATUS_COMPANY_NOT_STORED = "company_not_stored"
STATUS_BUDGET_EXHAUSTED = "budget_exhausted"
STATUS_PROVIDER_FAILED = "provider_failed"
# This application's own infrastructure failed: the database could not be read. Distinct from
# `provider_failed`, which is DeepSeek, and from `configuration_error`, which is a missing key.
# An outage must never be reported as a fact about the data.
STATUS_SERVICE_FAILED = "service_failed"
STATUS_CONFIGURATION_ERROR = "configuration_error"
STATUS_INVALID_CITATIONS = "invalid_citations"

# The statuses that mean the run did not produce a usable answer.
FAILED_STATUSES = frozenset(
    {
        STATUS_BUDGET_EXHAUSTED,
        STATUS_PROVIDER_FAILED,
        STATUS_SERVICE_FAILED,
        STATUS_CONFIGURATION_ERROR,
    }
)


class RunResult(BaseModel):
    """Everything one run produced, in a shape a caller can act on.

    `answer` is None whenever there is no answer to show, and `status` says which kind of
    nothing that is. Callers branch on `status`; readers read `answer`, `citations` and
    `limitations`.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    question: str
    reference_date: date
    model: str | None = None

    resolved: dict[str, Any] | None = None
    # The instant the run's own information date stops being knowable -- derived from
    # `resolved.as_of` with the same function the tools use, so the run's cutoff and theirs
    # cannot drift apart. None when nothing was settled, which is the honest answer for a
    # clarification or a refusal: there was no information date.
    #
    # This is the *overall* cutoff. A tool may report a narrower one inside its own payload --
    # `market_insider_analysis` derives its cutoff from the market window's end -- and that one
    # is that tool's, not this run's. See `app.agent.supervisor.DATE_RULES`.
    information_cutoff: datetime | None = None
    destination: str | None = None
    route_reason: str | None = None
    # The company this run was about, when one was identified. Set even for a run that settled
    # nothing, so a refusal can name what it refused.
    symbol: str | None = None

    answer: str | None = None
    # Read out of the run's evidence map, never out of the answer's prose.
    citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)

    tool_executions: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass
class RunDependencies:
    """What a run needs from the outside, injectable so tests can supply doubles.

    `client` is required. Defaulting it to a real client would mean a test that forgot to
    pass one quietly made a network call and spent money.
    """

    client: ModelClient
    budget: RunBudget
    known_symbols: KnownSymbols | None = None


def run_analysis(
    *,
    question: str,
    reference_date: date | None = None,
    symbol: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    as_of: date | None = None,
    client: ModelClient | None = None,
    budget: RunBudget | None = None,
    known_symbols: KnownSymbols | None = None,
    conversation: ConversationContext | None = None,
    progress: Progress | None = None,
    run_id: str | None = None,
) -> RunResult:
    """Answer one question about one company.

    Raises nothing for a data or provider condition: everything becomes a `RunResult` with a
    status. An exception escaping here would be a bug in this application rather than
    something about the question, and the CLI reports the two differently on purpose.

    `conversation` is the bounded context an earlier turn left behind, loaded by the caller
    from its own records -- never from a request body. Omitted, as the CLI omits it, the run
    behaves exactly as it did before there were conversations: one question, no history.

    `progress` receives events while the run works, for a client that is watching. It defaults
    to nothing at all, so the CLI and every existing caller are unaffected; a caller that
    passes one that raises cannot break the run, because the emitter is wrapped.
    """
    emit = safe(progress)
    identifier = run_id or f"run-{uuid.uuid4().hex[:12]}"
    run_budget = budget or RunBudget()
    explicit = ExplicitArguments(
        symbol=symbol, start_date=start_date, end_date=end_date, as_of=as_of
    )

    # A resumed clarification keeps its own reference date. A reply of "August 6 through
    # September 17" answers a question asked on some earlier day, and resolving that question
    # against *today* would silently move the window the user was originally asking about. An
    # explicitly supplied reference date still wins, because that one is the caller's to set.
    resumed_on = conversation.pending.reference_date if conversation and conversation.pending else None
    today = reference_date or resumed_on or date.today()

    base: dict[str, Any] = {
        "run_id": identifier,
        "question": question,
        "reference_date": today,
    }

    # --- the model, built only now -----------------------------------------------------
    model_name = getattr(client, "model_name", None)
    if client is None:
        try:
            client = build_client(_settings(), run_budget)
            model_name = client.model_name
        except ProviderAuthError as exc:
            return RunResult(
                **base,
                status=STATUS_CONFIGURATION_ERROR,
                warnings=[str(exc)],
                usage=run_budget.as_dict(),
            )

    # --- which companies this database knows, and in what sense -------------------------
    #
    # A database that cannot be read is a `service_failed`, never an absence. Collapsing the
    # two would turn an outage into the confident statement that this system holds no data
    # about a company -- a false claim about the data, made on the strength of not being able
    # to look at it.
    try:
        known = known_symbols if known_symbols is not None else _known_symbols()
    except SQLAlchemyError as exc:
        logger.exception("could not read the known symbol lists")
        return RunResult(
            **base,
            status=STATUS_SERVICE_FAILED,
            model=model_name,
            warnings=[
                "The stored company and holding lists could not be read, so nothing can be "
                f"said about what is held here: {type(exc).__name__}. This is a failure to "
                "read the database, not a statement that any company is missing."
            ],
            usage=run_budget.as_dict(),
        )

    # A company the caller named outright can be checked before spending anything: there is no
    # model needed to learn what the caller already said. Only the *kind* of refusal is decided
    # here -- a definite ticker this system does not hold is refused, while anything that is not
    # a definite ticker falls through to routing, which will ask which one was meant.
    if explicit.symbol and not known.recognises(explicit.symbol):
        if looks_like_a_ticker(explicit.symbol):
            return _company_not_stored(
                base=base, model_name=model_name, budget=run_budget,
                symbol=explicit.symbol.strip().upper(), known=known,
            )

    inputs = RequestInputs(
        question=question,
        reference_date=today,
        explicit=explicit,
        known=known,
        conversation=conversation,
    )

    # --- this run's own state, created here and discarded at the end --------------------
    evidence = EvidenceMap()

    def agent_call(settled: RunContext) -> Module1Outcome:
        """Run Module 1 against the context the routing step settled.

        The context arrives as an argument rather than being looked up, so the agent cannot
        run against anything but the request this run actually settled.
        """
        return run_module1(
            client=client,
            context=settled,
            budget=run_budget,
            evidence=evidence,
            progress=emit,
        )

    try:
        outcome = run_supervisor(
            client=client,
            inputs=inputs,
            budget=run_budget,
            evidence=evidence,
            run_module1_call=agent_call,
            progress=emit,
        )
    except ProviderAuthError as exc:
        return _failure(
            base, model_name, run_budget, evidence, STATUS_CONFIGURATION_ERROR, str(exc)
        )
    except (ProviderUnavailableError, ProviderResponseError) as exc:
        return _failure(
            base,
            model_name,
            run_budget,
            evidence,
            STATUS_PROVIDER_FAILED,
            f"The analysis model could not be used: {exc}. Nothing was analysed.",
        )
    except BudgetExhausted as exc:
        return _failure(
            base, model_name, run_budget, evidence, STATUS_BUDGET_EXHAUSTED, exc.detail
        )

    return _to_result(
        base=base,
        model_name=model_name,
        budget=run_budget,
        evidence=evidence,
        outcome=outcome,
    )


def _needed_a_window_it_was_not_given(module1: Module1Outcome | None) -> bool:
    """Whether this run tried to analyse a market window and had none to analyse.

    A precise signal rather than a guess from the prose: the refusal is raised by this
    application, carries a code, and can only mean one thing.
    """
    if module1 is None:
        return False
    return any(
        execution.rejection_code == REJECTION_NO_MARKET_WINDOW
        for execution in module1.executions
    )


def _company_not_stored(
    *,
    base: dict[str, Any],
    model_name: str | None,
    budget: RunBudget,
    symbol: str,
    known: KnownSymbols,
) -> RunResult:
    """The refusal for a company this system holds nothing for, with no model involved.

    Written here so the early check and the routing-time check produce the *same* result. Two
    places building the same answer slightly differently is how a client ends up with two
    shapes for one outcome.
    """
    return RunResult(
        **base,
        status=STATUS_COMPANY_NOT_STORED,
        model=model_name,
        symbol=symbol,
        destination=str(Destination.COMPANY_NOT_STORED),
        route_reason=(
            f"{symbol} is not among the companies with stored data or portfolio holdings"
        ),
        answer=company_not_stored_answer(symbol, known),
        usage=budget.as_dict(),
    )


def _to_result(
    *,
    base: dict[str, Any],
    model_name: str | None,
    budget: RunBudget,
    evidence: EvidenceMap,
    outcome: SupervisorOutcome,
) -> RunResult:
    route: SupervisorRoute | None = outcome.route
    module1 = outcome.module1
    findings = module1.findings if module1 else None
    warnings: list[str] = []

    if evidence.overflowed:
        warnings.append(
            f"{evidence.overflowed} citable item(s) were not recorded because this run's "
            "evidence map was full, so the evidence below is a partial set."
        )

    status = _status(outcome, budget, warnings)

    if budget.stopped_by:
        warnings.append(
            "This run stopped early: "
            + (budget.stop_detail or budget.stopped_by)
            + ". What was gathered before that is reported below."
        )

    if findings and not findings.evidence_refs and len(evidence):
        warnings.append(
            "The analysis agent did not name which evidence it relied on, so the citations "
            "below are this run's whole evidence set rather than a claimed subset."
        )

    return RunResult(
        **base,
        status=status,
        model=model_name,
        resolved=outcome.context.resolved.as_dict() if outcome.context else None,
        information_cutoff=(
            information_cutoff(outcome.context.resolved.as_of)
            if outcome.context
            else None
        ),
        destination=str(route.destination) if route else None,
        route_reason=route.reason if route else None,
        # Carried even when no request was settled, which is exactly the case a caller most
        # needs it in: "this company is not stored" is about a symbol that never became a
        # resolved request.
        symbol=(outcome.context.resolved.symbol if outcome.context else None)
        or (route.symbol if route else None),
        answer=outcome.answer,
        citations=evidence.citations_for(outcome.citations),
        evidence=evidence.as_list(),
        # The agent's own limitations plus every warning the tools produced. Read from the
        # execution log rather than from the agent's summary, so a coverage caveat cannot go
        # missing because the model did not mention it.
        limitations=collect_limitations(module1),
        next_steps=list(findings.next_steps) if findings else [],
        findings=list(findings.findings) if findings else [],
        tool_executions=[item.as_dict() for item in (module1.executions if module1 else [])],
        usage=budget.as_dict(),
        warnings=warnings,
    )


def _status(
    outcome: SupervisorOutcome, budget: RunBudget, warnings: list[str]
) -> str:
    """Which kind of outcome this run is.

    Ordered by what a caller most needs to know. An unroutable request is not a completed
    one, an answer withheld for invented citations is not a completed one, and a run that ran
    out of budget before writing anything is not a provider failure.
    """
    route = outcome.route

    if route is None:
        if budget.stopped_by:
            warnings.append(budget.stop_detail or "The run stopped before it could route.")
            return STATUS_BUDGET_EXHAUSTED
        warnings.append(
            outcome.routing_error
            or "The request could not be classified, so nothing was analysed."
        )
        return STATUS_PROVIDER_FAILED

    if route.destination is Destination.CLARIFICATION_NEEDED:
        return STATUS_CLARIFICATION_NEEDED
    if route.destination is Destination.UNSUPPORTED_CAPABILITY:
        return STATUS_UNSUPPORTED_CAPABILITY
    if route.destination is Destination.COMPANY_NOT_STORED:
        # An answer, not a failure: the run produced a definite statement about what this
        # system holds, and the answer explaining it was written in code.
        return STATUS_COMPANY_NOT_STORED

    if _needed_a_window_it_was_not_given(outcome.module1):
        # The question asked for a market comparison and named no period, so the agent tried
        # the market tool and was refused. The answer already asks which period to use; making
        # the *status* a clarification is what lets the reply be resumed rather than arriving
        # as a new question -- and it is decided here, from the refusal, rather than hoped for
        # from the routing prompt.
        return STATUS_CLARIFICATION_NEEDED

    if outcome.invalid_citations and outcome.answer is None:
        warnings.append(
            "The answer cited evidence that does not exist in this run "
            f"({', '.join(outcome.invalid_citations)}) and was withheld. The evidence and "
            "the tool log below are unaffected; only the prose was discarded."
        )
        return STATUS_INVALID_CITATIONS

    if outcome.answer is None:
        if budget.stopped_by:
            warnings.append(budget.stop_detail or "The run stopped before an answer.")
            return STATUS_BUDGET_EXHAUSTED
        warnings.append("The run produced no answer text.")
        return STATUS_COMPLETED

    return STATUS_COMPLETED


def _failure(
    base: dict[str, Any],
    model_name: str | None,
    budget: RunBudget,
    evidence: EvidenceMap,
    status: str,
    message: str,
) -> RunResult:
    """A run that could not finish, with whatever it gathered before it stopped."""
    return RunResult(
        **base,
        status=status,
        model=model_name,
        answer=None,
        evidence=evidence.as_list(),
        warnings=[message],
        usage=budget.as_dict(),
    )


def _settings():
    from app.config import settings

    return settings


def _known_symbols() -> KnownSymbols:
    with SessionLocal() as session:
        return read_known_symbols(session)


__all__ = [
    "FAILED_STATUSES",
    "RunDependencies",
    "RunResult",
    "STATUS_BUDGET_EXHAUSTED",
    "STATUS_CLARIFICATION_NEEDED",
    "STATUS_COMPANY_NOT_STORED",
    "STATUS_COMPLETED",
    "STATUS_CONFIGURATION_ERROR",
    "STATUS_INVALID_CITATIONS",
    "STATUS_PROVIDER_FAILED",
    "STATUS_SERVICE_FAILED",
    "STATUS_UNSUPPORTED_CAPABILITY",
    "run_analysis",
]
