"""Tool B: what the company reported for one concept, in one period, per one filing.

The stored observations come from SEC Company Facts and were written by Step 5B. This tool is
a read over them with four rules that exist because the underlying data makes all four easy to
get wrong.

**One concept at a time, never a sum.** `Revenues` and
`RevenueFromContractWithCustomerExcludingAssessedTax` are different concepts with different
definitions. An issuer that reports one has not thereby reported the other, and a total that
added them would be neither. Because `metric` selects exactly one concept, no caller -- and no
model -- can combine them by accident.

**A period is a start and an end.** NVDA's `Revenues` carries both a quarter (2026-04-27 to
2026-07-26) and a year-to-date figure (2026-01-26 to 2026-07-26) ending on the same day, and
the same period also reappears as a comparative inside a later filing. All of them are real.
When a request leaves more than one standing, the tool returns them as labelled candidates and
reports `partial`; it does not choose, and it does not add.

**Availability is the acceptance timestamp, not the period.** A figure is only knowable once
the filing carrying it was accepted. When an observation's filing is not stored here, or its
acceptance time is unknown, that cannot be established -- so the observation is kept out of the
strict result and listed separately as excluded. An outer join is what makes that possible: an
inner join would drop such a row silently, which is the one outcome this tool exists to
prevent.

**Absence is reported as absence.** A concept with no stored observation returns
`unavailable`, never `0`. And it always says that this is a limit of what was ingested --
one 10-K, one 10-Q, and six mapped concepts -- rather than a statement that the company never
reported it.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import company_facts
from app.analysis import information_cutoff
from app.company_facts import CONCEPTS_BY_NAME, ConceptMapping
from app.models import Company, FinancialFact, SecFiling
from app.tools.results import (
    UNKNOWN_COMPANY,
    ToolResult,
    ToolStatus,
    database_unavailable,
    merged_warnings,
    unavailable,
)

logger = logging.getLogger(__name__)

TOOL_NAME = "company_financial_facts"

DESCRIPTION = (
    "Read one reported financial figure for one stored US-listed company: revenue, net "
    "income, total assets, total liabilities, or cash and cash equivalents, as the company's "
    "own SEC filings reported it. Use it when asked what a company reported for a named "
    "metric in a named period. Select one metric and, where possible, an exact period "
    "(period_start and period_end) or a filing accession; the two revenue concepts are "
    "different figures and are never combined or summed. Each returned observation carries "
    "its exact taxonomy concept, unit, period, the filing accession and form that reported it, "
    "the filing's source URL, and the acceptance timestamp the information cutoff was applied "
    "to. Only observations whose filing was accepted before the cutoff are returned; those "
    "excluded for availability are listed separately with a reason. Several matching "
    "observations are returned as labelled candidates with status 'partial' rather than one "
    "being picked or added. A metric with no stored observation returns 'unavailable', never "
    "zero, and that always means 'not in this stored sample' -- never 'the company did not "
    "report it'. No ratios, growth rates, or accounting inference are produced."
)

# The metric names, which are the mapping names in `app.company_facts`. Declared as a Literal
# so the JSON schema a model sees lists the allowed values. A test asserts this set equals
# `CONCEPTS_BY_NAME`, so the two cannot drift apart.
MetricName = Literal[
    "revenue",
    "revenue_contract_with_customer",
    "net_income",
    "total_assets",
    "total_liabilities",
    "cash_and_cash_equivalents",
]

# How many observations each of the two lists may carry. A concept with a long history would
# otherwise fill a model's context. The totals are always reported alongside.
DEFAULT_CANDIDATE_LIMIT = 20
MAX_CANDIDATE_LIMIT = 50

# How many stored period ends an empty result lists before it stops enumerating them.
_PERIOD_ENDS_SHOWN = 8

# Why an observation did not enter the strict result.
EXCLUDED_ACCEPTED_AFTER_CUTOFF = "accepted_after_cutoff"
EXCLUDED_FILING_NOT_STORED = "filing_not_stored"
EXCLUDED_ACCEPTANCE_UNKNOWN = "acceptance_time_unknown"
EXCLUDED_ACCESSION_MISSING = "accession_missing"

# Why the request could not be answered.
REASON_NO_STORED_FACTS = "no_stored_financial_facts"
REASON_CONCEPT_NOT_IN_SAMPLE = "concept_not_in_stored_sample"
REASON_NO_MATCHING_OBSERVATION = "no_observation_matches_selection"
REASON_NONE_AVAILABLE = "no_observation_available_before_cutoff"

# How many observations the selection settled on.
SELECTION_SINGLE = "single"
SELECTION_MULTIPLE = "multiple_candidates"

SAMPLE_LIMITATION = (
    "This is a bounded sample. Only the latest original 10-K and 10-Q accepted before the "
    "ingestion cutoff were stored, and only a small set of concepts is mapped. 'No stored "
    "observation' therefore means it is not in this stored sample -- it is not a statement "
    "that the company never reported it."
)


class FinancialFactsRequest(BaseModel):
    """Which figure, for which period, knowable as at which date."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)
    metric: MetricName
    as_of: date
    # Both optional: they narrow an otherwise ambiguous selection. Giving only `period_end` is
    # legitimate and often still ambiguous, which is what the candidate list is for.
    period_start: date | None = None
    period_end: date | None = None
    # The filing that reported it, when the question is about one document.
    accession_number: str | None = Field(default=None, min_length=1, max_length=25)
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    candidate_limit: int = Field(
        default=DEFAULT_CANDIDATE_LIMIT, ge=1, le=MAX_CANDIDATE_LIMIT
    )

    @model_validator(mode="after")
    def _selection_is_answerable(self) -> "FinancialFactsRequest":
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_start > self.period_end
        ):
            raise ValueError(
                f"period_start {self.period_start.isoformat()} is after period_end "
                f"{self.period_end.isoformat()}"
            )

        mapping = CONCEPTS_BY_NAME[self.metric]
        if mapping.kind == "instant" and self.period_start is not None:
            # Not merely unlikely: `company_facts` refuses to store an instant fact that
            # carries a start, so such a request could only ever return nothing. Saying so is
            # kinder than an empty result the caller has to interpret.
            raise ValueError(
                f"{self.metric} ({mapping.concept}) is a balance-sheet figure with no period "
                "start; select it by period_end, not period_start"
            )
        return self


class FilingAvailability(BaseModel):
    """What the filing that reported an observation says about when it became public.

    `acceptance_datetime` is a `datetime`, not a pre-formatted string, so that the cutoff this
    observation was judged against and the timestamp it was judged on are rendered in exactly
    the same way by the serializer. Formatting one here by hand and letting Pydantic format
    the other is how two timestamps in one result end up looking like different kinds of value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    acceptance_datetime: datetime | None
    form_type: str
    filing_date: date
    report_date: date | None


class FactObservationOut(BaseModel):
    """One reported value, with everything needed to cite it back to its filing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    taxonomy: str
    concept: str
    metric: str
    unit: str
    # A Decimal, so the JSON form is an exact string rather than a binary float.
    value: Decimal
    period_start: date | None
    period_end: date
    instant: bool
    accession_number: str | None
    form: str | None
    filed_date: date | None
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None
    source_reference: str | None
    availability: FilingAvailability | None


class ExcludedObservation(BaseModel):
    """An observation the selection matched but the strict result could not include."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    concept: str
    unit: str
    value: Decimal
    period_start: date | None
    period_end: date
    accession_number: str | None


class FinancialFactsData(BaseModel):
    """The payload. Every count is given, so a trimmed list is never mistaken for the whole."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str
    concept: str
    metric_kind: str
    # Why this concept, and what its alternative is. Carried from the mapping so a reader does
    # not have to go looking for the reason it was chosen.
    metric_note: str
    selection: str
    selection_criteria: dict[str, Any]
    observations: list[FactObservationOut]
    observations_total: int
    observations_returned: int
    excluded: list[ExcludedObservation]
    excluded_total: int
    # Everything stored for this concept, before the selection and the cutoff were applied.
    stored_observation_count: int
    stored_units: list[str]
    limitations: list[str]


@dataclass(frozen=True)
class StoredObservation:
    """One stored fact joined to the filing that reported it.

    `filing_found` is separate from `acceptance_datetime is None` on purpose: a filing that is
    stored but silent about its acceptance time is a different problem from a filing that is
    not stored here at all, and the two are reported with different reasons.
    """

    taxonomy: str
    concept: str
    unit: str
    value: Decimal
    period_start: date | None
    period_end: date
    accession_number: str | None
    form: str | None
    filed_date: date | None
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None
    filing_found: bool
    acceptance_datetime: datetime | None
    form_type: str | None
    filing_date: date | None
    report_date: date | None
    source_document_url: str | None


def run(session: Session, request: FinancialFactsRequest) -> ToolResult:
    """Read the stored observations and classify them. Reads only; writes nothing."""
    mapping = CONCEPTS_BY_NAME[request.metric]
    cutoff = information_cutoff(request.as_of)
    limitations = [SAMPLE_LIMITATION, mapping.note]

    def answer(status: ToolStatus, reason: str | None, warnings: Sequence[str], data=None):
        return ToolResult(
            tool=TOOL_NAME,
            status=status,
            reason=reason,
            symbol=request.symbol,
            as_of=request.as_of,
            information_cutoff=cutoff,
            warnings=merged_warnings(warnings),
            data=data,
        )

    try:
        company = _company(session, request.symbol)
        if company is None:
            stored = _stored_tickers(session)
            return answer(
                ToolStatus.UNAVAILABLE,
                UNKNOWN_COMPANY,
                [
                    f"No company is stored under {request.symbol!r}. Stored tickers: "
                    f"{', '.join(stored) if stored else 'none'}. Ingest a company before "
                    "asking about its reported figures."
                ],
            )

        stored_for_company = _stored_fact_count(session, company.id)
        stored_for_concept = _observations(session, company.id, mapping.concept)
    except SQLAlchemyError as exc:
        logger.exception("financial facts could not read the stored data")
        return database_unavailable(
            tool=TOOL_NAME,
            error_name=type(exc).__name__,
            symbol=request.symbol,
            as_of=request.as_of,
            information_cutoff=cutoff,
        )

    if stored_for_company == 0:
        return answer(
            ToolStatus.UNAVAILABLE,
            REASON_NO_STORED_FACTS,
            [
                f"No financial observation at all is stored for {request.symbol}. Step 5B "
                "stores them: `python -m app.ingest_company_context --symbol "
                f"{request.symbol}`.",
                *limitations,
            ],
        )

    if not stored_for_concept:
        return answer(
            ToolStatus.UNAVAILABLE,
            REASON_CONCEPT_NOT_IN_SAMPLE,
            [
                f"No stored observation reports {request.metric} ({mapping.concept}) for "
                f"{request.symbol}. The value is absent from this sample, not zero.",
                *limitations,
            ],
        )

    matched = [row for row in stored_for_concept if _matches(row, request)]
    if not matched:
        return answer(
            ToolStatus.UNAVAILABLE,
            REASON_NO_MATCHING_OBSERVATION,
            [
                f"{len(stored_for_concept)} stored observation(s) of {request.metric} exist "
                "for this company and none matches the requested period, unit or filing. "
                f"Stored units: {', '.join(sorted({row.unit for row in stored_for_concept}))}. "
                f"Stored period ends: {_period_summary(stored_for_concept)}.",
                "Nothing was picked on your behalf and nothing was summed.",
                *limitations,
            ],
        )

    available, excluded = _classify(matched, cutoff)

    if not available:
        return answer(
            ToolStatus.UNAVAILABLE,
            REASON_NONE_AVAILABLE,
            [
                f"{len(matched)} observation(s) match, and none was accepted before the "
                f"information cutoff of {cutoff.isoformat()}, so none was knowable on "
                f"{request.as_of.isoformat()}. They are excluded rather than shown.",
                *limitations,
            ],
        )

    data = _payload(
        request=request,
        mapping=mapping,
        available=available,
        excluded=excluded,
        stored_count=len(stored_for_concept),
        stored_units=sorted({row.unit for row in stored_for_concept}),
        limitations=limitations,
    )

    warnings = list(limitations)
    status = ToolStatus.OK
    reason: str | None = None

    if len(available) > 1:
        status = ToolStatus.PARTIAL
        reason = SELECTION_MULTIPLE
        warnings.append(
            f"{len(available)} observations match and they are not interchangeable: they are "
            "different periods or different filings. They are listed as candidates, and were "
            "neither picked between nor added together. Narrow the request with period_start, "
            "period_end or accession_number."
        )
    if excluded:
        status = ToolStatus.PARTIAL
        reason = reason or _most_common_exclusion(excluded)
        warnings.append(
            f"{len(excluded)} matched observation(s) are excluded from the strict result: "
            "either the filing postdates the cutoff or when it became public cannot be "
            "established from the stored records. They are listed with a reason rather than "
            "dropped."
        )

    return answer(status, reason, warnings, data.model_dump(mode="json"))


# --- classification -----------------------------------------------------------------------


def _matches(row: StoredObservation, request: FinancialFactsRequest) -> bool:
    """Whether one stored observation satisfies the request's explicit filters.

    Every filter is optional and an absent one constrains nothing. Only the filters given are
    applied, so a bare `metric` returns the whole stored history of that concept as candidates
    rather than inventing a "latest" that the caller never asked for.
    """
    if request.period_start is not None and row.period_start != request.period_start:
        return False
    if request.period_end is not None and row.period_end != request.period_end:
        return False
    if (
        request.accession_number is not None
        and row.accession_number != request.accession_number
    ):
        return False
    if request.unit is not None and row.unit != request.unit:
        return False
    return True


def _classify(
    matched: Sequence[StoredObservation], cutoff: datetime
) -> tuple[list[StoredObservation], list[tuple[str, StoredObservation]]]:
    """Split matched observations into those knowable at the cutoff and those that are not.

    The order of the checks decides which reason a row is filed under when more than one could
    apply, and it runs from the most fundamental -- is there even a filing to check -- to the
    most situational.
    """
    available: list[StoredObservation] = []
    excluded: list[tuple[str, StoredObservation]] = []

    for row in matched:
        if row.accession_number is None:
            excluded.append((EXCLUDED_ACCESSION_MISSING, row))
        elif not row.filing_found:
            excluded.append((EXCLUDED_FILING_NOT_STORED, row))
        elif row.acceptance_datetime is None:
            # Stored, but it says nothing about when it was accepted, so the cutoff cannot be
            # applied. Unknown is not the same as available.
            excluded.append((EXCLUDED_ACCEPTANCE_UNKNOWN, row))
        elif row.acceptance_datetime >= cutoff:
            excluded.append((EXCLUDED_ACCEPTED_AFTER_CUTOFF, row))
        else:
            available.append(row)

    # Most recent first, so a trimmed candidate list keeps the periods a reader wants.
    available.sort(key=_recency, reverse=True)
    excluded.sort(key=lambda item: _recency(item[1]), reverse=True)
    return available, excluded


def _recency(row: StoredObservation) -> tuple:
    return (row.period_end, row.period_start or date.min, row.accession_number or "")


def _most_common_exclusion(excluded: Sequence[tuple[str, StoredObservation]]) -> str:
    """The reason code for a partial result, named after the most common exclusion.

    Ties break alphabetically rather than by dictionary order, so the same data always reports
    the same code.
    """
    counts: dict[str, int] = {}
    for reason, _ in excluded:
        counts[reason] = counts.get(reason, 0) + 1
    return max(sorted(counts), key=lambda reason: counts[reason])


# --- payload ------------------------------------------------------------------------------


def _payload(
    *,
    request: FinancialFactsRequest,
    mapping: ConceptMapping,
    available: Sequence[StoredObservation],
    excluded: Sequence[tuple[str, StoredObservation]],
    stored_count: int,
    stored_units: list[str],
    limitations: list[str],
) -> FinancialFactsData:
    shown = list(available[: request.candidate_limit])
    shown_excluded = list(excluded[: request.candidate_limit])

    criteria: dict[str, Any] = {"metric": request.metric}
    for name in ("period_start", "period_end", "accession_number", "unit"):
        value = getattr(request, name)
        if value is not None:
            criteria[name] = value.isoformat() if isinstance(value, date) else value

    return FinancialFactsData(
        metric=request.metric,
        concept=mapping.concept,
        metric_kind=mapping.kind,
        metric_note=mapping.note,
        selection=SELECTION_SINGLE if len(available) == 1 else SELECTION_MULTIPLE,
        selection_criteria=criteria,
        observations=[_observation(row, request.metric) for row in shown],
        observations_total=len(available),
        observations_returned=len(shown),
        excluded=[_excluded(reason, row) for reason, row in shown_excluded],
        excluded_total=len(excluded),
        stored_observation_count=stored_count,
        stored_units=stored_units,
        limitations=limitations,
    )


def _observation(row: StoredObservation, metric: str) -> FactObservationOut:
    return FactObservationOut(
        taxonomy=row.taxonomy,
        concept=row.concept,
        metric=metric,
        unit=row.unit,
        value=row.value,
        period_start=row.period_start,
        period_end=row.period_end,
        instant=row.period_start is None,
        accession_number=row.accession_number,
        form=row.form,
        filed_date=row.filed_date,
        fiscal_year=row.fiscal_year,
        fiscal_period=row.fiscal_period,
        frame=row.frame,
        source_reference=row.source_document_url,
        availability=(
            None
            if not row.filing_found or row.form_type is None or row.filing_date is None
            else FilingAvailability(
                acceptance_datetime=row.acceptance_datetime,
                form_type=row.form_type,
                filing_date=row.filing_date,
                report_date=row.report_date,
            )
        ),
    )


def _excluded(reason: str, row: StoredObservation) -> ExcludedObservation:
    return ExcludedObservation(
        reason=reason,
        concept=row.concept,
        unit=row.unit,
        value=row.value,
        period_start=row.period_start,
        period_end=row.period_end,
        accession_number=row.accession_number,
    )


# --- reads --------------------------------------------------------------------------------


def _observations(
    session: Session, company_id: int, concept: str
) -> list[StoredObservation]:
    """Every stored observation of one concept, with its filing's availability attached.

    The concept is bounded by the mapping table, so this returns a handful of rows rather than
    a history: the selection filters are applied in Python afterwards, which is what lets the
    payload report how many observations were stored, how many matched, and why each excluded
    one was left out.
    """
    rows = session.execute(
        select(
            FinancialFact.taxonomy,
            FinancialFact.concept,
            FinancialFact.unit,
            FinancialFact.value,
            FinancialFact.period_start,
            FinancialFact.period_end,
            FinancialFact.accession_number,
            FinancialFact.form,
            FinancialFact.filed_date,
            FinancialFact.fiscal_year,
            FinancialFact.fiscal_period,
            FinancialFact.frame,
            SecFiling.id,
            SecFiling.acceptance_datetime,
            SecFiling.form_type,
            SecFiling.filing_date,
            SecFiling.report_date,
            SecFiling.source_document_url,
        )
        .select_from(FinancialFact)
        .outerjoin(
            SecFiling,
            and_(
                SecFiling.accession_number == FinancialFact.accession_number,
                SecFiling.company_id == FinancialFact.company_id,
            ),
        )
        .where(
            FinancialFact.company_id == company_id,
            FinancialFact.taxonomy == company_facts.TAXONOMY,
            FinancialFact.concept == concept,
        )
        .order_by(
            FinancialFact.period_end.desc(),
            FinancialFact.period_start.desc().nullslast(),
            FinancialFact.accession_number,
        )
    ).all()

    return [
        StoredObservation(
            taxonomy=row[0],
            concept=row[1],
            unit=row[2],
            value=row[3],
            period_start=row[4],
            period_end=row[5],
            accession_number=row[6],
            form=row[7],
            filed_date=row[8],
            fiscal_year=row[9],
            fiscal_period=row[10],
            frame=row[11],
            filing_found=row[12] is not None,
            acceptance_datetime=row[13],
            form_type=row[14],
            filing_date=row[15],
            report_date=row[16],
            source_document_url=row[17],
        )
        for row in rows
    ]


def _stored_fact_count(session: Session, company_id: int) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(FinancialFact)
            .where(FinancialFact.company_id == company_id)
        )
        or 0
    )


def _company(session: Session, symbol: str) -> Company | None:
    return session.scalar(
        select(Company).where(func.upper(Company.ticker) == symbol.strip().upper())
    )


def _stored_tickers(session: Session) -> tuple[str, ...]:
    return tuple(session.scalars(select(Company.ticker).order_by(Company.ticker)))


def _period_summary(rows: Sequence[StoredObservation]) -> str:
    """The stored period ends, so an empty answer says what is there instead.

    Truncated with a count rather than silently: a reader needs to know the list is short.
    """
    ends = sorted({row.period_end.isoformat() for row in rows})
    shown = ends[:_PERIOD_ENDS_SHOWN]
    suffix = f" and {len(ends) - len(shown)} more" if len(ends) > len(shown) else ""
    return ", ".join(shown) + suffix


__all__ = [
    "DESCRIPTION",
    "FinancialFactsData",
    "FinancialFactsRequest",
    "MetricName",
    "TOOL_NAME",
    "run",
]
