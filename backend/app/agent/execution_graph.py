"""The approval workflow: a real interrupt, on the checkpointer the proposal run already uses.

**Three nodes, and the one in the middle is where a person is.** `prepare` resolves the proposal's
trades into the exact orders that may be sent, or refuses; `await_approval` interrupts and waits;
`record` writes the decision and opens the batch. Nothing in this graph submits anything to a
broker -- `app.order_executor` does that, from the batch the last node writes.

**Why a second graph rather than a node on the proposal graph.** The proposal graph's job ends
when a proposal is recorded, and `app.agent.recovery` resumes its threads by looking for rows in
`generating`. A proposal waiting for a decision is not in that state and never will be, so a
decision thread is invisible to generation recovery -- which is exactly what keeps a crash during
generation and a wait for a human from being confused for one another. It also means the
*existing* completed proposals need no compatibility path at all: their checkpoints are finished
and are never read again, because an approval starts a thread of its own and binds to the
proposal's stored record, not to its checkpoint.

**The interrupt is not decoration.** It is what makes the validated plan durable: the orders that
were checked against the market are in the checkpoint, so the confirmation that arrives later --
possibly from a restarted process, possibly minutes later -- confirms *those* orders rather than
re-resolving them against a market that has moved. That is the drift case, and it is the reason
the second click exists at all.

**Approval is refused without a checkpointer.** The interrupt needs somewhere to live, and a
graph that cannot be paused cannot be resumed by a decision endpoint. Saying so is the honest
answer; the alternative is an approval whose confirmation would silently be a fresh resolution.

**A rejection never enters this graph.** Declining a proposal is available always, means the same
thing whatever the market is doing, and creates nothing -- see `app.orders.record_rejection`. The
interrupt exists to gate *execution*, and a rejection is the absence of it.
"""

import logging
from collections.abc import Callable, Mapping
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

logger = logging.getLogger(__name__)

# What a resumed graph is told. Nothing else from the client reaches the graph: the resume value
# is built by `app.order_execution`, which is the only thing that calls `resume_execution`.
RESUME_CONFIRMED = "confirmed"


class ExecutionState(TypedDict, total=False):
    """What the approval workflow carries.

    The proposal is referenced by id rather than carried: `prepare` reads its stored record, and a
    copy of the proposal in the state could disagree with the row. The *plan* is carried, because
    it is what the person is shown and what the confirmation binds to, and it is a plain dict of
    strings so the checkpointer can serialise it.

    `refusal` is set when the orders may not be sent, and it is the reason the graph stops at
    `prepare` rather than interrupting: there is nothing to ask anybody about.
    """

    proposal_id: str
    idempotency_key: str
    plan: dict[str, Any] | None
    refusal: dict[str, Any] | None
    # True when the refusal is `renewal_required`: the plan is sound and its estimates are stale,
    # so the graph *does* interrupt and the next confirmation continues from here.
    renewal: bool
    confirmed: bool
    decision_id: str | None
    batch_id: str | None


def build_execution_graph(
    *,
    prepare: Callable[[Mapping[str, Any]], dict[str, Any]],
    record: Callable[[Mapping[str, Any]], dict[str, Any]],
    checkpointer: Any = None,
) -> Any:
    """Compile the approval graph, with the two halves of the work injected.

    `prepare` and `record` are arguments rather than imports for the same reason
    `build_module2_graph` takes `retrieve`: the interesting behaviour -- what is refused, what a
    confirmation does, what a replay writes -- is then testable without a broker, a database or a
    market clock, while the graph itself stays the real one.
    """

    def prepare_node(state: ExecutionState) -> dict[str, Any]:
        return prepare(state)

    def await_approval_node(state: ExecutionState) -> dict[str, Any]:
        """Pause here until a decision endpoint resumes this thread.

        The value passed to `interrupt` is what a caller reads back from `__interrupt__`, and it
        is deliberately the whole plan: a browser showing a confirmation is showing the orders
        that were validated, not a summary of them. What comes back is the resume value, and the
        only thing this graph takes from it is whether the person confirmed.
        """
        resumed = interrupt(
            {
                "proposal_id": state.get("proposal_id"),
                "plan": state.get("plan"),
                "renewal": bool(state.get("renewal")),
                "refusal": state.get("refusal"),
            }
        )
        confirmed = isinstance(resumed, Mapping) and bool(resumed.get(RESUME_CONFIRMED))
        return {"confirmed": confirmed}

    def record_node(state: ExecutionState) -> dict[str, Any]:
        return record(state)

    graph = StateGraph(ExecutionState)
    graph.add_node("prepare", prepare_node)
    graph.add_node("await_approval", await_approval_node)
    graph.add_node("record", record_node)

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare",
        _after_prepare,
        {"await_approval": "await_approval", END: END},
    )
    graph.add_edge("await_approval", "record")
    graph.add_edge("record", END)

    return graph.compile(checkpointer=checkpointer)


def _after_prepare(state: ExecutionState) -> str:
    """Stop before the interrupt when there is nothing to ask about.

    A refusal that is not a renewal means no order could be sent, so there is no plan to confirm
    and no reason to leave a thread waiting for an answer nobody can give. A renewal *does*
    interrupt: the orders are sound and the person is being asked to confirm them at the refreshed
    prices.
    """
    if state.get("refusal") and not state.get("renewal"):
        return END
    return "await_approval"


def thread_id(proposal_id: str) -> str:
    """The thread an approval of this proposal lives on.

    Derived from the proposal rather than stored, so a confirmation arriving in a process that
    never saw the first request can still find the interrupted thread. One proposal takes one
    decision, so there is exactly one such thread and no chance of addressing the wrong one.
    """
    return f"execution-{proposal_id}"


def config_for(proposal_id: str, *, recursion_limit: int = 25) -> dict[str, Any]:
    """The invocation config: the thread, and a ceiling on how many steps it may take."""
    return {
        "configurable": {"thread_id": thread_id(proposal_id)},
        "recursion_limit": recursion_limit,
    }


def start(graph: Any, state: ExecutionState) -> dict[str, Any]:
    """Run the graph from the beginning, to its interrupt or its end."""
    return dict(graph.invoke(state, config_for(str(state["proposal_id"]))))


def resume(graph: Any, proposal_id: str, *, confirmed: bool) -> dict[str, Any]:
    """Continue an interrupted thread, telling it whether the person confirmed.

    The resume value is built here rather than accepted from a caller: "an unrestricted
    client-controlled graph state" is exactly what this must not be, and the only thing a decision
    can say is whether it is confirmed.
    """
    return dict(
        graph.invoke(
            Command(resume={RESUME_CONFIRMED: confirmed}),
            config_for(proposal_id),
        )
    )


def interrupted(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """The value the graph paused with, or None if it did not pause."""
    pending = state.get("__interrupt__")
    if not pending:
        return None
    first = pending[0]
    value = getattr(first, "value", None)
    return dict(value) if isinstance(value, Mapping) else {}


def pending_interrupt(graph: Any, proposal_id: str) -> dict[str, Any] | None:
    """Whether this proposal's approval thread is already waiting, and what it is waiting with.

    Read from the checkpoint rather than from a column, because the wait *is* the checkpoint: a
    thread with a next step and an interrupt on it is a thread a confirmation can resume, and one
    whose graph finished is not.
    """
    snapshot = graph.get_state(config_for(proposal_id))
    if not snapshot.next:
        return None

    for task in snapshot.tasks:
        for item in getattr(task, "interrupts", ()) or ():
            value = getattr(item, "value", None)
            if isinstance(value, Mapping):
                return dict(value)
    return None


__all__ = [
    "ExecutionState",
    "RESUME_CONFIRMED",
    "build_execution_graph",
    "config_for",
    "interrupted",
    "pending_interrupt",
    "resume",
    "start",
    "thread_id",
]
