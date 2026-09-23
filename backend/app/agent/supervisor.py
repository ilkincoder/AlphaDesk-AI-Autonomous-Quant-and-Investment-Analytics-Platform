"""The Supervisor: what kind of request this is, and what the user is told.

Five destinations, and the routing decision that picks between them is validated rather than
trusted. The model returns a JSON object; Pydantic decides whether it is a decision this
application can act on. A destination outside the five, a missing company, or a period token
that is not in the closed set are all validation failures, and a validation failure is retried
once and then reported rather than guessed at.

**One destination is not the model's to choose.** `company_not_stored` is applied by the
application, from the database, the moment the company is unambiguous -- before the router is
even consulted about it. Whether this system holds a company is something it can look up, and a
model asked to decide it would be guessing at an answer that is already written down. The
routing prompt is told not to return that destination.

**Routing and resolution are one call, not two.** Deciding "this needs Module 1" and deciding
"about NVDA, over these dates" are the same judgement, and splitting them would mean two
requests that can disagree with each other. The resolution the model proposes is then checked
in code against the run's explicit arguments -- see `app.agent.context.resolve_request`.

**The Supervisor does not call tools.** It has no tools. That is what stops it answering a
company question from its own memory: it can only route to the agent that reads this
application's data, or tell the user why it cannot.

**The final answer is the Supervisor's, and its citations are checked.** The composition step
receives the agent's structured findings, the evidence entries themselves and every limitation
the tools reported -- not a paraphrase. The references it cites are validated against the
run's evidence map, and an answer citing something that does not exist gets one correction
before the run is reported as incomplete. The application never rewrites the prose itself:
inventing a citation to replace an invented citation would be the same mistake with more
confidence.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, ValidationError

from app.agent.budget import BudgetExhausted, RunBudget
from app.agent.context import (
    PERIOD_NONE,
    PERIOD_TOKENS,
    KnownSymbols,
    RequestInputs,
    RunContext,
    resolve_request,
)
from app.agent.evidence import EvidenceMap
from app.agent.llm import ModelClient
from app.agent.module1 import Module1Outcome
from app.agent.module2 import ProposalOutcome
from app.agent.progress import (
    KIND_COMPOSING,
    KIND_ROUTING,
    NULL_PROGRESS,
    Progress,
    ProgressEvent,
)
from app.agent.prompts import (
    CORRECTION_INSTRUCTION,
    NO_EVIDENCE_NOTE,
    SUPERVISOR_PROMPT,
)

logger = logging.getLogger(__name__)

# How many evidence entries the composition step is shown. Beyond this the list is trimmed,
# and the fact that it was trimmed is stated in the prompt: a model composing an answer from a
# partial evidence list has to know the list is partial.
MAX_EVIDENCE_IN_PROMPT = 24

# How many times a malformed routing decision is retried before the run gives up on it.
ROUTING_ATTEMPTS = 2

# How many times the final answer may be corrected for citing references that do not exist.
# One: a model that invents a reference twice is not converging, and the run should say so
# rather than keep paying for attempts.
CORRECTION_ATTEMPTS = 1

# What the answer must not do with dates. Three dates are in play and two of them are routinely
# different numbers, so a model that reads one out of a tool payload and reports it as the
# run's will be wrong in a way that matters: a filing accepted between the two cutoffs is
# admissible in the filing discussion and excluded from the market comparison, and an answer
# that runs them together can attribute evidence to a comparison that ruled it out.
DATE_RULES = """\
On dates, and this is a common way for the answer to be wrong:

- The **market comparison period** above bounds the price comparison and the transactions it
  counts. Nothing outside it belongs to that comparison.
- The **overall information date** above bounds what was knowable for the run as a whole. It is
  routinely later than the comparison period, because a question asked today about a window
  that ended days ago was still asked today.
- A tool result may report **its own** narrower cutoff. That is that tool's, never this run's.
  Do not present it as the overall information date, and do not quote it as though it governed
  the run.
- Do **not** attribute anything admitted under the wider information date to the market
  comparison. If a filing was readable for the filing discussion but falls outside the
  comparison period or its cutoff, say which discussion it belongs to rather than folding it
  into the comparison."""


class Destination(StrEnum):
    """The five things a request can be. A closed set, validated rather than hoped for."""

    MODULE1_ANALYSIS = "module1_analysis"
    # Module 2's portfolio-wide proposal. It is never chosen by the router: the only thing that
    # produces one is a person pressing a button, and asking a model to classify a button click
    # would spend a request to learn something the caller already said. It is a destination here
    # because it is dispatched through the same graph as everything else, not because the model
    # may pick it.
    REBALANCE_PROPOSAL = "rebalance_proposal"
    CLARIFICATION_NEEDED = "clarification_needed"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    # A definite ticker this system holds nothing for. Its own destination rather than a kind
    # of "unsupported": trading is something this application does not do, whereas Tesla is
    # something it simply has no data about, and telling a user the first when the second is
    # true sends them looking for a feature that is not missing.
    COMPANY_NOT_STORED = "company_not_stored"
    SIMPLE_RESPONSE = "simple_response"


# A ticker this system will act on: one to ten characters, starting with a letter, allowing the
# digits and separators real tickers use (BRK.B, RDS-A).
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def looks_like_a_ticker(symbol: str | None) -> bool:
    """Whether this is a *definite* claim about a company, rather than a name that failed to
    resolve.

    Shape alone is not enough and the case is what settles it. "Apple" is five letters starting
    with a capital -- syntactically indistinguishable from a ticker -- but it is a *name*, and
    treating it as a definite ticker would answer "this system holds nothing for Apple" to
    somebody who meant AAPL. Tickers are written in capitals; prose is not. So a token has to be
    both ticker-shaped and uppercase to count as one, and anything else is a lookup that failed
    and gets asked about instead.

    The residual, stated rather than hidden: an all-caps word that is not a ticker ("APPLE")
    still reads as a definite claim and will be reported as not stored. That is the safe
    direction to be wrong in only up to a point, and the answer names what *is* stored so the
    user can correct it in one turn.
    """
    if not symbol:
        return False
    stripped = symbol.strip()
    return stripped.isupper() and bool(_TICKER.match(stripped))


class SupervisorRoute(BaseModel):
    """The routing decision, as the model returns it.

    Every field the application acts on is declared here, so a decision that is missing
    something it needs fails validation at this boundary instead of surfacing as a strange
    answer later. `extra="ignore"` because a model adding a field it was not asked for has not
    made a mistake worth a retry.
    """

    model_config = ConfigDict(extra="ignore")

    destination: Destination
    reason: str = ""
    symbol: str | None = None
    period: str = "none"
    start_date: date | None = None
    end_date: date | None = None
    as_of: date | None = None
    clarification_question: str | None = None
    unsupported_reason: str | None = None
    # What this run will and will not cover, for a request that mixes a supported part with
    # an unsupported one.
    scope_note: str | None = None

    def validation_problem(self) -> str | None:
        """What is missing for this decision to be actionable, or None if it is ready."""
        if self.period not in PERIOD_TOKENS:
            return (
                f"period was {self.period!r}; it must be one of "
                f"{', '.join(PERIOD_TOKENS)}"
            )
        if self.destination is Destination.MODULE1_ANALYSIS and not self.symbol:
            return "destination was module1_analysis but no symbol was given"
        if self.destination is Destination.CLARIFICATION_NEEDED and not (
            self.clarification_question or ""
        ).strip():
            return (
                "destination was clarification_needed but no clarification_question was "
                "given"
            )
        if self.period == "explicit" and (self.start_date is None or self.end_date is None):
            return "period was explicit but both start_date and end_date were not given"
        return None


@dataclass
class SupervisorOutcome:
    """What the Supervisor decided and, when it produced one, the answer.

    `context` is set only when the request was settled, which is exactly when Module 1 is
    allowed to run. A caller that finds `context` None has a run that needs the user to say
    something more.
    """

    route: SupervisorRoute | None
    answer: str | None
    citations: list[str]
    invalid_citations: list[str]
    corrected: bool
    module1: Module1Outcome | None
    module2: ProposalOutcome | None
    stopped_by: str | None
    context: RunContext | None = None
    routing_error: str | None = None


class SupervisorState(TypedDict, total=False):
    route: dict[str, Any]
    answer: str | None
    citations: list[str]
    invalid: list[str]
    corrected: bool
    module1_findings: dict[str, Any] | None
    stopped: bool


# --- routing ------------------------------------------------------------------------------


def _routing_request(inputs: RequestInputs) -> str:
    """The routing question, with the resolved request stated plainly.

    The model is asked for a JSON object rather than a tool call because the provider's
    `response_format` supports `json_object` and its named-tool forcing is unavailable in
    thinking mode -- a mode this build does not use today but should not be pinned to.
    """
    return (
        f"User question:\n{inputs.question}\n\n"
        f"{inputs.describe_for_prompt()}\n\n"
        "Decide what kind of request this is. Reply with a single JSON object with these "
        "keys:\n"
        "  destination: one of module1_analysis, clarification_needed, "
        "unsupported_capability, simple_response. Do not use company_not_stored -- the "
        "application checks stored data itself and will apply it when it applies.\n"
        "  reason: one sentence saying why\n"
        "  symbol: the ticker this is about, or null\n"
        f"  period: one of {', '.join(PERIOD_TOKENS)}\n"
        "  start_date / end_date: YYYY-MM-DD, only when period is explicit\n"
        "  as_of: YYYY-MM-DD information cutoff, or null to use the resolved one\n"
        "  clarification_question: the question to ask the user, when clarification is "
        "needed\n"
        "  unsupported_reason: what is not available, when unsupported_capability\n"
        "  scope_note: for a mixed request, what will be analysed and what will not\n\n"
        "Reply with only the JSON object."
    )


def parse_route(content: str | None) -> tuple[SupervisorRoute | None, str | None]:
    """Read a routing decision, or say why it could not be read.

    Returns the problem rather than raising it, because a malformed decision is retried
    rather than fatal -- at least the first time.
    """
    if not content or not content.strip():
        return None, "the reply was empty"
    try:
        raw = json.loads(content)
    except ValueError as exc:
        return None, f"the reply was not valid JSON ({exc})"
    if not isinstance(raw, dict):
        return None, "the reply was not a JSON object"
    try:
        route = SupervisorRoute.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "the reply"
        return None, f"{location}: {first['msg']}"
    return route, route.validation_problem()


# --- the answer ---------------------------------------------------------------------------


def _settle(
    route: SupervisorRoute, inputs: RequestInputs, outcome: SupervisorOutcome
) -> SupervisorRoute:
    """Turn a routing decision into a settled request, or into a question for the user.

    This is where a decision becomes something the application may act on. A decision to
    analyse is settled against the caller's explicit arguments, and a disagreement is not
    reconciled in either direction -- it becomes a clarification, because only the person who
    asked can say which of the two they meant.
    """
    if route.destination is not Destination.MODULE1_ANALYSIS:
        outcome.route = route
        return route

    resolution = resolve_request(
        reference_date=inputs.reference_date,
        explicit=inputs.explicit,
        symbol=route.symbol,
        period=route.period,
        start_date=route.start_date,
        end_date=route.end_date,
        as_of=route.as_of,
    )

    if not resolution.settled:
        logger.info("the request could not be settled: %s", resolution.conflict)
        settled = SupervisorRoute(
            destination=Destination.CLARIFICATION_NEEDED,
            reason="the request could not be settled from what was supplied",
            symbol=route.symbol,
            period=route.period,
            clarification_question=resolution.conflict,
            scope_note=route.scope_note,
        )
        outcome.route = settled
        return settled

    # --- is there anything to analyse? -------------------------------------------------
    #
    # Checked here, in code, immediately after the company is unambiguous and before any model
    # request is spent on it. The database is authoritative about what it holds; asking a model
    # to decide it would be asking it to guess at something this application can look up.
    unavailable = _unknown_company(resolution.resolved.symbol, inputs)
    if unavailable is not None:
        outcome.route = unavailable
        if unavailable.destination is Destination.COMPANY_NOT_STORED:
            # Written here rather than by the model, and deliberately so. The database settled
            # this; there is nothing for a model to add, the wording is fixed, and the refusal
            # then cannot fail because a provider is unreachable or a budget ran out. A
            # question this application can answer outright should not depend on either.
            outcome.answer = company_not_stored_answer(
                unavailable.symbol or "", inputs.known
            )
        return unavailable

    outcome.context = RunContext(
        question=inputs.question,
        reference_date=inputs.reference_date,
        resolved=resolution.resolved,
        explicit=inputs.explicit,
        known=inputs.known,
    )
    outcome.route = route
    return route


def company_not_stored_answer(symbol: str, known: KnownSymbols) -> str:
    """What to tell someone who asked about a company this system holds nothing for.

    Three things it must not do, each of which is a way this answer usually goes wrong. It must
    not present the gap as a **missing capability** -- trading is something this application
    does not do, and Tesla is something it has no data about, and telling a user the first when
    the second is true sends them looking for a feature that is not missing. It must not say
    anything **about the company**, because nothing here is known about it. And it must not
    leave the user with nowhere to go.
    """
    ingested = ", ".join(known.companies) if known.companies else "none"
    held = ", ".join(known.holdings) if known.holdings else "none"
    lines = [
        f"This system holds no data for {symbol}, so it cannot answer that from its own "
        "records.",
        "",
        "That is a limit of what is stored here, not a statement about the company and not a "
        "capability this system lacks. Nothing here is known about its prices, its filings or "
        "its results, so nothing about them is being reported.",
        "",
        f"Companies it does hold data for: {ingested}.",
        f"Symbols held in the demo portfolio: {held}.",
    ]
    only_held = [item for item in known.holdings if item not in known.companies]
    if only_held:
        lines.append(
            f"Note that {', '.join(only_held)} can still be asked about as a holding, but "
            "there is no market, insider or filing data stored for them."
        )
    lines.append("")
    lines.append(
        "Ask again about one of those, or ingest the company first, and the question can be "
        "answered."
    )
    return "\n".join(lines)


def _unknown_company(
    symbol: str, inputs: RequestInputs
) -> SupervisorRoute | None:
    """A refusal, or a question, when the resolved company cannot be acted on.

    Two different situations, and conflating them is the mistake this exists to prevent.

    A **definite ticker** that is in neither the ingested companies nor the portfolio's
    holdings is a company this system holds nothing for. That is a fact the database settles,
    so it is answered without spending a model request -- and it is *not* reported as an
    unsupported capability, because nothing about the request was unsupported.

    A symbol that is **not ticker-shaped** is the model failing to resolve a name, not evidence
    that the company is absent. "Apple Inc." and "the chip maker" are not tickers, and this
    system does not translate names into tickers -- so the answer is to ask which one, never to
    assert that nothing is stored.
    """
    if inputs.known.recognises(symbol):
        return None

    if not looks_like_a_ticker(symbol):
        logger.info("the company could not be resolved to a ticker: %r", symbol)
        return SupervisorRoute(
            destination=Destination.CLARIFICATION_NEEDED,
            reason=f"{symbol!r} is not a ticker this system can look up",
            symbol=symbol,
            period="none",
            clarification_question=(
                f"I could not tell which company {symbol!r} refers to. Which ticker do you "
                "mean, for example NVDA?"
            ),
        )

    ticker = symbol.strip().upper()
    logger.info("%s is not among the stored symbols", ticker)
    return SupervisorRoute(
        destination=Destination.COMPANY_NOT_STORED,
        reason=f"{ticker} is not among the companies with stored data or portfolio holdings",
        symbol=ticker,
        period="none",
    )


def _routing_event_data(route: SupervisorRoute, outcome: SupervisorOutcome) -> dict:
    """What a routing event carries.

    The same facts the finished result exposes -- where the request went and what was settled
    for it. Not the reason the model gave for choosing, which is closer to reasoning than to
    outcome, and not the question that was asked.
    """
    resolved = outcome.context.resolved.as_dict() if outcome.context else None
    return {
        "destination": str(route.destination),
        "symbol": route.symbol,
        "resolved": resolved,
        "clarification_needed": route.destination is Destination.CLARIFICATION_NEEDED,
    }


def compose_request(
    *,
    context: RunContext,
    route: SupervisorRoute,
    module1: Module1Outcome | None,
    evidence: EvidenceMap,
) -> str:
    """The composition prompt: the findings, the evidence itself, and every limitation.

    Evidence is trimmed by count when there is too much of it, and the trim is stated. What
    is never trimmed away is the warnings: a limitation that vanishes between the tools and
    the answer is how an answer becomes wrong while looking complete.
    """
    findings = module1.findings if module1 else None

    parts = [
        f"User question:\n{context.question}",
        "",
        context.describe_for_prompt(),
    ]

    if route.scope_note:
        parts += ["", f"Scope for this run: {route.scope_note}"]

    if findings and findings.findings:
        parts += ["", "Findings from the analysis agent:"]
        parts += [f"- {finding}" for finding in findings.findings]
    else:
        parts += [
            "",
            "The analysis agent produced no structured findings for this run.",
        ]

    if findings and findings.portfolio_context:
        parts += ["", f"Portfolio context: {findings.portfolio_context}"]

    if findings and findings.next_steps:
        parts += ["", "Possible next steps the agent suggested:"]
        parts += [f"- {step}" for step in findings.next_steps]

    limitations = collect_limitations(module1)
    if limitations:
        parts += ["", "Limitations that must be carried into the answer:"]
        parts += [f"- {item}" for item in limitations]

    parts += ["", "Evidence you may cite:"]
    parts.append(
        evidence.render(limit=MAX_EVIDENCE_IN_PROMPT)
        if len(evidence)
        else NO_EVIDENCE_NOTE
    )

    parts += [
        "",
        "Write the answer for the user. Cite evidence as [E1], [E2] and so on, using only "
        "references from the list above. State the resolved company and dates. Carry every "
        "limitation forward. Separate what was observed from what you make of it, and "
        "suggest conditional next steps rather than a bare conclusion.",
        "",
        f"{DATE_RULES}",
    ]
    return "\n".join(parts)


def collect_limitations(module1: Module1Outcome | None) -> list[str]:
    """Every limitation this run knows about, deduplicated in order.

    Two sources, and the second is the one that matters. The agent's own `limitations` list
    is what it chose to report. The tools' warnings are what they actually said -- and they
    are collected from the execution log rather than from the agent, so a coverage caveat
    reaches the reader even when the agent never summarises it. That is not a hypothetical:
    the first live run of this flow produced a findings step that returned prose instead of
    JSON, and the `insufficient_coverage` warning was briefly nowhere in the answer.

    Nothing caps this list. Everything else in the prompt can be trimmed, but a limitation
    that does not reach the composition step is one that cannot reach the user.
    """
    findings = module1.findings if module1 else None
    seen: set[str] = set()
    collected: list[str] = []

    for item in (findings.limitations if findings and findings.limitations else []):
        if item not in seen:
            seen.add(item)
            collected.append(item)

    for execution in (module1.executions if module1 else []):
        for warning in execution.warnings:
            if warning not in seen:
                seen.add(warning)
                collected.append(warning)

    return collected


def cited_references(answer: str) -> list[str]:
    """The `[E<n>]` references an answer cites, in order, without duplicates."""
    found: list[str] = []
    for match in re.finditer(r"\[(E\d+)\]", answer):
        reference = match.group(1)
        if reference not in found:
            found.append(reference)
    return found


def build_supervisor_graph(
    *,
    client: ModelClient,
    inputs: RequestInputs,
    budget: RunBudget,
    evidence: EvidenceMap,
    run_module1_call,
    outcome: SupervisorOutcome,
    run_module2_call=None,
    progress: Progress = NULL_PROGRESS,
):
    """The routing graph.

    Both module callables are injected so a test can drive either directly. Neither is imported:
    this module decides *which* one runs, and the caller owns what running one means.
    """

    def route_node(state: SupervisorState) -> SupervisorState:
        # An intent the caller stated outright is applied here, in code, before the router is
        # consulted about it -- the same rule `company_not_stored` follows, and for the same
        # reason: this application already knows the answer, and a model asked to decide it would
        # be guessing at something written down. It also means the button spends no model request
        # on being classified as a button.
        if inputs.explicit_intent is not None:
            stated = SupervisorRoute(
                destination=inputs.explicit_intent,
                reason="the caller stated this intent outright",
                period=PERIOD_NONE,
            )
            outcome.route = stated
            logger.info("dispatching a stated intent straight to %s", stated.destination)
            progress(ProgressEvent(KIND_ROUTING, _routing_event_data(stated, outcome)))
            return {"route": stated.model_dump(mode="json")}

        messages = [
            {"role": "system", "content": SUPERVISOR_PROMPT},
            {"role": "user", "content": _routing_request(inputs)},
        ]
        problem: str | None = None

        for attempt in range(1, ROUTING_ATTEMPTS + 1):
            try:
                budget.before_model_request()
            except BudgetExhausted as exhausted:
                logger.warning("routing did not happen: %s", exhausted)
                outcome.routing_error = exhausted.detail
                return {"route": {}, "stopped": True}

            response = client.complete(
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=budget.max_output_tokens,
            )
            budget.record_model(
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )

            parsed, problem = parse_route(response.content)
            if parsed is None:
                logger.warning("routing attempt %d was unusable: %s", attempt, problem)
                if attempt < ROUTING_ATTEMPTS:
                    messages = [
                        *messages,
                        response.message,
                        {
                            "role": "user",
                            "content": (
                                f"That decision could not be used: {problem}. Reply with "
                                "only a JSON object matching the description above."
                            ),
                        },
                    ]
                continue

            if (
                parsed.destination is Destination.REBALANCE_PROPOSAL
                and inputs.explicit_intent is not Destination.REBALANCE_PROPOSAL
            ):
                # The enum has a member the router may not choose, and this is where that is
                # enforced rather than asked for. A model that returns it for a typed question has
                # not found a hidden capability -- it has invented one, and a portfolio-wide
                # rebalance is not an answer to a question about a company.
                problem = (
                    f"{parsed.destination} is not a destination that may be chosen here; it is "
                    "applied only for a request that states it outright, and this one did not"
                )
                logger.warning("routing attempt %d returned %s", attempt, parsed.destination)
                if attempt < ROUTING_ATTEMPTS:
                    messages = [
                        *messages,
                        response.message,
                        {
                            "role": "user",
                            "content": (
                                f"That decision could not be used: {problem}. Reply with only "
                                "a JSON object matching the description above."
                            ),
                        },
                    ]
                continue

            settled_route = _settle(parsed, inputs, outcome)
            progress(
                ProgressEvent(
                    KIND_ROUTING,
                    _routing_event_data(settled_route, outcome),
                )
            )
            return {"route": settled_route.model_dump(mode="json")}

        # Two unusable decisions. The run says so rather than picking a destination on the
        # model's behalf -- guessing here would be the application inventing a decision.
        outcome.routing_error = problem or "the routing decision could not be read"
        return {"route": {}, "stopped": True}

    def analysis_node(state: SupervisorState) -> SupervisorState:
        # `outcome.context` is set by `_settle`, which is the only way this node is reached
        # with `module1_analysis` as the destination. The settled context is handed to the
        # agent rather than looked up by it, so the agent has no way to run against anything
        # else -- there is exactly one context per run and this is it.
        assert outcome.context is not None
        module1 = run_module1_call(outcome.context)
        outcome.module1 = module1
        findings = module1.findings
        return {
            "module1_findings": findings.model_dump(mode="json") if findings else None
        }

    def rebalance_node(state: SupervisorState) -> SupervisorState:
        """Module 2's workflow, reached only by a stated intent.

        The callable is injected rather than imported so that this module does not depend on how
        a proposal is actually produced -- it dispatches, and `app.rebalance_api` owns the
        snapshot, the providers and the persistence. That is the same boundary `run_module1_call`
        draws on the other branch, and it is what keeps the two modules from reaching into each
        other.
        """
        assert run_module2_call is not None  # noqa: S101 - only reached by a stated intent
        outcome.module2 = run_module2_call()
        return {}

    def compose_node(state: SupervisorState) -> SupervisorState:
        # `_settle` guarantees a context on the only path that reaches composition.
        assert outcome.context is not None
        return _compose(
            state=state,
            client=client,
            context=outcome.context,
            budget=budget,
            evidence=evidence,
            outcome=outcome,
            progress=progress,
        )

    def reply_node(state: SupervisorState) -> SupervisorState:
        """A destination that needs no tools and no evidence, but still needs prose."""
        try:
            budget.before_model_request()
        except BudgetExhausted as exhausted:
            outcome.routing_error = exhausted.detail
            return {"stopped": True}

        route = outcome.route
        instruction = {
            Destination.CLARIFICATION_NEEDED: (
                "The request needs clarifying. Ask the user exactly this, in one short "
                "question, and nothing else:\n\n"
                f"{route.clarification_question if route else ''}"
            ),
            Destination.UNSUPPORTED_CAPABILITY: (
                "Explain plainly that this is not available, what is available instead, and "
                "why. Keep it short and do not invent capabilities.\n\n"
                f"Reason: {route.unsupported_reason if route else ''}"
            ),
            Destination.SIMPLE_RESPONSE: (
                "Answer briefly and conversationally. You have no company data for this "
                "question; if it turns out to need some, say which company and period you "
                "would need and that a question about them can be run."
            ),
        }[route.destination]

        response = client.complete(
            messages=[
                {"role": "system", "content": SUPERVISOR_PROMPT},
                {"role": "user", "content": f"User question:\n{inputs.question}"},
                {"role": "user", "content": instruction},
            ],
            max_tokens=budget.max_output_tokens,
        )
        budget.record_model(
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )
        # Recorded on the outcome as well as in the state. The state is what the graph
        # carries between nodes; the outcome is what the run reports, and a clarification or
        # refusal that only existed in the state would reach the caller as no answer at all.
        outcome.answer = response.content or ""
        return {"answer": outcome.answer}

    def after_route(state: SupervisorState) -> str:
        if state.get("stopped"):
            return END
        destination = outcome.route.destination if outcome.route else None
        if destination is Destination.MODULE1_ANALYSIS:
            return "analysis"
        if destination is Destination.REBALANCE_PROPOSAL:
            return "rebalance"
        # A company this system holds nothing for is answered without a model request at all.
        # The database settled it; there is nothing for a model to add, and spending a request
        # on it would make the refusal depend on the provider being reachable.
        if destination is Destination.COMPANY_NOT_STORED:
            return END
        return "reply"

    graph = StateGraph(SupervisorState)
    graph.add_node("route", route_node)
    graph.add_node("analysis", analysis_node)
    graph.add_node("compose", compose_node)
    graph.add_node("reply", reply_node)
    graph.add_node("rebalance", rebalance_node)
    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route",
        after_route,
        {"analysis": "analysis", "reply": "reply", "rebalance": "rebalance", END: END},
    )
    graph.add_edge("analysis", "compose")
    graph.add_edge("compose", END)
    graph.add_edge("reply", END)
    # Module 2 ends the run itself: it produces a structured proposal, not prose, and there is
    # nothing for the composition step to write. Its own record is the output.
    graph.add_edge("rebalance", END)
    return graph.compile()


def _compose(
    *,
    state: SupervisorState,
    client: ModelClient,
    context: RunContext,
    budget: RunBudget,
    evidence: EvidenceMap,
    outcome: SupervisorOutcome,
    progress: Progress = NULL_PROGRESS,
) -> SupervisorState:
    """Write the final answer, and check what it cited.

    The check is on the references, and only on the references. Whether the prose is
    factually supported by the passages is a different question, and one this code cannot
    answer -- see the README, and the live-demo review, which is what that question is for.
    """
    route = outcome.route or SupervisorRoute(destination=Destination.MODULE1_ANALYSIS)
    prompt = compose_request(
        context=context, route=route, module1=outcome.module1, evidence=evidence
    )
    progress(ProgressEvent(KIND_COMPOSING))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SUPERVISOR_PROMPT},
        {"role": "user", "content": prompt},
    ]

    answer: str | None = None
    invalid: list[str] = []

    for attempt in range(0, CORRECTION_ATTEMPTS + 1):
        try:
            budget.before_model_request()
        except BudgetExhausted as exhausted:
            logger.warning("the answer was not composed: %s", exhausted)
            outcome.stopped_by = budget.stopped_by
            return {"stopped": True, "answer": answer}

        response = client.complete(
            messages=messages, max_tokens=budget.max_output_tokens
        )
        budget.record_model(
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )
        answer = response.content or ""
        cited = cited_references(answer)
        valid, invalid = evidence.validate(cited)
        outcome.invalid_citations = invalid
        outcome.citations = valid

        if not invalid:
            outcome.answer = answer
            return {"answer": answer, "citations": valid, "invalid": []}

        logger.warning("the answer cited %s, which do not exist", ", ".join(invalid))
        if attempt < CORRECTION_ATTEMPTS:
            outcome.corrected = True
            messages = [
                *messages,
                response.message,
                {
                    "role": "user",
                    "content": CORRECTION_INSTRUCTION.format(
                        invalid=", ".join(invalid),
                        verb="is" if len(invalid) == 1 else "are",
                        available=", ".join(evidence.references()) or "none",
                    ),
                },
            ]

    # Still citing references that do not exist. The answer is withheld rather than shown
    # with invented citations, and the run reports itself incomplete.
    logger.warning("the answer still cited unknown references after correction")
    outcome.answer = None
    return {"answer": None, "citations": outcome.citations, "invalid": invalid}


def run_supervisor(
    *,
    client: ModelClient,
    inputs: RequestInputs,
    budget: RunBudget,
    evidence: EvidenceMap,
    run_module1_call,
    run_module2_call=None,
    progress: Progress = NULL_PROGRESS,
    recursion_limit: int | None = None,
) -> SupervisorOutcome:
    """Route the request and, when it warrants one, produce the final answer.

    `run_module2_call` is only reachable through `RequestInputs.explicit_intent`: a question typed
    into the chat can never be routed to a portfolio-wide rebalance, however it is worded.
    """
    outcome = SupervisorOutcome(
        route=None,
        answer=None,
        citations=[],
        invalid_citations=[],
        corrected=False,
        module1=None,
        module2=None,
        stopped_by=None,
    )
    graph = build_supervisor_graph(
        client=client,
        inputs=inputs,
        budget=budget,
        evidence=evidence,
        run_module1_call=run_module1_call,
        run_module2_call=run_module2_call,
        outcome=outcome,
        progress=progress,
    )
    initial: SupervisorState = {"route": {}, "stopped": False}
    final = graph.invoke(
        initial, {"recursion_limit": recursion_limit or (2 * budget.max_model_requests + 6)}
    )

    if final.get("stopped") and budget.stopped_by is None:
        budget.stop("supervisor_step_limit", "the run did not finish within its step budget")

    outcome.stopped_by = budget.stopped_by
    return outcome


def describe_route(route: SupervisorRoute | None) -> str:
    if route is None:
        return "no routing decision"
    return f"{route.destination} ({route.reason})"


__all__ = [
    "CORRECTION_ATTEMPTS",
    "company_not_stored_answer",
    "looks_like_a_ticker",
    "Destination",
    "MAX_EVIDENCE_IN_PROMPT",
    "ROUTING_ATTEMPTS",
    "SupervisorOutcome",
    "SupervisorRoute",
    "SupervisorState",
    "build_supervisor_graph",
    "cited_references",
    "compose_request",
    "describe_route",
    "parse_route",
    "run_supervisor",
]
