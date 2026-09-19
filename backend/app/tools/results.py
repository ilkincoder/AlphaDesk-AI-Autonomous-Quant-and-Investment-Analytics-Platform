"""The envelope every Module 1 tool answers with.

Step 7B will put these tools in front of a language model. A model cannot read a CLI's exit
code, and it must not have to scan a warning list to discover that a number is missing. So
every tool returns the same small shape, and the most important field in it is `status`.

**Four statuses, and the line between them.** The rule is that `status` describes whether the
tool could do its job -- never what the data means:

* `ok`          -- the call ran and everything that was asked for is present.
* `partial`     -- the call ran and there is data, but something that was asked for was
                    withheld (unavailable metrics, an amendment blocking a comparison, several
                    candidate observations where one was expected). `reason` says what.
* `unavailable` -- the call ran correctly and the store holds nothing that answers it. This is
                    an *answer*, not a fault: "we hold no prices for that symbol" is a fact
                    about this database.
* `failed`      -- the call could not run at all: the database, the search index or the
                    embedding model was unreachable. Nothing is claimed about the data.

That line matters because the two are easy to confuse. `insufficient_coverage` is an
*analytical conclusion* -- it says the sample is too thin to conclude anything -- and it lives
inside the payload. A call that returns it is `ok` or `partial`, not a failure.

`unavailable` and `failed` carry a machine-readable `reason` and `data` is None, so a caller
can never mistake an error string for a result. `warnings` is always populated, including on
those, because the explanation of *why* there is no data is exactly what a reader needs there.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolStatus(StrEnum):
    """Whether the tool could do its job. Not a statement about the data's meaning."""

    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


# Reason codes shared by more than one tool. Each tool also has its own, declared where it is
# raised, because a code is only useful if it says something the reader could not have guessed.
UNKNOWN_COMPANY = "unknown_company"
DATABASE_UNAVAILABLE = "database_unavailable"


class ToolResult(BaseModel):
    """One tool call's answer.

    `data` is the tool's payload and is present only when there is something to report. Its
    shape is the tool's own business -- each tool documents it, and for the two that wrap an
    existing calculation it is that calculation's existing `as_dict()` rather than a second
    definition of the same fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    status: ToolStatus
    # A code such as "no_price_series_stored". Set whenever the status is not `ok`.
    reason: str | None = None
    symbol: str | None = None
    # The end date the question was asked about, in the market's timezone. None for tools whose
    # answer is about the present rather than a historical moment.
    as_of: date | None = None
    # The exclusive UTC instant the cutoff was applied at, so a reader can check the arithmetic
    # rather than trust it.
    information_cutoff: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    data: dict[str, Any] | None = None

    @property
    def carried_out(self) -> bool:
        """True when the tool ran, whether or not it found anything."""
        return self.status is not ToolStatus.FAILED

    def as_json(self) -> dict[str, Any]:
        """JSON-safe. Decimals became exact strings on the way in, not on the way out."""
        return self.model_dump(mode="json")


def unavailable(
    *,
    tool: str,
    reason: str,
    symbol: str | None = None,
    as_of: date | None = None,
    information_cutoff: datetime | None = None,
    warnings: tuple[str, ...] | list[str] = (),
) -> ToolResult:
    """The tool ran and the store holds nothing that answers the request."""
    return ToolResult(
        tool=tool,
        status=ToolStatus.UNAVAILABLE,
        reason=reason,
        symbol=symbol,
        as_of=as_of,
        information_cutoff=information_cutoff,
        warnings=list(warnings),
        data=None,
    )


def failed(
    *,
    tool: str,
    reason: str,
    message: str,
    symbol: str | None = None,
    as_of: date | None = None,
    information_cutoff: datetime | None = None,
    warnings: tuple[str, ...] | list[str] = (),
) -> ToolResult:
    """The tool could not run.

    `message` must already be sanitised: a short line naming what was unreachable, never a
    connection string, a query, or an exception dump. The full exception belongs in the log,
    which is where the caller of this function puts it.
    """
    return ToolResult(
        tool=tool,
        status=ToolStatus.FAILED,
        reason=reason,
        symbol=symbol,
        as_of=as_of,
        information_cutoff=information_cutoff,
        warnings=[*warnings, message],
        data=None,
    )


def database_unavailable(
    *,
    tool: str,
    error_name: str,
    symbol: str | None = None,
    as_of: date | None = None,
    information_cutoff: datetime | None = None,
    warnings: tuple[str, ...] | list[str] = (),
) -> ToolResult:
    """The database could not be read.

    Shared, because the three SQL-only tools would otherwise each spell the same sentence
    slightly differently. `error_name` is the exception's class name and nothing more: the
    message of a driver-level exception can carry a host, a user, or a fragment of the
    statement, and none of that belongs in a tool's answer.
    """
    return failed(
        tool=tool,
        reason=DATABASE_UNAVAILABLE,
        message=(
            "The stored data could not be read, so nothing is claimed about this request: "
            f"{error_name}."
        ),
        symbol=symbol,
        as_of=as_of,
        information_cutoff=information_cutoff,
        warnings=warnings,
    )


def merged_warnings(*groups: tuple[str, ...] | list[str]) -> list[str]:
    """Concatenate warning groups, dropping exact repeats but keeping order.

    The same sentence can arrive from two places -- a calculation's own limitations and a
    tool's standing caveats -- and printing it twice makes a result look padded.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for warning in group:
            if warning not in seen:
                seen.add(warning)
                merged.append(warning)
    return merged
