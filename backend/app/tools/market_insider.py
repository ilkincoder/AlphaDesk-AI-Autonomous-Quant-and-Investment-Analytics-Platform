"""Tool A: how the price moved, and what insiders reported doing, over one date window.

This is a thin, typed wrapper around work that already exists. The reads are
`app.analyze_insiders.run`'s, the rules are `app.analysis`'s, and neither is re-implemented
here -- the tool's whole job is to state its inputs precisely, decide what kind of answer it
is giving, and make the payload safe to hand to a language model.

**The window is the caller's, never the data's.** The command line will happily default a
missing date to the stored range. This tool will not: both dates are required, and nothing is
widened to fit whatever happens to be stored. If the window lies outside the stored prices the
result says so through `partial` and the calculation's own reasons, rather than quietly
answering a different question than the one asked.

**The payload is the existing result, not a second copy of it.** `AnalysisResult.as_dict()` is
already documented, already tested, and already serializes every Decimal as an exact string.
Re-declaring those twenty-odd fields as Pydantic models would create a second definition of one
contract, and the two would eventually disagree. Instead the tool adds exactly two keys:

* `included_transactions` -- the per-row listing is capped, with the full count alongside it.
  The counts and money totals are untouched, because those come from the calculation.
* `stored_price_availability` -- the first and last price date this database actually holds,
  so "the window you asked for" and "the window we hold" are both visible.

Nothing here adds a signal, a score, or a claim about what any of it means.
"""

import logging
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import analysis
from app.analyze_insiders import run as analyse_insiders
from app.models import Company, DailyPrice
from app.tools.results import (
    UNKNOWN_COMPANY,
    ToolResult,
    ToolStatus,
    database_unavailable,
    merged_warnings,
    unavailable,
)

logger = logging.getLogger(__name__)

TOOL_NAME = "market_insider_analysis"

DESCRIPTION = (
    "Analyse one stored US-listed company's daily price move and its SEC Form 4 insider "
    "transactions over an explicit date window. Use it when asked how a stock moved, or "
    "whether insiders were buying or selling, in a defined period. Returns the requested and "
    "actual price dates, the observation count, provider and adjustment mode, first and last "
    "close and the calculated return, the eligible purchases and sales with their reported "
    "values, the excluded rows and why, a sample comparison, an overall conclusion, and the "
    "coverage and amendment warnings. It reports what the stored records show and recommends "
    "nothing: there is no signal, no confidence score, and no profitability claim. Both dates "
    "are required and the window is never widened to fit the stored data. A price comparison "
    "needs at least two stored closes inside the window; a result may be 'partial' because a "
    "metric was withheld, and 'insufficient_coverage' in the payload means the sample is too "
    "thin to conclude anything."
)

# How many per-row transactions the payload lists. A company with a year of Form 4s could
# otherwise put thousands of rows in a model's context. The counts and the money totals are
# always complete; only this listing is trimmed, and `included_transactions` says by how much.
MAX_INCLUDED_ROWS = 50

# The reason reported when a partial result's own calculation named no specific cause.
REASON_METRICS_WITHHELD = "metrics_withheld"


class MarketInsiderRequest(BaseModel):
    """The window to analyse.

    `end_date` is not just a filter: it is what the information cutoff is derived from, through
    `analysis.information_cutoff`, so a filing accepted after that date cannot influence the
    answer.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)
    start_date: date
    end_date: date

    @field_validator("symbol")
    @classmethod
    def _normalise_symbol(cls, value: str) -> str:
        # Stored tickers are upper case, and `analysis` compares against them exactly. Doing
        # this here means a caller writing "nvda" gets an answer rather than "no company".
        normalised = value.strip().upper()
        if not normalised:
            raise ValueError("symbol must not be blank")
        return normalised

    @model_validator(mode="after")
    def _window_is_ordered(self) -> "MarketInsiderRequest":
        if self.start_date > self.end_date:
            raise ValueError(
                f"start_date {self.start_date.isoformat()} is after end_date "
                f"{self.end_date.isoformat()}"
            )
        return self


def run(session: Session, request: MarketInsiderRequest) -> ToolResult:
    """Read the stored records and analyse them. Reads only; writes nothing."""
    try:
        result = analyse_insiders(
            request.symbol, request.start_date, request.end_date, session=session
        )
        availability = _stored_price_availability(session, request.symbol)
    except SQLAlchemyError as exc:
        # An expected operational failure, reported as such. A coding mistake is a different
        # thing and is left to raise rather than being relabelled "no data".
        logger.exception("market insider analysis could not read the stored data")
        return database_unavailable(
            tool=TOOL_NAME,
            error_name=type(exc).__name__,
            symbol=request.symbol,
            as_of=request.end_date,
        )

    status, reason = _status(result)
    warnings = merged_warnings(result.limitations)

    if status is ToolStatus.UNAVAILABLE:
        return unavailable(
            tool=TOOL_NAME,
            reason=reason,
            symbol=request.symbol,
            as_of=request.end_date,
            information_cutoff=result.information_cutoff,
            warnings=warnings,
        )

    data, truncation_warning = _payload(result, availability)
    if truncation_warning is not None:
        warnings = merged_warnings(warnings, [truncation_warning])

    return ToolResult(
        tool=TOOL_NAME,
        status=status,
        reason=reason,
        symbol=request.symbol,
        as_of=request.end_date,
        information_cutoff=result.information_cutoff,
        warnings=warnings,
        data=data,
    )


def _status(result: analysis.AnalysisResult) -> tuple[ToolStatus, str | None]:
    """Whether the analysis could run, and what it withheld if it did.

    `insufficient_coverage` is deliberately not consulted. It is this project's standing
    conclusion about a three-filing sample, not a statement that the call failed, and treating
    it as one would make every honest answer look like an error.
    """
    if result.unavailable_reason is not None:
        # `analysis` spells the no-company case out at length. Every tool reports it as
        # `unknown_company`, so the vocabulary is the same whichever tool was called; the
        # explanation itself is already carried in the warnings.
        reason = (
            UNKNOWN_COMPANY
            if result.unavailable_reason == analysis.UNAVAILABLE_NO_COMPANY
            else result.unavailable_reason
        )
        return ToolStatus.UNAVAILABLE, reason

    withheld = (
        result.price_unavailable_reason is not None
        or result.sample_comparison == analysis.UNAVAILABLE
        or not result.values_complete
        or result.amendment_uncertainty
    )
    if withheld:
        # Every path that withholds something already records why, so this fallback should be
        # unreachable. Stating it anyway keeps the promise that a non-ok status always carries
        # a reason, rather than leaving a caller to handle `partial` with `reason: null`.
        return (
            ToolStatus.PARTIAL,
            result.sample_comparison_reason
            or result.price_unavailable_reason
            or REASON_METRICS_WITHHELD,
        )
    return ToolStatus.OK, None


def _payload(
    result: analysis.AnalysisResult,
    availability: dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None]:
    """The existing result, plus the two tool-level keys, plus a truncation warning."""
    data = result.as_dict()

    included = data["transactions"]["included"]
    total = len(included)
    kept = included[:MAX_INCLUDED_ROWS]
    data["transactions"]["included"] = kept
    data["included_transactions"] = {
        "returned": len(kept),
        "total": total,
        "omitted": total - len(kept),
        "note": None,
    }
    data["stored_price_availability"] = availability

    if total <= len(kept):
        return data, None

    # Said in the warning list as well as in the payload. A reader who only looks at warnings
    # should still learn that the listing is short.
    note = (
        f"{total - len(kept)} eligible transaction(s) are not listed individually. This cap "
        f"is {MAX_INCLUDED_ROWS} rows. The counts and the reported values above cover every "
        "eligible transaction, not only the ones listed."
    )
    data["included_transactions"]["note"] = note
    return data, note


def _stored_price_availability(
    session: Session, symbol: str
) -> dict[str, Any] | None:
    """The first and last price date this database holds for the company.

    Reported separately from the dates the analysis used, because the two answer different
    questions. `prices.first_date` is the first close *inside the requested window*; this is
    the first close held at all. When a window falls outside the stored range, that difference
    is the explanation.
    """
    row = session.execute(
        select(
            func.min(DailyPrice.trading_date),
            func.max(DailyPrice.trading_date),
            func.count(),
        )
        .select_from(DailyPrice)
        .join(Company, Company.id == DailyPrice.company_id)
        .where(func.upper(Company.ticker) == symbol)
    ).one()

    first, last, count = row
    if count == 0:
        return None

    return {
        "first_date": first.isoformat(),
        "last_date": last.isoformat(),
        "observation_count": count,
        "note": (
            "Every stored price for this company, across every provider and adjustment "
            "basis. The analysis itself uses only the dates inside the requested window."
        ),
    }


__all__ = [
    "DESCRIPTION",
    "MAX_INCLUDED_ROWS",
    "MarketInsiderRequest",
    "TOOL_NAME",
    "run",
]
