"""The Module 1 agent: which tool to call, and what it said.

A two-node cycle. `decide` asks the model what to do next; if it asked for tools, `act`
executes them and hands back the results; when the model stops asking, `summarise` turns what
was gathered into structured findings. LangGraph sequences those three and nothing else -- the
judgement lives in this module, written out, rather than inside a framework's agent
abstraction.

**Executing a tool call is where the trust boundary is.** Everything before it is model
output, which is a suggestion. `execute_tool_call` therefore:

* refuses a tool name that is not one of the four, before anything is looked up;
* re-derives the company, the market window and the information cutoff from the run context,
  refusing a call that disagrees -- the model chooses *which* tool and *what to look for*, not
  who the run is about or what was knowable;
* parses the argument JSON defensively, because a model can emit a string that is not JSON
  and that is a correctable mistake rather than a crash;
* opens its own short-lived database session, so no transaction is ever held open while a
  model request is in flight.

**A refusal is a message, not an exception.** The model is told which argument was wrong and
why, and gets to try again inside the budget. Only genuine failures -- provider errors, an
exhausted budget -- end the run.

**Nothing here can widen what the agent may do.** The four tool schemas are the entire
capability surface: no SQL, no shell, no code execution, no fetching, no writes. Filing text
is data the tools returned, and a sentence inside a filing that reads like an instruction is
still just text -- there is no code path by which it could become anything else.
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.agent.budget import BudgetExhausted, RunBudget
from app.agent.context import RunContext
from app.agent.evidence import EvidenceMap
from app.agent.llm import ModelClient
from app.agent.prompts import FINDINGS_SYSTEM_PROMPT, MODULE1_PROMPT
from app.agent.progress import (
    KIND_FINDINGS,
    KIND_TOOL,
    NULL_PROGRESS,
    Progress,
    ProgressEvent,
)
from app.db import SessionLocal
from app.tools import TOOLS, invoke

logger = logging.getLogger(__name__)

# Any single string inside a tool result fed back to the model is cut to this. A filing
# passage is the reason: the retrieval tool already bounds a passage at 3000 characters, and
# five of those in one message is more context than the decision in front of the model needs.
# The passage in full is still in the evidence map, for the step that writes the answer.
MAX_STRING_CHARS = 800

# How many items of any one list are kept when a result still does not fit.
MAX_LIST_ITEMS = 8

# How many evidence entries the findings step is shown, and how much of each passage. Both
# are deliberately smaller than the composition step's allowance: this step only has to digest
# what happened, and the step that writes the answer is the one that needs the passages whole.
# Digesting is also the step that failed first in practice -- given the full text of eleven
# passages, the model tried to write the report and ran past its token ceiling doing it.
MAX_EVIDENCE_FOR_FINDINGS = 16
FINDINGS_EXCERPT_CHARS = 400

# The reason recorded when the agent could not finish inside its budget.
STOPPED_AGENT_LIMIT = "agent_step_limit"


_REFERENCE = re.compile(r"\bE\d+\b")


class Module1Findings(BaseModel):
    """What the agent reports, structured, so the Supervisor gets findings and not prose.

    This reads model output, so it is tolerant in the two ways that matter and strict about
    everything else. Extra keys are ignored -- a model that adds a helpful field has not made
    a mistake worth spending a retry on -- and a field asked for as a list but returned as one
    string is read as a one-item list. That second one is not hypothetical: asked for
    `"findings": [...]`, `deepseek-flash` sometimes answers with a single sentence instead,
    and rejecting that costs a whole retry to learn nothing.
    """

    model_config = ConfigDict(extra="ignore")

    findings: list[str] = []
    evidence_refs: list[str] = []
    limitations: list[str] = []
    portfolio_context: str | None = None
    next_steps: list[str] = []

    @field_validator("findings", "limitations", "next_steps", mode="before")
    @classmethod
    def _one_string_is_a_one_item_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            # Not split on punctuation: a finding is a sentence, and splitting one on its
            # commas would turn one true statement into several wrong ones.
            return [value] if value.strip() else []
        return value

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _references_are_extracted(cls, value: Any) -> Any:
        """Pull the `E<n>` tokens out, whether they arrive as a list or as prose.

        A model asked for references often writes "E1, E2 and E5" or "see [E3]". What matters
        is which references it named, not how it punctuated them.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return _REFERENCE.findall(value)
        if isinstance(value, list):
            found: list[str] = []
            for item in value:
                found.extend(_REFERENCE.findall(str(item)))
            return found
        return value


@dataclass
class ToolExecution:
    """One tool call: what was asked, what happened, and whether it cost anything."""

    tool: str
    arguments: dict[str, Any]
    status: str | None
    reason: str | None
    references: list[str] = field(default_factory=list)
    # Set when the call was refused for a reason the run reacts to.
    rejection_code: str | None = None
    # Every warning the tool produced. Held here, on the application's own record, so that a
    # coverage caveat reaches the reader even if the model never manages to summarise it --
    # which is exactly what happened the first time this ran against the live provider.
    warnings: list[str] = field(default_factory=list)
    reused: bool = False
    rejected: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "status": self.status,
            "reason": self.reason,
            "evidence_refs": list(self.references),
            "rejection_code": self.rejection_code,
            "warnings": list(self.warnings),
            "reused_previous_result": self.reused,
            "rejected": self.rejected,
        }


# Refusal codes. A sentence tells the model what to do; a code lets the *application* act on
# the same fact without reading prose. Only the cases something else branches on get one.
REJECTION_NO_MARKET_WINDOW = "no_market_window"


class ToolCallRejected(Exception):
    """A call this application will not execute, in a form the model can correct.

    `code` is set for the refusals the run itself reacts to -- see
    `REJECTION_NO_MARKET_WINDOW` -- and left None for the ones only the model needs to know
    about. Matching on the message instead would make the behaviour depend on wording.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CachedResult:
    """A tool result already obtained in this run, kept so an identical call is not repeated."""

    text: str
    status: str
    reason: str | None
    references: tuple[str, ...]
    warnings: tuple[str, ...]
    summary: str


@dataclass
class Module1Outcome:
    """Everything the Supervisor and the run result need from the agent."""

    findings: Module1Findings | None
    executions: list[ToolExecution]
    stopped_by: str | None
    messages: list[dict[str, Any]]


def tool_schemas() -> list[dict[str, Any]]:
    """The four tools, as the provider wants them declared.

    Built from the request models themselves rather than written out again, so a tool's
    arguments and the schema the model is shown cannot drift apart.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.request_model.model_json_schema(),
            },
        }
        for spec in TOOLS.values()
    ]


# --- the trust boundary -----------------------------------------------------------------


def enforced_arguments(
    tool: str, arguments: Mapping[str, Any], context: RunContext
) -> dict[str, Any]:
    """The arguments a call will actually run with, or a refusal explaining why not.

    The company, the market window and the information cutoff come from the run context. A
    call may omit them -- they are filled in -- but a call that *disagrees* with them is
    refused, because agreeing would mean answering a question the user did not ask.
    """
    spec = TOOLS.get(tool)
    if spec is None:
        raise ToolCallRejected(
            f"{tool!r} is not an available tool. Available tools: {', '.join(TOOLS)}."
        )

    effective = dict(arguments)
    resolved = context.resolved

    for name in spec.request_model.model_fields:
        if name == "symbol":
            _settle_symbol(effective, arguments, resolved)
        elif name == "as_of":
            _settle_cutoff(effective, arguments, resolved)
        elif name in ("start_date", "end_date"):
            _settle_window(effective, arguments, name, resolved, tool)

    return effective


def _settle_symbol(
    effective: dict[str, Any], arguments: Mapping[str, Any], resolved: Any
) -> None:
    given = arguments.get("symbol")
    if given is not None and str(given).strip().upper() != resolved.symbol.upper():
        raise ToolCallRejected(
            f"symbol was {given!r}, but this run is about {resolved.symbol}. A tool call "
            "cannot change which company is being analysed; use the resolved company or "
            "omit the argument."
        )
    # Always the resolved spelling, never the caller's. A restated "nvda" and an omitted
    # symbol are the same call, and normalising here is what makes the identical-call cache
    # recognise them as one rather than executing the same question twice.
    effective["symbol"] = resolved.symbol


def _settle_cutoff(
    effective: dict[str, Any], arguments: Mapping[str, Any], resolved: Any
) -> None:
    """The information cutoff may be pulled *earlier*, never pushed later.

    An earlier cutoff is a strictly more conservative question -- "what was knowable by then"
    -- and refusing it would block a safer reading. A later one would let the agent read
    filings that were not public at the moment being analysed, which is the thing the cutoff
    exists to prevent.
    """
    given = arguments.get("as_of")
    if given is None:
        effective["as_of"] = resolved.as_of
        return
    parsed = _as_date(given, "as_of")
    if parsed > resolved.as_of:
        raise ToolCallRejected(
            f"as_of was {parsed.isoformat()}, which is later than this run's information "
            f"cutoff of {resolved.as_of.isoformat()}. A later cutoff would read filings that "
            "were not public at the time being analysed."
        )
    effective["as_of"] = parsed


def _settle_window(
    effective: dict[str, Any],
    arguments: Mapping[str, Any],
    name: str,
    resolved: Any,
    tool: str,
) -> None:
    """The market window is the run's, not the agent's."""
    required = resolved.start_date if name == "start_date" else resolved.end_date
    if required is None:
        raise ToolCallRejected(
            f"{tool} analyses a market window, and this run has none -- the question does "
            "not name a period. Answer without it, or ask the user which period they mean.",
            code=REJECTION_NO_MARKET_WINDOW,
        )

    given = arguments.get(name)
    if given is None:
        effective[name] = required
        return
    parsed = _as_date(given, name)
    if parsed != required:
        raise ToolCallRejected(
            f"{name} was {parsed.isoformat()}, but this run's market window is "
            f"{resolved.window_description}. Do not change it; use the resolved dates or "
            "omit them."
        )
    effective[name] = parsed


def _as_date(value: Any, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ToolCallRejected(
            f"{field_name} was {value!r}, which is not a date in YYYY-MM-DD form."
        ) from None


# --- bounding what goes back to the model -------------------------------------------------


def _shorten_strings(node: Any, limit: int) -> Any:
    if isinstance(node, dict):
        return {key: _shorten_strings(value, limit) for key, value in node.items()}
    if isinstance(node, list):
        return [_shorten_strings(value, limit) for value in node]
    if isinstance(node, str) and len(node) > limit:
        return node[:limit] + f" ... [shortened from {len(node)} characters]"
    return node


def _cap_lists(node: Any, max_items: int) -> Any:
    if isinstance(node, dict):
        return {key: _cap_lists(value, max_items) for key, value in node.items()}
    if isinstance(node, list):
        kept = [_cap_lists(value, max_items) for value in node[:max_items]]
        if len(node) > max_items:
            kept.append(f"... [{len(node) - max_items} further items not shown]")
        return kept
    return node


def bounded_tool_result(
    result: Any, limit: int, references: Sequence[str] = ()
) -> tuple[str, bool]:
    """One tool result as text, bounded, with whether it had to be cut.

    Three passes, in order of how much they cost the model. Strings are shortened -- a
    passage quoted in full is not needed to judge whether it is relevant, and the full text
    stays in the evidence map for the step that writes the answer. Then long lists lose their
    tails. Only if a result is *still* too long is its payload dropped, and that is reported
    rather than silent: the envelope's status, reason and warnings survive every pass,
    because those are the parts a later step has to be able to trust.

    The result's evidence references travel in the same message, which is how the model
    learns which identifiers it is allowed to cite.
    """
    payload = _cap_lists(_shorten_strings(result.as_json(), MAX_STRING_CHARS), MAX_LIST_ITEMS)
    if references:
        payload["_evidence_refs"] = list(references)

    text = json.dumps(payload)
    if len(text) <= limit:
        return text, False

    # Still too long: drop the payload, keep the envelope. Status, reason and warnings are
    # what the next decision depends on; the detail is already in the evidence map.
    reduced = {key: value for key, value in payload.items() if key != "data"}
    reduced["data"] = (
        "omitted: this result was too large to pass back in full. The envelope above is "
        "complete and the result is recorded in this run's evidence, citable by the "
        "references listed in _evidence_refs."
    )
    text = json.dumps(reduced)
    if len(text) > limit:
        text = text[:limit] + " ... [truncated]"
    return text, True


# --- executing --------------------------------------------------------------------------


def cache_key(tool: str, arguments: Mapping[str, Any]) -> str:
    """A stable key for "the same call", so an identical repeat can be reused."""
    return f"{tool}:{json.dumps(arguments, sort_keys=True, default=str)}"


def _tool_message(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload)


def execute_tool_call(
    tool: str,
    raw_arguments: str,
    *,
    context: RunContext,
    budget: RunBudget,
    evidence: EvidenceMap,
    cache: dict[str, CachedResult],
) -> tuple[str, ToolExecution]:
    """Run one tool call, and return the message the model sees plus the record of it."""
    arguments = _parse_arguments(raw_arguments)
    if isinstance(arguments, str):
        execution = ToolExecution(
            tool=tool, arguments={}, status=None, reason=None, rejected=arguments
        )
        return _tool_message({"error": "invalid_arguments", "detail": arguments}), execution

    try:
        effective = enforced_arguments(tool, arguments, context)
    except ToolCallRejected as rejection:
        # Refused before the database is touched. The model is told why so it can correct
        # itself, which is the whole reason this is a message rather than an exception.
        execution = ToolExecution(
            tool=tool,
            arguments=arguments,
            status=None,
            reason=None,
            rejection_code=rejection.code,
            rejected=rejection.message,
        )
        return _tool_message({"error": "rejected", "detail": rejection.message}), execution

    key = cache_key(tool, effective)
    cached = cache.get(key)
    if cached is not None:
        # The previous result still applies within this run, so it is reused rather than
        # re-executed -- and it does not spend another tool call.
        logger.info("reusing the earlier %s result for identical arguments", tool)
        execution = ToolExecution(
            tool=tool,
            arguments=effective,
            status=cached.status,
            reason=cached.reason,
            references=list(cached.references),
            warnings=list(cached.warnings),
            reused=True,
        )
        return cached.text, execution

    budget.before_tool_call()

    try:
        # One short-lived session per call. No transaction is held open across a model
        # request, and a tool's read cannot observe another tool's half-finished work.
        with SessionLocal() as session:
            result = invoke(tool, effective, session)
    except SQLAlchemyError as exc:
        # The tools already turn an unreadable database into `failed`. This catches the same
        # thing happening outside their own try blocks.
        logger.exception("tool %s could not reach the database", tool)
        execution = ToolExecution(
            tool=tool,
            arguments=effective,
            status="failed",
            reason="database_unavailable",
        )
        return (
            _tool_message(
                {
                    "error": "tool_failed",
                    "detail": (
                        f"{tool} could not run because the stored data could not be read "
                        f"({type(exc).__name__}). Nothing is known about the data."
                    ),
                }
            ),
            execution,
        )

    budget.record_tool()

    references = _record_evidence(evidence, result, tool)
    text, trimmed = bounded_tool_result(result, budget.max_tool_result_chars, references)
    if trimmed:
        logger.info("the %s result was bounded before being passed back", tool)

    cache[key] = CachedResult(
        text=text,
        status=str(result.status),
        reason=result.reason,
        references=tuple(references),
        warnings=tuple(result.warnings),
        summary=result.reason or str(result.status),
    )

    execution = ToolExecution(
        tool=tool,
        arguments=effective,
        status=str(result.status),
        reason=result.reason,
        references=list(references),
        warnings=list(result.warnings),
    )
    return text, execution


def _parse_arguments(raw_arguments: str) -> dict[str, Any] | str:
    """The parsed arguments, or a message explaining why they could not be parsed."""
    try:
        arguments = json.loads(raw_arguments or "{}")
    except (TypeError, ValueError):
        return (
            "The arguments were not valid JSON. Send a JSON object, for example "
            '{"symbol": "NVDA"}.'
        )
    if not isinstance(arguments, dict):
        return "The arguments must be a JSON object."
    return arguments


def _record_evidence(evidence: EvidenceMap, result: Any, tool: str) -> list[str]:
    """Add a result to the evidence map, one entry per citable thing.

    A filing search is the reason this returns a list: its citable things are the passages,
    not the search. Everything else contributes exactly one entry.
    """
    if tool == "filing_evidence_search":
        return evidence.add_filing_passages(
            tool=tool,
            symbol=result.symbol or "",
            status=str(result.status),
            data=result.data or {},
        )
    reference = evidence.add_tool_result(
        tool=tool,
        symbol=result.symbol or "",
        status=str(result.status),
        label=f"{tool} result for {result.symbol}",
        summary=result.as_json().get("data"),
    )
    return [reference] if reference else []


# --- the graph --------------------------------------------------------------------------


class Module1State(TypedDict, total=False):
    messages: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    finished: bool
    stopped: bool


def build_module1_graph(
    *,
    client: ModelClient,
    context: RunContext,
    budget: RunBudget,
    evidence: EvidenceMap,
    executions: list[ToolExecution],
    cache: dict[str, CachedResult] | None = None,
    holder: dict[str, Module1Findings] | None = None,
    progress: Progress = NULL_PROGRESS,
):
    """The `decide` / `act` / `summarise` graph.

    The nodes close over the run's client, context, budget and evidence map. The graph state
    carries only the conversation and what is pending, because that is all that has to travel
    between nodes -- the budget and the evidence belong to the run, not to a step.
    """
    schemas = tool_schemas()
    results: dict[str, CachedResult] = cache if cache is not None else {}
    # Filled by `summarise`, read by the caller. A single-slot holder rather than graph state
    # because the node has to report two things at once -- the parsed findings and whether the
    # graph should stop -- and LangGraph merges state by key, which cannot express "one or the
    # other". It is created by `run_module1` so it belongs to the run, not to a node.
    found: dict[str, Module1Findings] = holder if holder is not None else {}

    def decide(state: Module1State) -> Module1State:
        try:
            budget.before_model_request()
        except BudgetExhausted as exhausted:
            logger.warning("the agent stopped before deciding: %s", exhausted)
            return {"pending": [], "finished": True, "stopped": True}

        response = client.complete(
            messages=[{"role": "system", "content": MODULE1_PROMPT}, *state["messages"]],
            tools=schemas,
            max_tokens=budget.max_output_tokens,
        )
        budget.record_model(
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )

        # The assistant turn goes back verbatim, tool-call ids included: the ids are how the
        # tool messages that follow are matched to the calls they answer.
        messages = [*state["messages"], response.message]
        pending = [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in response.tool_calls
        ]
        return {"messages": messages, "pending": pending, "finished": not pending}

    def act(state: Module1State) -> Module1State:
        messages = list(state["messages"])
        stopped = False

        for call in state.get("pending", []):
            try:
                text, execution = execute_tool_call(
                    call["name"],
                    call["arguments"],
                    context=context,
                    budget=budget,
                    evidence=evidence,
                    cache=results,
                )
            except BudgetExhausted as exhausted:
                # Every tool call the assistant announced needs an answer, or the
                # conversation is one the provider would reject. The answer is that the run
                # ran out of budget -- which is true, and is also why the graph stops here.
                logger.warning("a tool call was not executed: %s", exhausted)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": _tool_message(
                            {"error": "not_executed", "detail": exhausted.detail}
                        ),
                    }
                )
                stopped = True
                break

            executions.append(execution)
            # Emitted here, by the code that ran the call, from the record of it. A client
            # watching learns which tool ran and how it went -- the same fields the finished
            # result carries, no more.
            progress(ProgressEvent(KIND_TOOL, _tool_event_data(execution)))
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": text}
            )

        return {"messages": messages, "pending": [], "finished": True, "stopped": stopped}

    def summarise(state: Module1State) -> Module1State:
        """Turn what the tools returned into structured findings, in its own conversation.

        Deliberately *not* one more turn of the tool-calling conversation. That conversation
        ends with several thousand characters of tool results and an agent that has been
        asked to analyse things, and the model reliably answers it by writing the report --
        ignoring the JSON instruction entirely, `response_format` and all. A short, separate
        conversation, given the evidence and asked only to digest it, does what it is told.
        It is also several times cheaper: the digest is a fraction of the history.
        """
        try:
            budget.before_model_request()
        except BudgetExhausted as exhausted:
            logger.warning("findings were not requested: %s", exhausted)
            return {"stopped": True}

        messages = [
            {"role": "system", "content": FINDINGS_SYSTEM_PROMPT},
            {"role": "user", "content": _findings_brief(context, evidence, executions)},
        ]
        for attempt in (1, 2):
            response = client.complete(
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=budget.max_findings_tokens,
            )
            budget.record_model(
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )
            parsed, problem = _parse_findings(response.content)
            if parsed is not None:
                found["findings"] = parsed
                progress(
                    ProgressEvent(
                        KIND_FINDINGS,
                        {
                            "findings": len(parsed.findings),
                            "limitations": len(parsed.limitations),
                        },
                    )
                )
                return {}

            # Truncation is worth naming, because it has a different fix from a malformed
            # reply: the JSON was fine, there was simply not enough room to finish it.
            truncated = response.finish_reason == "length"
            logger.warning(
                "findings attempt %d was not usable: %s%s",
                attempt,
                problem,
                " (the reply hit the token ceiling)" if truncated else "",
            )
            if attempt == 1:
                # One correction, and only one: a model that cannot produce this shape twice
                # will not manage it a third time, and the budget is better spent elsewhere.
                messages = [
                    *messages,
                    response.message,
                    {
                        "role": "user",
                        "content": (
                            "Your reply was cut off before the JSON was complete. Send the "
                            "same object again, more briefly: shorten each finding to one "
                            "clause, and keep every limitation but state each in as few "
                            "words as it takes to stay recognisable."
                            if truncated
                            else "That was not a JSON object with the keys described. Reply "
                            "with only the JSON object, and nothing else."
                        ),
                    },
                ]

        return {"stopped": True}

    def after_decide(state: Module1State) -> str:
        if state.get("stopped"):
            return END
        if state.get("pending"):
            return "act"
        return "summarise"

    def after_act(state: Module1State) -> str:
        # A run that ran out of budget mid-call must end here. Going back to `decide` would
        # make another model request, which is the one thing an exhausted budget forbids.
        return END if state.get("stopped") else "decide"

    graph = StateGraph(Module1State)
    graph.add_node("decide", decide)
    graph.add_node("act", act)
    graph.add_node("summarise", summarise)
    graph.add_edge(START, "decide")
    graph.add_conditional_edges(
        "decide", after_decide, {"act": "act", "summarise": "summarise", END: END}
    )
    graph.add_conditional_edges("act", after_act, {"decide": "decide", END: END})
    graph.add_edge("summarise", END)
    return graph.compile()


def _tool_event_data(execution: ToolExecution) -> dict:
    """What a tool event carries: the same fields the finished result exposes in its log."""
    return {
        "tool": execution.tool,
        "status": execution.status,
        "reason": execution.reason,
        "reused": execution.reused,
        "rejected": execution.rejected,
        "evidence_refs": list(execution.references),
    }


def _findings_brief(
    context: RunContext, evidence: EvidenceMap, executions: Sequence[ToolExecution]
) -> str:
    """What the findings step is given: the question, what ran, and what came back.

    Assembled by the application from its own records rather than handed the agent's
    conversation. The warnings are listed explicitly, before the evidence, because a model
    that reads them last is a model that may not reach them.
    """
    parts = [
        f"Question: {context.question}",
        "",
        context.describe_for_prompt(),
        "",
        "Tool calls that ran:",
    ]
    if not executions:
        parts.append("- none")
    for execution in executions:
        note = f"- {execution.tool}: {execution.status or 'not executed'}"
        if execution.reason:
            note += f" ({execution.reason})"
        if execution.reused:
            note += " [reused an earlier identical call]"
        if execution.rejected:
            note += f" [refused: {execution.rejected}]"
        parts.append(note)

    warnings = [warning for execution in executions for warning in execution.warnings]
    parts += ["", "Warnings the tools reported, all of which belong in `limitations`:"]
    parts += [f"- {warning}" for warning in warnings] or ["- none"]

    parts += [
        "",
        "Evidence available to cite (passages are excerpted here; the full text is kept for "
        "the step that writes the answer):",
        evidence.render(
            limit=MAX_EVIDENCE_FOR_FINDINGS, excerpt_chars=FINDINGS_EXCERPT_CHARS
        ),
    ]
    return "\n".join(parts)


def _parse_findings(content: str | None) -> tuple[Module1Findings | None, str | None]:
    """Read the findings reply, and say why if it could not be read.

    The reason is returned rather than only logged, because "the findings step produced
    nothing" is the kind of failure that is invisible from outside -- the answer still gets
    written, from the evidence, and looks fine. Naming the cause is what makes it fixable.
    """
    if not content or not content.strip():
        return None, "the reply was empty"
    try:
        raw = json.loads(content)
    except ValueError as exc:
        # Truncation and malformed output both land here, and they have different fixes, so
        # the message distinguishes them.
        return None, f"the reply was not valid JSON ({exc})"
    if not isinstance(raw, dict):
        return None, f"the reply was a JSON {type(raw).__name__}, not an object"
    try:
        return Module1Findings.model_validate(raw), None
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "the reply"
        return None, f"{location}: {first['msg']}"


def run_module1(
    *,
    client: ModelClient,
    context: RunContext,
    budget: RunBudget,
    evidence: EvidenceMap,
    progress: Progress = NULL_PROGRESS,
    recursion_limit: int | None = None,
) -> Module1Outcome:
    """Run the agent to completion, or to whatever stopped it."""
    executions: list[ToolExecution] = []
    found: dict[str, Module1Findings] = {}
    graph = build_module1_graph(
        client=client,
        context=context,
        budget=budget,
        evidence=evidence,
        executions=executions,
        holder=found,
        progress=progress,
    )

    initial: Module1State = {
        "messages": [{"role": "user", "content": _agent_brief(context)}],
        "pending": [],
        "finished": False,
        "stopped": False,
    }
    # The budget is the real bound. This is a backstop, so a graph that somehow kept cycling
    # stops rather than running until the process is killed.
    limit = recursion_limit or (2 * budget.max_model_requests + 6)

    final = graph.invoke(initial, {"recursion_limit": limit})
    messages = final.get("messages", [])
    findings = found.get("findings")

    if final.get("stopped") and budget.stopped_by is None:
        # A stop that no limit recorded: the agent could not finish its work inside the steps
        # it had, which is still a budget outcome rather than a silent empty answer.
        budget.stop(STOPPED_AGENT_LIMIT, "the agent did not finish within its step budget")

    return Module1Outcome(
        findings=findings,
        executions=executions,
        stopped_by=budget.stopped_by,
        messages=messages,
    )


def _agent_brief(context: RunContext) -> str:
    return (
        f"Question: {context.question}\n\n"
        f"{context.describe_for_prompt()}\n\n"
        "Gather what is needed with the available tools, then report your findings."
    )


__all__ = [
    "MAX_STRING_CHARS",
    "CachedResult",
    "Module1Findings",
    "Module1Outcome",
    "Module1State",
    "REJECTION_NO_MARKET_WINDOW",
    "ToolCallRejected",
    "ToolExecution",
    "bounded_tool_result",
    "build_module1_graph",
    "cache_key",
    "enforced_arguments",
    "execute_tool_call",
    "run_module1",
    "tool_schemas",
]
