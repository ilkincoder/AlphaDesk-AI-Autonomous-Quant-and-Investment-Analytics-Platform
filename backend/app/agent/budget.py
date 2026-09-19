"""What one analysis run is allowed to spend.

Every ceiling in this module is a counter or a clock, not a sentence in a prompt. That
distinction is the whole point: a model asked to "be brief" is being asked politely, and a
model that has run out of tool calls is stopped. Prompts drift, providers ignore them, and a
loop that trusts one will eventually make forty requests instead of four.

The limits, and what each one is for:

* **Model requests per run** -- the outer bound on everything. One routing call, the Module 1
  loop, one findings call and one composition call, plus retries.
* **Tool executions per run** -- a model that keeps re-asking the same question must not be
  able to keep the database busy indefinitely.
* **Output tokens per request** -- sent to the provider as `max_tokens`. Without it the
  provider's own default applies, which is a limit nobody here chose.
* **Characters per tool result** -- what actually goes back into the conversation. A filing
  search can return passages far larger than anything worth putting in a context window.
* **Wall-clock deadline** -- checked before every request and every tool call, so a run that
  cannot finish inside its budget stops rather than being killed from outside.

`BudgetExhausted` carries the name of the limit that fired, and that name reaches the run
result. "The run stopped" is not a useful thing to report; "the run stopped because it had used
all 10 tool calls" is.
"""

from dataclasses import dataclass
from time import monotonic

# The limit names, as reported in a run result. Constants rather than loose strings so the
# counter that fires and the report that explains it cannot disagree.
LIMIT_MODEL_REQUESTS = "model_requests"
LIMIT_TOOL_CALLS = "tool_calls"
LIMIT_DEADLINE = "deadline_seconds"


class BudgetExhausted(Exception):
    """A ceiling was reached, so the work was not started.

    Raised before the request is made rather than after, so a limit never costs a call it was
    meant to prevent.
    """

    def __init__(self, limit: str, detail: str) -> None:
        self.limit = limit
        self.detail = detail
        super().__init__(f"{limit}: {detail}")


@dataclass
class RunBudget:
    """The counters for one run, and the ceilings they are counted against.

    Deliberately mutable and deliberately per-run: a fresh instance is created for every
    call to `run_analysis`, which is what stops one run's spending from counting against
    another's, and stops one run's evidence from leaking into the next.
    """

    max_model_requests: int = 12
    max_tool_calls: int = 10
    max_output_tokens: int = 1500
    # The findings step gets more room than the others, and needs it. Its reply must restate
    # every limitation the tools reported -- ten of them is ordinary for a run that used three
    # tools -- and a JSON document cut off mid-string is not a document. 1500 was tried first
    # and the reply was truncated on every live run.
    max_findings_tokens: int = 3000
    max_tool_result_chars: int = 12000
    deadline_seconds: float = 180.0
    max_transient_retries: int = 2

    # Injected so a test can control the clock and the backoff instead of waiting for them.
    clock: object = monotonic

    started_at: float = None  # type: ignore[assignment]
    model_requests: int = 0
    tool_calls: int = 0
    transient_retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stopped_by: str | None = None
    # The sentence explaining `stopped_by`, carried to the run result so the answer can say
    # which ceiling was reached rather than only that one was.
    stop_detail: str | None = None

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()

    # --- the clock ---------------------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        return self.clock() - self.started_at

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_seconds - self.elapsed_seconds)

    @property
    def expired(self) -> bool:
        return self.elapsed_seconds >= self.deadline_seconds

    # --- spending ----------------------------------------------------------------------

    def before_model_request(self) -> None:
        """Raise unless one more model request fits inside the budget."""
        self._check_deadline()
        if self.model_requests >= self.max_model_requests:
            self._stop(
                LIMIT_MODEL_REQUESTS,
                f"the run has used all {self.max_model_requests} model requests",
            )

    def before_tool_call(self) -> None:
        """Raise unless one more tool execution fits inside the budget."""
        self._check_deadline()
        if self.tool_calls >= self.max_tool_calls:
            self._stop(
                LIMIT_TOOL_CALLS,
                f"the run has used all {self.max_tool_calls} tool calls",
            )

    def retry_allowed(self) -> bool:
        """Whether another attempt at a transient failure is within the allowance.

        Deliberately does not raise. Running out of retries means *the provider stayed down*,
        and the caller has to report that as a provider failure -- reporting it as budget
        exhaustion would name the wrong cause and send a reader looking in the wrong place.
        The deadline is separate, and does raise, because that one really is the budget.
        """
        return self.transient_retries < self.max_transient_retries

    def spend_retry(self) -> None:
        """Count one retry, refusing if the deadline has passed."""
        self._check_deadline()
        self.transient_retries += 1

    def record_model(self, *, prompt_tokens: int, completion_tokens: int) -> None:
        self.model_requests += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    def record_tool(self) -> None:
        self.tool_calls += 1

    # --- reporting ---------------------------------------------------------------------

    def stop(self, limit: str, detail: str) -> None:
        """Record that the run stopped, without raising.

        Used where the work is already done and there is nothing left to prevent -- running
        out of budget between the last tool call and the composition request, say. The
        `before_*` methods raise because they exist to prevent something; this one only
        records.
        """
        self.stopped_by = limit
        self.stop_detail = detail

    def _check_deadline(self) -> None:
        if self.expired:
            self._stop(
                LIMIT_DEADLINE,
                f"the run passed its {self.deadline_seconds:g}s deadline",
            )

    def _stop(self, limit: str, detail: str) -> None:
        self.stopped_by = limit
        raise BudgetExhausted(limit, detail)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        """The usage block of a run result. Counters and limits, never prompt text."""
        return {
            "model_requests": self.model_requests,
            "tool_calls": self.tool_calls,
            "transient_retries": self.transient_retries,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "limits": {
                "max_model_requests": self.max_model_requests,
                "max_tool_calls": self.max_tool_calls,
                "max_output_tokens": self.max_output_tokens,
                "max_findings_tokens": self.max_findings_tokens,
                "max_tool_result_chars": self.max_tool_result_chars,
                "deadline_seconds": self.deadline_seconds,
                "max_transient_retries": self.max_transient_retries,
            },
            "stopped_by": self.stopped_by,
            "stop_detail": self.stop_detail,
        }


def truncate_for_context(text: str, limit: int) -> tuple[str, bool]:
    """Cut `text` to `limit` characters, saying whether it was cut.

    Half of a JSON document is not valid JSON, which matters because these strings are fed
    back to a model that will try to read them. Callers therefore truncate the *values* they
    put into a result rather than the serialised result -- see
    `app.agent.module1._bounded_result`. This helper is the last line of defence for a string
    that is still too long after that, and it never silently returns something shorter.
    """
    if len(text) <= limit:
        return text, False
    return text[:limit], True


__all__ = [
    "BudgetExhausted",
    "LIMIT_DEADLINE",
    "LIMIT_MODEL_REQUESTS",
    "LIMIT_TOOL_CALLS",
    "RunBudget",
    "truncate_for_context",
]
