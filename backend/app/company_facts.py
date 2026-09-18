"""SEC Company Facts: the issuer's own reported XBRL values, fetched and narrowed.

The endpoint is `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`, documented
at <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>. NVDA's is
about 3.9 MiB and carries 627 US-GAAP concepts; this module keeps six of them and says why.

It shares the transport in `app.sec_edgar` rather than growing its own: the User-Agent, the
rate limiter, the redirect validation, the timeouts, the size cap and the error taxonomy all
already exist there, and a second copy of any of them would be a second thing to get wrong.

**Numbers are never turned into floats.** The payload is parsed with `parse_float=Decimal`,
so a value written as `1.23` arrives as `Decimal("1.23")` rather than as the nearest binary
double. Integers arrive as Python ints, which are already exact.

**Observations are not collapsed.** Two entries can share a concept and an end date and still
be different facts -- NVDA reports both a quarter and a year-to-date figure ending
`2026-07-26` -- and both are kept. See `app.models.FinancialFact` for why the natural key
carries the period start.
"""

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from app.sec_edgar import SecEdgarClient, SecEdgarError, normalize_cik

COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
TAXONOMY = "us-gaap"

# What this milestone is labelled as in a stored snapshot. The endpoint is fetched today and
# what comes back is today's view of the issuer's history, filtered here to the selected
# filings -- it is not a recording of what the endpoint returned on the analysis date.
SNAPSHOT_LABEL = "current-source snapshot, filtered to selected filings"


class CompanyFactsError(SecEdgarError):
    """Base class for every failure this module reports.

    Under `SecEdgarError` rather than beside it: Company Facts is an SEC interface, reached
    over the same transport under the same fair-access rules, so a caller should be able to
    catch one family for all of it.
    """


class MalformedCompanyFactsError(CompanyFactsError):
    """The payload could not be read as Company Facts."""


@dataclass(frozen=True)
class ConceptMapping:
    """One concept this milestone keeps, and what it means.

    `note` is carried into the output so a reader never has to guess why a concept was
    chosen or what its alternative was.
    """

    name: str
    concept: str
    kind: str  # "duration" or "instant"
    note: str


# An explicitly small set. Adding every available metric would make this a copy of the
# endpoint rather than a selection from it.
CONCEPTS: tuple[ConceptMapping, ...] = (
    ConceptMapping(
        name="revenue",
        concept="Revenues",
        kind="duration",
        note=(
            "One of the two concepts issuers use for total revenue, and the one NVIDIA "
            "reports today. It is NOT a fallback for the other: they are different concepts "
            "with different definitions, and an issuer that reports one has not thereby "
            "reported the other."
        ),
    ),
    ConceptMapping(
        name="revenue_contract_with_customer",
        concept="RevenueFromContractWithCustomerExcludingAssessedTax",
        kind="duration",
        note=(
            "The other revenue concept. Apple reports this one far more than `Revenues`; "
            "NVIDIA used it through fiscal 2022 and then moved to `Revenues`. Whichever an "
            "issuer reports, it is stored separately and never added to the other -- two "
            "concepts are not one series, and a total mixing them would be neither."
        ),
    ),
    ConceptMapping(
        name="net_income",
        concept="NetIncomeLoss",
        kind="duration",
        note=(
            "Net income (loss) attributable to parent. Deliberately allowed to be negative "
            "in the schema -- a loss-making period reports a negative value and a "
            "non-negative constraint would reject a real filing."
        ),
    ),
    ConceptMapping(
        name="total_assets",
        concept="Assets",
        kind="instant",
        note="Total assets. A balance-sheet figure, so it has no period start.",
    ),
    ConceptMapping(
        name="total_liabilities",
        concept="Liabilities",
        kind="instant",
        note="Total liabilities. A balance-sheet figure, so it has no period start.",
    ),
    ConceptMapping(
        name="cash_and_cash_equivalents",
        concept="CashAndCashEquivalentsAtCarryingValue",
        kind="instant",
        note="Cash and cash equivalents at carrying value.",
    ),
)

CONCEPTS_BY_NAME: Mapping[str, ConceptMapping] = {item.name: item for item in CONCEPTS}


@dataclass(frozen=True)
class FactObservation:
    """One reported value, for one period, as one filing reported it."""

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


@dataclass(frozen=True)
class ParsedCompanyFacts:
    """What was taken from one payload, and what was not."""

    entity_name: str | None
    observations: tuple[FactObservation, ...]
    concepts_matched: tuple[str, ...]
    concepts_unmatched: tuple[str, ...]
    limitations: tuple[str, ...]


def fetch_payload(client: SecEdgarClient, cik: str) -> tuple[bytes, str, datetime]:
    """Fetch an issuer's Company Facts. Returns the bytes, the URL, and when.

    The bytes are returned rather than a parsed structure so the caller can hash and store
    exactly what the SEC sent, before any of it is interpreted.
    """
    normalized = normalize_cik(cik)
    url = COMPANY_FACTS_URL.format(cik=normalized)
    body, _ = client.fetch_bytes(
        url,
        accept="application/json",
        on_missing=CompanyFactsError,
        what=f"Company Facts for CIK {normalized}",
    )
    return body, url, datetime.now(timezone.utc)


def content_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parse_company_facts(
    payload: bytes, *, accessions: Collection[str]
) -> ParsedCompanyFacts:
    """Read the mapped concepts out of a payload, keeping only the given accessions.

    Filtering by accession is what ties a figure to a filing the analysis actually selected,
    so a value cannot enter from a filing whose availability was never established.
    """
    try:
        # parse_float=Decimal is the point of this call: without it every decimal value
        # would pass through a binary float before this code ever saw it.
        document = json.loads(payload, parse_float=Decimal)
    except ValueError as exc:
        raise MalformedCompanyFactsError(
            f"Company Facts did not return valid JSON: {exc}"
        ) from exc

    if not isinstance(document, dict):
        raise MalformedCompanyFactsError(
            f"Company Facts returned a {type(document).__name__}, not an object"
        )

    facts = document.get("facts")
    if not isinstance(facts, dict):
        raise MalformedCompanyFactsError("Company Facts has no 'facts' object")

    gaap = facts.get(TAXONOMY)
    if not isinstance(gaap, dict):
        raise MalformedCompanyFactsError(
            f"Company Facts has no {TAXONOMY!r} taxonomy, so none of the mapped concepts "
            "can be read"
        )

    wanted = set(accessions)
    observations: list[FactObservation] = []
    matched: list[str] = []
    unmatched: list[str] = []

    for mapping in CONCEPTS:
        entry = gaap.get(mapping.concept)
        if not isinstance(entry, dict):
            unmatched.append(mapping.name)
            continue

        found = [
            observation
            for observation in _observations_for(entry, mapping)
            if observation.accession_number in wanted
        ]
        if found:
            matched.append(mapping.name)
            observations.extend(found)
        else:
            # The concept exists, but nothing in the selected filings reports it. That is a
            # different fact from the concept being absent, and it is reported as such.
            unmatched.append(mapping.name)

    entity_name = document.get("entityName")
    limitations = [
        (
            "This is a current-source snapshot of SEC Company Facts, filtered to the "
            "selected filings. It is not a recording of what the endpoint returned on the "
            "analysis date, and it is not an as-of view."
        ),
        (
            "Only a small set of concepts is stored. Revenue has two distinct concepts and "
            "both are kept separately; which one is populated depends on what the selected "
            "filings actually report, and the other is reported as unmatched rather than "
            "as zero. They are never combined."
        ),
    ]
    if unmatched:
        limitations.append(
            "No observation in the selected filings reported: "
            f"{', '.join(sorted(unmatched))}. These are reported as unmatched rather than "
            "as zero."
        )

    return ParsedCompanyFacts(
        entity_name=entity_name if isinstance(entity_name, str) else None,
        observations=tuple(observations),
        concepts_matched=tuple(matched),
        concepts_unmatched=tuple(unmatched),
        limitations=tuple(limitations),
    )


def _observations_for(
    entry: Mapping, mapping: ConceptMapping
) -> Sequence[FactObservation]:
    """Every observation of one concept, across all its units."""
    units = entry.get("units")
    if not isinstance(units, dict):
        raise MalformedCompanyFactsError(
            f"the {mapping.concept!r} entry has no 'units' object"
        )

    observations: list[FactObservation] = []
    for unit, items in units.items():
        if not isinstance(items, list):
            raise MalformedCompanyFactsError(
                f"the {mapping.concept!r} unit {unit!r} is not a list"
            )
        for item in items:
            observations.append(_observation(item, mapping, str(unit)))

    # A stable order, so a stored set of rows does not depend on dictionary ordering.
    observations.sort(
        key=lambda o: (o.period_end, o.period_start or date.min, o.accession_number or "")
    )
    return observations


def _observation(item: Mapping, mapping: ConceptMapping, unit: str) -> FactObservation:
    where = f"{mapping.concept}[{unit}]"
    if not isinstance(item, dict):
        raise MalformedCompanyFactsError(f"{where}: an observation is not an object")

    period_end = _required_date(item.get("end"), f"{where}/end")
    period_start = _optional_date(item.get("start"), f"{where}/start")

    if mapping.kind == "instant" and period_start is not None:
        # An instant fact describes a point in time. If the source gives it a start, the
        # concept is not the one this mapping claims, and quietly storing a duration as a
        # balance would be worse than stopping.
        raise MalformedCompanyFactsError(
            f"{where}: {mapping.concept!r} is mapped as an instant fact but the payload "
            f"gives it a period start of {period_start}"
        )

    return FactObservation(
        taxonomy=TAXONOMY,
        concept=mapping.concept,
        unit=unit,
        value=_decimal(item.get("val"), f"{where}/val"),
        period_start=period_start,
        period_end=period_end,
        accession_number=_optional_text(item.get("accn")),
        form=_optional_text(item.get("form")),
        filed_date=_optional_date(item.get("filed"), f"{where}/filed"),
        fiscal_year=_optional_int(item.get("fy"), f"{where}/fy"),
        fiscal_period=_optional_text(item.get("fp")),
        frame=_optional_text(item.get("frame")),
    )


def _decimal(raw: object, where: str) -> Decimal:
    """A value that is exactly what the payload said.

    Floats have already become Decimals by the time they arrive, and integers are exact as
    they are, so the only two shapes accepted here are the two the JSON parser produces.
    """
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, bool) or not isinstance(raw, int):
        raise MalformedCompanyFactsError(
            f"{where} is {raw!r} ({type(raw).__name__}), which is not a number"
        )
    else:
        value = Decimal(raw)

    if not value.is_finite():
        raise MalformedCompanyFactsError(f"{where} is not a finite number ({raw!r})")
    return value


def _required_date(raw: object, where: str) -> date:
    parsed = _optional_date(raw, where)
    if parsed is None:
        raise MalformedCompanyFactsError(f"{where} is missing")
    return parsed


def _optional_date(raw: object, where: str) -> date | None:
    text = _optional_text(raw)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise MalformedCompanyFactsError(
            f"{where} is not a calendar date ({text!r})"
        ) from None


def _optional_int(raw: object, where: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise MalformedCompanyFactsError(f"{where} is not a whole number ({raw!r})")
    return raw


def _optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None
