"""Price and insider-activity analysis. Pure: no database, no HTTP, no settings.

Reads plain values in and returns a typed result out, so every rule in it can be exercised
from a fixture. The command that reads PostgreSQL lives in `app.analyze_insiders`.

The module exists to answer four questions, and to be explicit about which of them it can:

1. How did the price move over a defined period?
2. Which stored transactions are purchases and which are sales?
3. Do those two point in opposite directions?
4. **Is the evidence good enough to say anything at all?**

The fourth is not a footnote. A comparison can be perfectly calculable from a sample that is
far too thin to support a conclusion, and the result carries those as two separate fields
precisely so they cannot be confused.

Nothing here recommends anything. There is no confidence score, no price target, and no
"strong bullish/bearish" label, because none of those follow from what these records say.
"""

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

# The market whose calendar decides when a day ends. A filing accepted at 21:00 UTC is
# still "that day" in New York, and the cutoff has to agree with the exchange, not with UTC.
MARKET_TIMEZONE = ZoneInfo("America/New_York")

# Form 4 transaction codes: P and S are the only two that describe a purchase or a sale.
PURCHASE_CODE = "P"
SALE_CODE = "S"
ACQUIRED = "A"
DISPOSED = "D"

NON_DERIVATIVE_TABLE = "nonDerivativeTable"

# The only forms that carry insider transactions. Stated explicitly rather than relied on:
# today nothing else produces `insider_transactions` rows, but disclosure ingestion now puts
# 10-K, 10-Q and 8-K rows in `sec_filings`, and an analysis that joined them would be one
# change away from counting a prospectus as an insider trade.
OWNERSHIP_FORMS = ("4", "4/A")

# The security classes this analysis understands, normalised. NVDA's stored filings use both
# "Common Stock" and "Common" for the same class, so a mapping that knew only one would drop
# real rows without saying so. Anything outside this set is excluded *and reported* rather
# than folded into a total, because two share classes are not one number.
COMMON_STOCK_TITLES = frozenset({"common stock", "common"})

# --- outcomes ---------------------------------------------------------------------------

PRICE_UP_NET_SELLING = "price_up_net_selling"
PRICE_DOWN_NET_BUYING = "price_down_net_buying"
SAME_DIRECTION = "same_direction"
NO_DIRECTIONAL_DIFFERENCE = "no_directional_difference"
NO_ELIGIBLE_TRANSACTIONS = "no_eligible_transactions"
UNAVAILABLE = "unavailable"

INSUFFICIENT_COVERAGE = "insufficient_coverage"

COVERAGE_PARTIAL = "partial"
COVERAGE_UNKNOWN = "unknown"

# Why nothing could be analysed at all, as opposed to why one metric is missing.
UNAVAILABLE_NO_COMPANY = "no_company_stored_under_that_symbol"

# Why a price metric could not be produced.
PRICE_REASON_NO_SERIES = "no_price_series_stored"
PRICE_REASON_AMBIGUOUS_SERIES = "more_than_one_price_series_stored"
PRICE_REASON_TOO_FEW_DATES = "fewer_than_two_price_dates_in_range"

# Why the sample comparison was withheld.
COMPARISON_REASON_AMENDMENTS = "amendment_uncertainty"
COMPARISON_REASON_NO_ELIGIBLE = "no_eligible_transactions"
COMPARISON_REASON_PRICE = "price_metrics_unavailable"
COMPARISON_REASON_INCOMPLETE_VALUES = "some_eligible_values_unavailable"

# Why a row was left out of the purchase/sale totals.
EXCLUDED_DERIVATIVE = "derivative_security"
EXCLUDED_CODE = "code_not_purchase_or_sale"
EXCLUDED_SECURITY_CLASS = "security_class_not_supported"
EXCLUDED_INCONSISTENT = "inconsistent_code_direction"
EXCLUDED_OUTSIDE_RANGE = "transaction_date_outside_price_range"
EXCLUDED_AFTER_CUTOFF = "filed_after_cutoff"
EXCLUDED_ACCEPTANCE_UNKNOWN = "acceptance_time_unknown"

# Why a particular row's reported value could not be computed.
VALUE_SHARES_UNUSABLE = "shares_missing_or_not_positive"
VALUE_PRICE_UNUSABLE = "price_missing_or_zero"

# How many identities are kept per exclusion reason before the list is truncated. The count
# is always exact; only the exemplars are capped.
_EXCLUSION_IDENTITIES_SHOWN = 10

# Decimals are serialised exactly. This is the only rounding anywhere, and it exists for
# reading -- it is never used to decide a sign or a comparison.
DISPLAY_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class PriceSeriesSelection:
    """The one series being analysed. Series are never combined."""

    provider: str
    adjustment_basis: str
    provider_adjust_mode: str


@dataclass(frozen=True)
class PriceObservation:
    trading_date: date
    close: Decimal


@dataclass(frozen=True)
class TransactionRecord:
    """One stored transaction, flattened.

    Owners are deliberately absent. They attach to the filing, so a join would multiply
    every transaction by the number of owners on it -- exactly the fan-out Step 1 warned
    about. An owner can be looked up from the accession; it is not needed to count a trade.
    """

    accession_number: str
    source_table: str
    row_position: int
    security_title: str
    transaction_date: date
    transaction_code: str
    acquired_disposed: str
    shares: Decimal | None
    price_per_share: Decimal | None
    is_derivative: bool
    acceptance_datetime: datetime | None
    # Filing-level, so it describes the filing and not this row.
    rule_10b5_1: bool | None
    is_amendment: bool
    source_document_url: str

    @property
    def identity(self) -> str:
        return f"{self.accession_number}:{self.source_table}[{self.row_position}]"


@dataclass(frozen=True)
class CoverageInput:
    """What the ingestion records say about how much was looked at."""

    ingestion_run_count: int
    latest_run_started_at: datetime | None
    latest_requested_filings: int | None
    latest_stored_filings: int | None
    discovery_scope: str | None


@dataclass(frozen=True)
class IncludedTransaction:
    accession_number: str
    source_table: str
    row_position: int
    transaction_date: date
    transaction_code: str
    acquired_disposed: str
    security_title: str
    shares: Decimal
    price_per_share: Decimal | None
    # None when the row cannot be valued. The stored record is untouched; only this number
    # is withheld.
    reported_value: Decimal | None
    value_unavailable_reason: str | None
    rule_10b5_1: bool | None
    source_document_url: str

    @property
    def is_purchase(self) -> bool:
        return self.transaction_code == PURCHASE_CODE


@dataclass(frozen=True)
class Exclusion:
    reason: str
    count: int
    identities: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    status: str
    ingestion_run_count: int
    latest_run_started_at: datetime | None
    latest_requested_filings: int | None
    latest_stored_filings: int | None
    discovery_scope: str | None


@dataclass(frozen=True)
class AnalysisResult:
    symbol: str
    company_cik: str | None
    company_name: str | None

    requested_start: date
    requested_end: date

    price_series: PriceSeriesSelection | None
    price_first_date: date | None
    price_last_date: date | None
    price_observation_count: int
    first_close: Decimal | None
    last_close: Decimal | None
    price_change_percent: Decimal | None
    price_unavailable_reason: str | None

    information_cutoff: datetime

    purchase_count: int
    sale_count: int
    purchase_value_known: Decimal
    sale_value_known: Decimal
    purchase_rows_without_value: int
    sale_rows_without_value: int
    values_complete: bool
    net_reported_value: Decimal | None

    sample_comparison: str
    sample_comparison_reason: str | None
    overall_conclusion: str

    included: tuple[IncludedTransaction, ...]
    exclusions: tuple[Exclusion, ...]
    amendment_accessions: tuple[str, ...]
    amendment_uncertainty: bool

    coverage: Coverage
    limitations: tuple[str, ...]
    methodology: tuple[str, ...]
    # Set only when the analysis could not run at all -- no such company, no prices stored.
    # A metric-level gap uses `price_unavailable_reason` instead, so the two are not
    # confused: one means "nothing here", the other means "this number is missing".
    unavailable_reason: str | None = None

    def as_dict(self) -> dict:
        """JSON-safe. Every Decimal is an exact string; `display` holds the rounded view."""
        return {
            "symbol": self.symbol,
            "company": {"cik": self.company_cik, "name": self.company_name},
            "period": {
                "requested_start": _iso(self.requested_start),
                "requested_end": _iso(self.requested_end),
                "information_cutoff": self.information_cutoff.isoformat(),
            },
            "prices": {
                "series": (
                    None
                    if self.price_series is None
                    else {
                        "provider": self.price_series.provider,
                        "adjustment_basis": self.price_series.adjustment_basis,
                        "provider_adjust_mode": self.price_series.provider_adjust_mode,
                    }
                ),
                "first_date": _iso(self.price_first_date),
                "last_date": _iso(self.price_last_date),
                "observation_count": self.price_observation_count,
                "first_close": _dec(self.first_close),
                "last_close": _dec(self.last_close),
                "change_percent": _dec(self.price_change_percent),
                "unavailable_reason": self.price_unavailable_reason,
            },
            "transactions": {
                "purchase_count": self.purchase_count,
                "sale_count": self.sale_count,
                "purchase_value_known": _dec(self.purchase_value_known),
                "sale_value_known": _dec(self.sale_value_known),
                "purchase_rows_without_value": self.purchase_rows_without_value,
                "sale_rows_without_value": self.sale_rows_without_value,
                "values_complete": self.values_complete,
                "net_reported_value": _dec(self.net_reported_value),
                "included": [
                    {
                        "accession_number": row.accession_number,
                        "source_table": row.source_table,
                        "row_position": row.row_position,
                        "transaction_date": _iso(row.transaction_date),
                        "transaction_code": row.transaction_code,
                        "acquired_disposed": row.acquired_disposed,
                        "security_title": row.security_title,
                        "shares": _dec(row.shares),
                        "price_per_share": _dec(row.price_per_share),
                        "reported_value": _dec(row.reported_value),
                        "value_unavailable_reason": row.value_unavailable_reason,
                        "rule_10b5_1": row.rule_10b5_1,
                        "source_document_url": row.source_document_url,
                    }
                    for row in self.included
                ],
            },
            "exclusions": [
                {
                    "reason": exclusion.reason,
                    "count": exclusion.count,
                    "identities": list(exclusion.identities),
                }
                for exclusion in self.exclusions
            ],
            "amendments": {
                "uncertainty": self.amendment_uncertainty,
                "accessions": list(self.amendment_accessions),
            },
            "sample_comparison": self.sample_comparison,
            "sample_comparison_reason": self.sample_comparison_reason,
            "overall_conclusion": self.overall_conclusion,
            "unavailable_reason": self.unavailable_reason,
            "coverage": {
                "status": self.coverage.status,
                "ingestion_run_count": self.coverage.ingestion_run_count,
                "latest_run_started_at": _iso_dt(self.coverage.latest_run_started_at),
                "latest_requested_filings": self.coverage.latest_requested_filings,
                "latest_stored_filings": self.coverage.latest_stored_filings,
                "discovery_scope": self.coverage.discovery_scope,
            },
            "limitations": list(self.limitations),
            "methodology": list(self.methodology),
            "display": {
                # The only rounded values in the whole result, and they are for reading.
                "price_change_percent": _display(self.price_change_percent),
                "purchase_value": _display(self.purchase_value_known),
                "sale_value": _display(self.sale_value_known),
                "net_reported_value": _display(self.net_reported_value),
            },
        }


def information_cutoff(end_date: date) -> datetime:
    """The exclusive UTC instant at which `end_date` stops being knowable.

    The end date is an end-of-day cutoff in the market's own timezone, so it becomes the
    first moment of the *following* local day, converted to UTC. Using UTC midnight instead
    would cut the day off four or five hours early and drop filings accepted that afternoon.
    """
    next_local_midnight = datetime.combine(
        end_date + timedelta(days=1), time.min, tzinfo=MARKET_TIMEZONE
    )
    return next_local_midnight.astimezone(timezone.utc)


def analyze(
    *,
    symbol: str,
    company_cik: str | None,
    company_name: str | None,
    requested_start: date,
    requested_end: date,
    observations: Sequence[PriceObservation],
    transactions: Sequence[TransactionRecord],
    coverage: CoverageInput,
    price_series: PriceSeriesSelection | None,
    price_unavailable_reason: str | None = None,
) -> AnalysisResult:
    """Produce the analysis. Pure; every input is a plain value."""
    cutoff = information_cutoff(requested_end)

    in_range = sorted(
        (
            item
            for item in observations
            if requested_start <= item.trading_date <= requested_end
        ),
        key=lambda item: item.trading_date,
    )
    distinct_dates = {item.trading_date for item in in_range}

    first_close = in_range[0].close if in_range else None
    last_close = in_range[-1].close if in_range else None
    price_first_date = in_range[0].trading_date if in_range else None
    price_last_date = in_range[-1].trading_date if in_range else None

    if price_series is None:
        price_change = None
        price_reason = price_unavailable_reason or PRICE_REASON_NO_SERIES
    elif len(distinct_dates) < 2:
        # One observation is not a change, and a change needs two real prices. Zero would be
        # a number pretending to be a measurement.
        price_change = None
        price_reason = PRICE_REASON_TOO_FEW_DATES
    else:
        price_change = (last_close / first_close - Decimal("1")) * Decimal("100")
        price_reason = None

    included, exclusions = _classify(
        transactions,
        cutoff=cutoff,
        range_start=price_first_date,
        range_end=price_last_date,
    )

    purchasable = [row for row in included if row.is_purchase]
    sellable = [row for row in included if not row.is_purchase]

    purchase_value = sum(
        (row.reported_value for row in purchasable if row.reported_value is not None),
        start=Decimal("0"),
    )
    sale_value = sum(
        (row.reported_value for row in sellable if row.reported_value is not None),
        start=Decimal("0"),
    )
    purchase_missing = sum(1 for row in purchasable if row.reported_value is None)
    sale_missing = sum(1 for row in sellable if row.reported_value is None)
    values_complete = purchase_missing == 0 and sale_missing == 0

    # A net built from partial data would look complete. Missing values are not zeros.
    net_value = (purchase_value - sale_value) if values_complete else None

    amendment_accessions = tuple(
        sorted(
            {
                record.accession_number
                for record in transactions
                if record.is_amendment and _accepted_before(record, cutoff)
            }
        )
    )

    comparison, comparison_reason = _compare(
        price_change=price_change,
        price_unavailable_reason=price_reason,
        net_value=net_value,
        has_eligible=bool(included),
        amendment_accessions=amendment_accessions,
        values_complete=values_complete,
    )

    result_coverage = Coverage(
        # Complete coverage is never inferred from the oldest and newest filing dates: an
        # absence of filings is not evidence of an absence of activity.
        status=(COVERAGE_PARTIAL if coverage.ingestion_run_count > 0 else COVERAGE_UNKNOWN),
        ingestion_run_count=coverage.ingestion_run_count,
        latest_run_started_at=coverage.latest_run_started_at,
        latest_requested_filings=coverage.latest_requested_filings,
        latest_stored_filings=coverage.latest_stored_filings,
        discovery_scope=coverage.discovery_scope,
    )

    return AnalysisResult(
        symbol=symbol,
        company_cik=company_cik,
        company_name=company_name,
        requested_start=requested_start,
        requested_end=requested_end,
        price_series=price_series,
        price_first_date=price_first_date,
        price_last_date=price_last_date,
        price_observation_count=len(distinct_dates),
        first_close=first_close,
        last_close=last_close,
        price_change_percent=price_change,
        price_unavailable_reason=price_reason,
        information_cutoff=cutoff,
        purchase_count=len(purchasable),
        sale_count=len(sellable),
        purchase_value_known=purchase_value,
        sale_value_known=sale_value,
        purchase_rows_without_value=purchase_missing,
        sale_rows_without_value=sale_missing,
        values_complete=values_complete,
        net_reported_value=net_value,
        sample_comparison=comparison,
        sample_comparison_reason=comparison_reason,
        overall_conclusion=INSUFFICIENT_COVERAGE,
        included=tuple(included),
        exclusions=exclusions,
        amendment_accessions=amendment_accessions,
        amendment_uncertainty=bool(amendment_accessions),
        coverage=result_coverage,
        limitations=_limitations(
            result_coverage=result_coverage,
            price_reason=price_reason,
            amendment_accessions=amendment_accessions,
            purchase_count=len(purchasable),
            purchase_missing=purchase_missing,
            sale_missing=sale_missing,
        ),
        methodology=_methodology(cutoff),
    )


def unavailable_result(
    *,
    symbol: str,
    reason: str,
    requested_start: date,
    requested_end: date,
    company_cik: str | None = None,
    company_name: str | None = None,
    note: str | None = None,
    coverage: CoverageInput | None = None,
) -> AnalysisResult:
    """A typed result for the case where there is nothing to analyse.

    Not an exception: "we hold no data for that symbol" is an answer, and one the caller can
    inspect. Nothing is guessed to fill the gaps -- every metric is None or zero and the
    reason says why.
    """
    resolved_coverage = coverage or CoverageInput(0, None, None, None, None)
    notes = [
        "Nothing was analysed, so no metric here carries any meaning.",
        "Retrospective analysis over currently stored data; not a point-in-time backtest.",
    ]
    if note:
        notes.append(note)

    return AnalysisResult(
        symbol=symbol,
        company_cik=company_cik,
        company_name=company_name,
        requested_start=requested_start,
        requested_end=requested_end,
        price_series=None,
        price_first_date=None,
        price_last_date=None,
        price_observation_count=0,
        first_close=None,
        last_close=None,
        price_change_percent=None,
        price_unavailable_reason=reason,
        information_cutoff=information_cutoff(requested_end),
        purchase_count=0,
        sale_count=0,
        purchase_value_known=Decimal("0"),
        sale_value_known=Decimal("0"),
        purchase_rows_without_value=0,
        sale_rows_without_value=0,
        values_complete=True,
        net_reported_value=None,
        sample_comparison=UNAVAILABLE,
        sample_comparison_reason=reason,
        overall_conclusion=INSUFFICIENT_COVERAGE,
        included=(),
        exclusions=(),
        amendment_accessions=(),
        amendment_uncertainty=False,
        coverage=Coverage(
            status=(
                COVERAGE_PARTIAL
                if resolved_coverage.ingestion_run_count > 0
                else COVERAGE_UNKNOWN
            ),
            ingestion_run_count=resolved_coverage.ingestion_run_count,
            latest_run_started_at=resolved_coverage.latest_run_started_at,
            latest_requested_filings=resolved_coverage.latest_requested_filings,
            latest_stored_filings=resolved_coverage.latest_stored_filings,
            discovery_scope=resolved_coverage.discovery_scope,
        ),
        limitations=tuple(notes),
        methodology=_methodology(information_cutoff(requested_end)),
        unavailable_reason=reason,
    )


def _classify(
    transactions: Sequence[TransactionRecord],
    *,
    cutoff: datetime,
    range_start: date | None,
    range_end: date | None,
) -> tuple[list[IncludedTransaction], tuple[Exclusion, ...]]:
    """Split transactions into included rows and counted reasons for the rest.

    The order of the checks decides which reason a row is filed under when more than one
    could apply. It runs from the most fundamental -- is this even a transaction in the right
    instrument -- to the most situational.
    """
    counts: Counter[str] = Counter()
    identities: defaultdict[str, list[str]] = defaultdict(list)
    included: list[IncludedTransaction] = []

    def exclude(reason: str, record: TransactionRecord) -> None:
        counts[reason] += 1
        if len(identities[reason]) < _EXCLUSION_IDENTITIES_SHOWN:
            identities[reason].append(record.identity)

    for record in transactions:
        if record.is_derivative or record.source_table != NON_DERIVATIVE_TABLE:
            exclude(EXCLUDED_DERIVATIVE, record)
            continue

        if record.transaction_code not in (PURCHASE_CODE, SALE_CODE):
            # Grants, gifts, option exercises, tax withholding, and every other code. They
            # are real events and they are not purchases or sales.
            exclude(EXCLUDED_CODE, record)
            continue

        if not _is_supported_class(record.security_title):
            exclude(EXCLUDED_SECURITY_CLASS, record)
            continue

        expected = ACQUIRED if record.transaction_code == PURCHASE_CODE else DISPOSED
        if record.acquired_disposed != expected:
            # A purchase that was disposed of, or a sale that was acquired. That is a
            # data-quality signal; reinterpreting it would be inventing a fact.
            exclude(EXCLUDED_INCONSISTENT, record)
            continue

        if (
            range_start is None
            or range_end is None
            or not range_start <= record.transaction_date <= range_end
        ):
            exclude(EXCLUDED_OUTSIDE_RANGE, record)
            continue

        if record.acceptance_datetime is None:
            # Without an acceptance time the cutoff cannot be applied, so the row cannot be
            # shown to have been knowable at the end of the period.
            exclude(EXCLUDED_ACCEPTANCE_UNKNOWN, record)
            continue

        if record.acceptance_datetime >= cutoff:
            exclude(EXCLUDED_AFTER_CUTOFF, record)
            continue

        included.append(_value(record))

    return included, tuple(
        Exclusion(reason=reason, count=count, identities=tuple(identities[reason]))
        for reason, count in sorted(counts.items())
    )


def _value(record: TransactionRecord) -> IncludedTransaction:
    """Attach a reported value, or say why one cannot be attached."""
    reason: str | None = None
    value: Decimal | None = None

    if record.shares is None or record.shares <= 0:
        reason = VALUE_SHARES_UNUSABLE
    elif record.price_per_share is None or record.price_per_share == 0:
        # A reported zero price is usually a gift, a grant, or an award "for no
        # consideration". Treating it as a value of zero would fold those into a purchase or
        # sale total as though money had changed hands.
        reason = VALUE_PRICE_UNUSABLE
    else:
        value = record.shares * record.price_per_share

    return IncludedTransaction(
        accession_number=record.accession_number,
        source_table=record.source_table,
        row_position=record.row_position,
        transaction_date=record.transaction_date,
        transaction_code=record.transaction_code,
        acquired_disposed=record.acquired_disposed,
        security_title=record.security_title,
        shares=record.shares if record.shares is not None else Decimal("0"),
        price_per_share=record.price_per_share,
        reported_value=value,
        value_unavailable_reason=reason,
        rule_10b5_1=record.rule_10b5_1,
        source_document_url=record.source_document_url,
    )


def _compare(
    *,
    price_change: Decimal | None,
    price_unavailable_reason: str | None,
    net_value: Decimal | None,
    has_eligible: bool,
    amendment_accessions: tuple[str, ...],
    values_complete: bool,
) -> tuple[str, str | None]:
    """The sample comparison, and why it is what it is.

    Precedence, most specific first: an amendment withhold, then a missing price period,
    then "there is nothing to compare", then the reasons the numbers are missing, and only
    then the directions themselves.

    A missing price period comes before "no eligible transactions" on purpose. Without a
    period there is no window to place a transaction in, so every transaction is excluded
    for being outside it -- and reporting that as "no eligible transactions" would describe
    the consequence while hiding the cause.

    Signs are read from unrounded values.
    """
    if amendment_accessions:
        return UNAVAILABLE, COMPARISON_REASON_AMENDMENTS
    if price_change is None:
        return UNAVAILABLE, COMPARISON_REASON_PRICE
    if not has_eligible:
        return NO_ELIGIBLE_TRANSACTIONS, COMPARISON_REASON_NO_ELIGIBLE
    if not values_complete or net_value is None:
        return UNAVAILABLE, COMPARISON_REASON_INCOMPLETE_VALUES

    if price_change == 0 or net_value == 0:
        return NO_DIRECTIONAL_DIFFERENCE, None
    if price_change > 0 and net_value < 0:
        return PRICE_UP_NET_SELLING, None
    if price_change < 0 and net_value > 0:
        return PRICE_DOWN_NET_BUYING, None
    return SAME_DIRECTION, None


def _accepted_before(record: TransactionRecord, cutoff: datetime) -> bool:
    return (
        record.acceptance_datetime is not None
        and record.acceptance_datetime < cutoff
    )


def _is_supported_class(title: str) -> bool:
    """Normalised membership of the explicit allowlist, never a substring match."""
    return " ".join(title.split()).lower() in COMMON_STOCK_TITLES


def _limitations(
    *,
    result_coverage: Coverage,
    price_reason: str | None,
    amendment_accessions: tuple[str, ...],
    purchase_count: int,
    purchase_missing: int,
    sale_missing: int,
) -> tuple[str, ...]:
    notes = [
        "Retrospective analysis over currently stored data. This is not a point-in-time "
        "backtest: later provider revisions and adjusted price histories have not been "
        "reconstructed, so the prices are as they stand today, not as they stood then.",
        "Transaction dates are matched to the actual price comparison dates, and no missing "
        "session is forward-filled. The presence of some bars is not evidence of a complete "
        "trading calendar.",
        "Prices are split-adjusted as reported by the provider; transaction shares and prices "
        "are the original SEC figures. They are deliberately not combined with each other.",
        "P and S describe purchases and sales, including private ones. Nothing here is "
        "labelled an open-market transaction, because Form 4 does not say that.",
    ]
    if result_coverage.status != COVERAGE_PARTIAL:
        notes.append(
            "No ingestion record exists for this company, so how much was searched is "
            "unknown rather than merely narrow."
        )
    else:
        notes.append(
            "Filings come from SEC's recent submissions list only, so this is a bounded "
            "sample of insider activity and not a complete history."
        )
    if purchase_count == 0:
        notes.append(
            "No purchases appear in these filings. That is not the same statement as "
            "'insiders made no purchases during this period' -- an unimported or "
            "unsampled filing could contain one."
        )
    if purchase_missing or sale_missing:
        notes.append(
            f"{purchase_missing + sale_missing} eligible transaction(s) could not be valued "
            "because the reported shares or price were missing or unusable. The stored rows "
            "are unchanged; only the derived value is withheld."
        )
    if price_reason is not None:
        notes.append(f"Price metrics are unavailable: {price_reason}.")
    if amendment_accessions:
        notes.append(
            "An amendment was accepted before the cutoff, so net insider direction and the "
            "divergence comparison are withheld. Which original transactions it corrects "
            "cannot be determined from the stored records."
        )
    return tuple(notes)


def _methodology(cutoff: datetime) -> tuple[str, ...]:
    """The rules, restated in the result so it explains itself without the README."""
    return (
        "price_change_percent = (last_close / first_close - 1) * 100, over the first and "
        "last available closes inside the requested range.",
        "A transaction counts only when it is non-derivative, in a supported common-share "
        "class, coded P/A or S/D, dated inside the actual price comparison dates, and filed "
        "by a filing accepted before the information cutoff.",
        f"The information cutoff is the exclusive start of the day after the requested end "
        f"date in {MARKET_TIMEZONE.key}, which is {cutoff.isoformat()}.",
        "reported_value = reported_shares * reported_price_per_share, using the SEC's own "
        "pair. Net reported value is purchases minus sales. These are estimates from "
        "reported prices and are not exact cash flows.",
        "Transactions are read independently of reporting owners, so no owner join can "
        "multiply a total.",
        "The sample comparison describes what these records show. It is not a validated "
        "signal and carries no confidence score.",
    )


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _iso_dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dec(value: Decimal | None) -> str | None:
    """Exact, unrounded. The value that decisions were actually made from."""
    return None if value is None else str(value)


def _display(value: Decimal | None) -> str | None:
    """Rounded for reading only. Never used to decide a sign or a comparison."""
    if value is None:
        return None
    return str(value.quantize(DISPLAY_PLACES, rounding=ROUND_HALF_UP))