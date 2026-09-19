"""Doubles for the agent tests: a scripted model, and the ways a model goes wrong.

The suite runs offline and spends nothing, so nothing here reaches DeepSeek. What it must not
do is stand in for the *code under test*: the two LangGraphs are real, the budget is real, the
evidence map is real, and the four tools are the real ones reading a real PostgreSQL. Only the
model is replaced.

The double reproduces the provider's own message shape, because the graphs append
`response.message` to the conversation and a double that produced a different shape would make
the tests pass against a history the real client never sends.
"""

import contextlib
import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from unittest import mock

from sqlalchemy.orm import Session

from app.agent.llm import (
    ModelResponse,
    ProviderAuthError,
    ProviderResponseError,
    ProviderUnavailableError,
    ToolCall,
    assistant_message,
)


def call(call_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    """One tool call as the scripted model should emit it."""
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments)}


def raw_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    """A tool call whose argument string is not valid JSON, for the failure paths."""
    return {"id": call_id, "name": name, "arguments": arguments}


def say(content: str) -> tuple[str, list]:
    """A turn with prose and no tool calls."""
    return (content, [])


def calls(content: str | None, *tool_calls: Mapping[str, Any]) -> tuple[str | None, list]:
    """A turn that asks for tools."""
    return (content, [dict(item) for item in tool_calls])


def route(
    destination: str = "module1_analysis",
    *,
    symbol: str | None = "NVDA",
    period: str = "explicit",
    start_date: str | None = "2026-08-06",
    end_date: str | None = "2026-09-17",
    as_of: str | None = None,
    reason: str = "the question is about stored company data",
    clarification_question: str | None = None,
    unsupported_reason: str | None = None,
    scope_note: str | None = None,
) -> tuple[str, list]:
    """A routing decision, already serialised the way the model returns it."""
    return (
        json.dumps(
            {
                "destination": destination,
                "reason": reason,
                "symbol": symbol,
                "period": period,
                "start_date": start_date,
                "end_date": end_date,
                "as_of": as_of,
                "clarification_question": clarification_question,
                "unsupported_reason": unsupported_reason,
                "scope_note": scope_note,
            }
        ),
        [],
    )


def findings(
    *items: str,
    refs: Sequence[str] = (),
    limitations: Sequence[str] = (),
    portfolio: str | None = None,
    next_steps: Sequence[str] = (),
) -> tuple[str, list]:
    """A structured findings report, as the summarise step asks for."""
    return (
        json.dumps(
            {
                "findings": list(items),
                "evidence_refs": list(refs),
                "limitations": list(limitations),
                "portfolio_context": portfolio,
                "next_steps": list(next_steps),
            }
        ),
        [],
    )


class ScriptedModel:
    """A model that returns a fixed sequence of responses, and records what it was sent.

    Running out of script raises rather than repeating the last turn: a test that needed more
    turns than it wrote should fail loudly, not loop.
    """

    model_name = "scripted/test-model"

    def __init__(self, script: Sequence[tuple[Any, Sequence[Mapping[str, Any]]]]) -> None:
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []

    @property
    def remaining(self) -> int:
        return len(self._script)

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        response_format: Mapping[str, Any] | None = None,
        max_tokens: int,
    ) -> ModelResponse:
        self.requests.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": list(tools) if tools else None,
                "response_format": dict(response_format) if response_format else None,
                "max_tokens": max_tokens,
            }
        )
        if not self._script:
            raise AssertionError(
                f"the scripted model was asked for turn {len(self.requests)} but only "
                f"{len(self.requests) - 1} were written"
            )

        content, tool_calls = self._script.pop(0)
        parsed = tuple(
            ToolCall(
                id=item["id"],
                name=item["name"],
                arguments=item["arguments"],
            )
            for item in tool_calls
        )
        return ModelResponse(
            content=content,
            tool_calls=parsed,
            finish_reason="tool_calls" if parsed else "stop",
            message=assistant_message(content=content, tool_calls=parsed),
            prompt_tokens=100,
            completion_tokens=20,
        )


class FailingModel:
    """A model that always fails, with a chosen error type.

    The error type matters: the application treats an authentication failure and a rate limit
    completely differently, so a double that could only raise one of them could not test
    either behaviour properly.
    """

    model_name = "scripted/failing-model"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or ProviderUnavailableError("the provider was unavailable")
        self.calls = 0

    def complete(self, **_: Any) -> ModelResponse:
        self.calls += 1
        raise self.error


class FailOnceModel(ScriptedModel):
    """Fails the first N requests with a chosen error, then follows the script."""

    def __init__(
        self,
        script: Sequence[tuple[Any, Sequence[Mapping[str, Any]]]],
        *,
        failures: int = 1,
        error: Exception | None = None,
    ) -> None:
        super().__init__(script)
        self.failures = failures
        self.error = error or ProviderUnavailableError("temporarily unavailable")
        self.failed = 0

    def complete(self, **kwargs: Any) -> ModelResponse:
        if self.failed < self.failures:
            self.failed += 1
            self.requests.append({"messages": [], "tools": None, "response_format": None,
                                  "max_tokens": 0})
            raise self.error
        return super().complete(**kwargs)


class SessionProxy:
    """Hands the tool dispatcher the test's own session without closing it afterwards.

    Closing it would return the connection the outer rollback depends on, so the dispatcher's
    `with SessionLocal() as session:` gets this instead. The same shape
    `test_analyze_insiders_command.py` already uses for the same reason.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


@contextlib.contextmanager
def tool_sessions(session: Session) -> Iterator[None]:
    """Point the tool dispatcher at the test's rolled-back transaction.

    Without this the dispatcher opens its own connection from `app.db`, which is connected to
    the *application* database, not the test one. A test would then be reading real stored
    data while asserting against rows it had just created in a transaction nothing else can
    see -- and it would pass or fail depending on what happened to be ingested.
    """
    with mock.patch(
        "app.agent.module1.SessionLocal", return_value=SessionProxy(session)
    ):
        yield


# Errors the application is expected to tell apart.
AUTH_ERROR = ProviderAuthError("the configured key was rejected")
RESPONSE_ERROR = ProviderResponseError("the provider rejected the request: HTTP 400")
UNAVAILABLE_ERROR = ProviderUnavailableError("the provider was unavailable: APITimeoutError")

__all__ = [
    "AUTH_ERROR",
    "RESPONSE_ERROR",
    "UNAVAILABLE_ERROR",
    "FailOnceModel",
    "FailingModel",
    "ScriptedModel",
    "SessionProxy",
    "call",
    "calls",
    "findings",
    "raw_call",
    "route",
    "say",
    "tool_sessions",
]
