"""Storing completed backtest runs, and finding the one a request already made.

Smaller than the other stores in this application because a backtest is smaller than they are.
A proposal and an execution batch are *workflows*: they have states, they hold a slot while they
run, and something has to decide what a second request gets. A backtest is a calculation with a
provider request in front of it. It takes a second or two, it holds nothing while it runs, and
the only question a repeated request raises is whether it should be paid for twice.

So there is one mechanism here, and it is the whole duplicate story: `request_id` is unique, and
a request that carries an id already on a row gets that row rather than a second run.

**Nothing is stored until the run has finished.** A row is a record of an answer, and there is no
useful half of one. A run that failed is reported as an error and writes nothing -- which also
means a retry with the same request id is free to try again rather than replaying a failure.

**A stored row is read as values, never as an ORM instance.** The session that read it closes
before the caller uses it, and an instance outliving its session raises the moment an attribute
is touched. Everything crossing this module's boundary is a plain dict.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import BacktestRun


def new_id() -> str:
    """A server-generated run identifier.

    Random rather than sequential, for the reason `app.conversations.new_id` gives: an id that
    counts runs tells anyone who reads a URL how many exist.
    """
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def find(request_id: str) -> dict | None:
    """The run this request id already produced, or `None`.

    Read before any provider request is made, so a duplicate costs nothing. The row's parameters
    are *not* compared against the incoming ones: a request id names a request, and two different
    configurations sent under one id are one request that was already answered. Re-running the
    second would be spending the provider's credits on a question nobody asked twice.
    """
    with SessionLocal() as session:
        row = session.scalar(
            select(BacktestRun).where(BacktestRun.request_id == request_id)
        )
        return _as_dict(row) if row is not None else None


def load(run_id: str) -> dict | None:
    with SessionLocal() as session:
        row = session.get(BacktestRun, run_id)
        return _as_dict(row) if row is not None else None


def latest() -> dict | None:
    """The most recently created run, or `None` if none has been stored.

    This is what the page reads on load, so it can put back the configuration and the results it
    was last showing rather than starting from an empty form.

    Ordered by `created_at` and then by `id`, like `app.proposals.latest`: two runs recorded in
    the same instant would otherwise be a tie, and what a tie falls back to would be whatever the
    database happened to return. The `created_at` index is what makes this read cheap.
    """
    with SessionLocal() as session:
        row = session.scalars(
            select(BacktestRun)
            .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
            .limit(1)
        ).first()
        return None if row is None else _as_dict(row)


def save_explanation(
    run_id: str, *, text: str, model: str | None, prompt_version: str | None
) -> dict | None:
    """Attach an explanation to a run, and return the row as it now stands.

    Written only when there is text to write. A run that could not be explained keeps its nulls
    rather than a row of blanks, which is what makes "not explained yet" and "explained" two
    states a reader can tell apart -- and what lets the next request try again rather than
    replaying a failure.

    `None` when there is no such run. The caller has already read the row to explain it, so this
    is a race rather than a lookup: the run was deleted between the two statements, and there is
    nowhere to put the text.
    """
    with SessionLocal() as session:
        row = session.get(BacktestRun, run_id)
        if row is None:
            return None
        row.explanation = text
        row.explanation_model = model
        row.explanation_prompt_version = prompt_version
        session.commit()
        return _as_dict(row)


def record(
    *,
    request_id: str,
    run: dict,
    snapshot: dict,
) -> dict:
    """Write one finished run, and return the row that stands.

    `run` is the columns; `snapshot` is the exact bars that were consumed. On the unique
    violation two simultaneous identical requests can produce, the row the other one wrote is
    read back and returned rather than the insert being retried: both requests asked the same
    question, and one answer is the right number of answers.
    """
    run_id = new_id()
    try:
        with SessionLocal() as session:
            session.add(
                BacktestRun(
                    id=run_id,
                    request_id=request_id,
                    created_at=_now(),
                    input_snapshot=snapshot,
                    **run,
                )
            )
            session.commit()
    except IntegrityError:
        existing = find(request_id)
        if existing is None:  # pragma: no cover - the violation came from this table
            raise
        return existing

    stored = load(run_id)
    if stored is None:  # pragma: no cover - the row was committed a moment ago
        raise RuntimeError(f"backtest run {run_id} was written and could not be read back")
    return stored


def _as_dict(row: BacktestRun) -> dict:
    """Every column, as plain values, read before the session closes."""
    return {
        column.name: getattr(row, column.name) for column in row.__table__.columns
    }


__all__ = ["find", "latest", "load", "new_id", "record", "save_explanation"]
