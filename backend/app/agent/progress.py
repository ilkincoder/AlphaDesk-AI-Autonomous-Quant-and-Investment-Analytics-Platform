"""What a run reports while it is running, for a client that is watching.

A run takes up to three minutes. The JSON route returns only when it is finished, which is fine
for a command line and poor for a page: the reader sees nothing at all until everything is
ready. This module is the channel that carries what is genuinely happening, so a client can say
*which* part is taking the time.

**Only real things are emitted, and only at the moment they happen.** Each event below is
raised by the code that did the work -- routing by the node that decided, a tool event by the
dispatcher that executed the call. Nothing here is a timer, a percentage, or a stage the run
does not actually have. That is the whole reason this is a channel over the existing pipeline
rather than a progress bar computed beside it.

**Nothing here is private.** No prompt text, no model reasoning, no token counts mid-flight,
no budget internals. Everything an event carries is something the finished result already
exposes: the destination, the resolved dates, which tool ran and how it went. A progress
channel that leaks more than the result would be a new disclosure surface, not a convenience.

**One event carries prose, and it is provisional.** `answer_draft` is the answer as the model
writes it, which is the one thing a reader is waiting for and the one thing a finished result
cannot convey any other way. It is emitted *before* the answer is final -- the run can still send
the text back for a correction, or stop before it has one -- so what it shows can still change.
The terminal event always carries the version that stands, and a reader must be told the draft is
not final. It is a snapshot of
everything written so far rather than a delta, so an event lost to a full queue is repaired by
the next one instead of leaving a hole in the middle of a sentence.

**Progress is a courtesy, never a dependency.** Every emission is wrapped, so an emitter that
raises logs and the run continues. A client watching a run must not be able to break it, and a
client that stops watching must not stop it either -- `app.analysis_api` runs the analysis on
its own thread for exactly that reason.
"""

import json
import logging
import queue
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# The event kinds. A closed set, because a client renders them and an unknown kind would be
# either ignored -- which is a silent bug -- or guessed at.
KIND_ROUTING = "routing"
KIND_TOOL = "tool"
KIND_FINDINGS = "findings"
KIND_COMPOSING = "composing"
# The answer, as it is being written. A snapshot of the prose so far, not a delta -- see the
# module docstring. The only event that carries model output, and the only one whose content is
# provisional.
KIND_ANSWER_DRAFT = "answer_draft"
# A named stage of a workflow that has no tools and no routing -- the Module 2 proposal run,
# which synchronises, retrieves, proposes and calculates. It is the same rule as the kinds
# above: emitted by the code that did the work, for a step the run actually has, never a timer
# or a percentage.
KIND_STAGE = "stage"
# The two terminal events. Exactly one of them ends a stream.
KIND_DONE = "done"
KIND_ERROR = "error"

TERMINAL_KINDS = frozenset({KIND_DONE, KIND_ERROR})


@dataclass(frozen=True)
class ProgressEvent:
    """One thing that happened, as it happened."""

    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS

    def as_dict(self) -> dict[str, Any]:
        """The wire shape: the kind under `event`, the rest beside it."""
        return {"event": self.kind, **self.data}


class Progress(Protocol):
    """Anything that accepts an event. A callable, so a test can pass a list's append."""

    def __call__(self, event: ProgressEvent) -> None: ...


class NullProgress:
    """The default. Accepts everything and does nothing, which is what the CLI wants."""

    def __call__(self, event: ProgressEvent) -> None:
        return None


NULL_PROGRESS = NullProgress()


def safe(progress: Progress | None) -> Progress:
    """An emitter that cannot break the run that is emitting.

    A client's connection failing mid-stream, a queue that has been closed underneath us, a
    bug in the caller's own emitter: none of those are reasons for a paid analysis to fail
    half way through. The failure is logged rather than swallowed, because an emitter that is
    always failing is a bug somebody needs to see.
    """
    if progress is None:
        return NULL_PROGRESS

    def emit(event: ProgressEvent) -> None:
        try:
            progress(event)
        except Exception:  # noqa: BLE001 - deliberately everything; see the docstring
            logger.exception("a progress event could not be delivered: %s", event.kind)

    return emit


class QueueProgress:
    """An emitter backed by a bounded queue, for a stream a client may stop reading.

    **Bounded, and it drops the oldest.** A client that stops reading must not be able to make
    the run accumulate events without limit; if it falls behind, the newest events are the ones
    worth having. Dropping is counted and reported rather than silent.

    The terminal event is never lost. It is put by the same drop-oldest path, which always
    makes room, so the last thing in the queue is always the outcome.
    """

    def __init__(self, sink: "queue.Queue[ProgressEvent]", *, maxsize: int) -> None:
        self._sink = sink
        self._maxsize = maxsize
        self.dropped = 0

    def __call__(self, event: ProgressEvent) -> None:
        try:
            self._sink.put_nowait(event)
            return
        except queue.Full:
            pass

        # Make room by discarding the oldest, then try once more. If that races with the
        # reader and fails again the event is dropped -- the terminal one included, which is
        # why the route also carries the outcome in its response body rather than relying on
        # the stream alone.
        try:
            self._sink.get_nowait()
            self.dropped += 1
        except queue.Empty:
            pass
        try:
            self._sink.put_nowait(event)
        except queue.Full:  # pragma: no cover - needs a race to reach
            self.dropped += 1


def sse_frame(event: ProgressEvent) -> str:
    """One server-sent event, over the wire.

    The kind is on the `event:` line and repeated inside the data. The repetition is
    deliberate: a client that reads only `data` -- which is most of them, and how the frontend
    here is written -- gets a self-describing payload rather than having to parse two lines in
    step.

    Here rather than in either of the two routes that write it, because it is the other end of
    `QueueProgress` above: what one puts on a queue, this turns into bytes, and the two streams
    in this application must not be able to frame an event differently from each other.
    """
    return f"event: {event.kind}\ndata: {json.dumps(event.as_dict())}\n\n"


def drain_events(events: "queue.Queue[ProgressEvent]") -> Iterator[str]:
    """Yield queued events as frames until a terminal one.

    Returns as soon as an outcome arrives, so a handler thread is never left waiting on a
    queue nothing will write to again.
    """
    while True:
        event = events.get()
        yield sse_frame(event)
        if event.terminal:
            return


__all__ = [
    "KIND_ANSWER_DRAFT",
    "KIND_COMPOSING",
    "KIND_DONE",
    "KIND_ERROR",
    "KIND_FINDINGS",
    "KIND_ROUTING",
    "KIND_STAGE",
    "KIND_TOOL",
    "NULL_PROGRESS",
    "NullProgress",
    "Progress",
    "ProgressEvent",
    "QueueProgress",
    "TERMINAL_KINDS",
    "drain_events",
    "safe",
    "sse_frame",
]
