"""Doubles for the conversation tests: a scripted runner, and a way to point the store at the
test database.

The API tests exercise the real routes, the real coordination logic and the real database. Only
two things are replaced. The **model** is scripted, so a whole conversation runs offline and
spends nothing. And the **session factory** is redirected, so the conversation store writes to
the rolled-back test transaction rather than to the application database -- without that, a test
would be writing conversations into the development database and asserting against rows nothing
else could see.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import contextlib
from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any
from unittest import mock

from sqlalchemy.orm import Session

from app.agent.run import RunResult

REFERENCE = date(2026, 9, 17)

# Distinguishes "no resolved context" from "I did not say". A helper where `resolved=None`
# silently produced a default would make every test of a run that settled nothing test a run
# that settled something.
UNSET: Any = object()


class SessionProxy:
    """Hands a caller the test's session without closing it afterwards.

    Closing it would return the connection the outer rollback depends on, so a caller's
    `with SessionLocal() as session:` gets this instead.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


@contextlib.contextmanager
def conversation_sessions(session: Session) -> Iterator[None]:
    """Point the conversation store at the test's rolled-back transaction.

    Both the store and the run's symbol lookup are redirected. The second matters as much as
    the first: `run_analysis` reads the known-symbol lists itself when the caller does not
    supply them, and a test that seeded a company inside its transaction would otherwise be
    told the database holds nothing.
    """
    with mock.patch("app.conversations.SessionLocal", return_value=SessionProxy(session)):
        with mock.patch("app.agent.run.SessionLocal", return_value=SessionProxy(session)):
            yield


def a_result(
    *,
    status: str = "completed",
    question: str = "a question",
    answer: str = "an answer",
    symbol: str | None = "NVDA",
    reference_date: date = REFERENCE,
    resolved: Any = UNSET,
    citations: Sequence[dict] = (),
    limitations: Sequence[str] = (),
    run_id: str = "run-1",
    usage: dict | None = None,
    information_cutoff: Any = UNSET,
) -> RunResult:
    """A run result, for a runner double that needs to return one."""
    return RunResult(
        run_id=run_id,
        status=status,
        question=question,
        reference_date=reference_date,
        model="scripted/test-model",
        symbol=symbol,
        resolved=(
            {
                "symbol": symbol,
                "start_date": "2026-08-06",
                "end_date": "2026-09-17",
                "as_of": "2026-09-17",
                "period": "explicit",
                "period_convention": None,
            }
            if resolved is UNSET
            else resolved
        ),
        answer=answer,
        citations=list(citations),
        limitations=list(limitations),
        usage=usage or {"model_requests": 3, "tool_calls": 1, "total_tokens": 100},
        # The run's own cutoff, derived from `as_of` as the real one is -- unless the caller
        # says otherwise, which is how a run that settled nothing is expressed.
        information_cutoff=(
            _cutoff(reference_date) if information_cutoff is UNSET else information_cutoff
        ),
    )


def _cutoff(as_of: date):
    from app.analysis import information_cutoff

    return information_cutoff(as_of)


class ScriptedAnalysis:
    """A stand-in for `run_analysis`, returning prepared results and recording its calls.

    Deliberately not a mock: it records the arguments it was given, because what the API passes
    -- the question, the reference date, and above all the conversation context -- is most of
    what these tests are about.
    """

    def __init__(self, script: Sequence[RunResult] | None = None) -> None:
        self._script = list(script or [])
        self.calls: list[dict[str, Any]] = []
        self.default: RunResult | None = None

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, **kwargs: Any) -> RunResult:
        self.calls.append(kwargs)
        if self._script:
            result = self._script.pop(0)
        elif self.default is not None:
            result = self.default
        else:
            raise AssertionError(
                f"the scripted runner was asked for run {len(self.calls)} but only "
                f"{len(self.calls) - 1} were written"
            )
        # The run's own identifiers come from the caller, as the real one's do.
        if kwargs.get("run_id"):
            result = result.model_copy(update={"run_id": kwargs["run_id"]})
        return result


class ExplodingAnalysis:
    """A runner that raises, for the case where the analysis itself is broken."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or RuntimeError("something went wrong inside the run")
        self.calls = 0

    def __call__(self, **_: Any) -> RunResult:
        self.calls += 1
        raise self.error


__all__ = [
    "REFERENCE",
    "UNSET",
    "ExplodingAnalysis",
    "ScriptedAnalysis",
    "SessionProxy",
    "a_result",
    "conversation_sessions",
]
