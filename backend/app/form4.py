"""SEC Form 4 ownership XML: bytes in, typed values out.

Pure. It imports no HTTP client, no database, and no application settings, so the whole
parser can be exercised from a fixture without a network or a running PostgreSQL -- the
same reason `valuation.py` is kept free of those imports.

`parse_ownership_document` takes raw XML bytes and returns what the document *says*. The
accession number, filing date, acceptance timestamp, retrieval time, and source URL are
not in the XML; they come from the submissions feed, and the caller attaches them (see
`app.sec_edgar.FilingRecord`). That split is what keeps this module free of provenance it
would otherwise have to be handed from outside.

Three properties are worth stating outright, because they are the ones a careless parser
loses:

* **Absent is not the same as zero or false.** A missing price is `None`; a price reported
  as `0` is `Decimal("0")`. `rule_10b5_1` is `None` when the document does not say, and
  `False` when it says no.
* **Nothing is inferred.** An amendment records the original submission date only when the
  document states one. A transaction-to-owner assignment is never invented: Form 4 puts
  owners at document level, so they are returned as a list beside the transactions, not
  spread across them.
* **Nothing is silently dropped.** A transaction row that cannot be read raises rather than
  being skipped. Holding rows are a different thing from transactions and are excluded by
  name -- and counted, so the exclusion is visible rather than invisible.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

# The two tables a Form 4 can carry transactions in, and the two holding-only tables that
# look similar but are not transactions.
NON_DERIVATIVE_TABLE = "nonDerivativeTable"
DERIVATIVE_TABLE = "derivativeTable"

_TRANSACTION_ELEMENT = {
    NON_DERIVATIVE_TABLE: "nonDerivativeTransaction",
    DERIVATIVE_TABLE: "derivativeTransaction",
}
_HOLDING_ELEMENT = {
    NON_DERIVATIVE_TABLE: "nonDerivativeHolding",
    DERIVATIVE_TABLE: "derivativeHolding",
}

# The element carrying the exercise price has been named at least two ways across schema
# versions. Both are accepted rather than betting on one.
_EXERCISE_PRICE_ELEMENTS = ("conversionOrExercisePrice", "exercisePrice")

_TRUE_FLAGS = frozenset({"1", "true"})
_FALSE_FLAGS = frozenset({"0", "false"})


class Form4Error(Exception):
    """Base class for every failure this parser reports."""


class MalformedDocumentError(Form4Error):
    """The bytes cannot be read as an ownership document."""


class NumberParseError(MalformedDocumentError):
    """A numeric or date field cannot be read as the kind of value it claims to be.

    A subclass of `MalformedDocumentError` so a caller can handle all document problems
    together, or numeric ones specifically.
    """


class IssuerMismatchError(Form4Error):
    """The document is about a different company than the one requested."""


@dataclass(frozen=True)
class FootnoteRef:
    """One footnote reference, and the field it was attached to.

    Field-level rather than row-level because that is what the document states: a
    footnote sits inside the element it explains, so `transactionShares` and
    `transactionPricePerShare` can carry different ones.
    """

    field: str
    footnote_id: str


@dataclass(frozen=True)
class ReportingOwner:
    """A person or entity who signed the filing.

    Attached to the filing, not to any transaction -- in Form 4 XML `reportingOwner` is a
    sibling of the transaction tables, and the document never says which owner a given row
    belongs to. Nothing here guesses.

    The relationship flags are `None` when the element is absent, which is a different
    fact from `False`.
    """

    owner_cik: str
    owner_name: str
    is_director: bool | None
    is_officer: bool | None
    is_ten_percent_owner: bool | None
    is_other: bool | None
    officer_title: str | None
    other_text: str | None


@dataclass(frozen=True)
class TransactionRow:
    """One reported transaction, from the non-derivative or derivative table.

    The derivative-only fields are `None` on non-derivative rows.
    """

    source_table: str
    # 0-based position among the *transactions* of its table, in document order. Holding
    # rows do not consume a position, so this matches what Step 4 will store.
    row_position: int
    security_title: str
    transaction_date: date
    transaction_code: str
    acquired_disposed: str
    shares: Decimal
    # None when not reported. An explicitly reported 0 stays Decimal("0").
    price_per_share: Decimal | None
    ownership_direct_indirect: str | None
    nature_of_ownership: str | None
    shares_owned_following: Decimal | None
    footnote_refs: tuple[FootnoteRef, ...]

    underlying_security_title: str | None
    underlying_shares: Decimal | None
    exercise_price: Decimal | None
    expiration_date: date | None


@dataclass(frozen=True)
class Form4Document:
    """Everything the ownership XML states, with nothing inferred."""

    document_type: str
    schema_version: str | None
    issuer_cik: str
    issuer_name: str
    issuer_trading_symbol: str | None
    period_of_report: date | None
    # Only ever set by the document itself, which means only on amendments. Ordinary
    # Form 4s do not carry it.
    date_of_original_submission: date | None
    # True / False / None. None means the document does not say -- not "no".
    rule_10b5_1: bool | None
    footnotes: Mapping[str, str]
    remarks: str | None
    owners: tuple[ReportingOwner, ...]
    transactions: tuple[TransactionRow, ...]
    # Transaction-element-free rows that were present and deliberately not treated as
    # transactions. Counted so the exclusion is not invisible.
    holding_rows_skipped: int

    @property
    def is_amendment(self) -> bool:
        """Derived from the document's own type, not from the accession number."""
        return self.document_type.upper().endswith("/A")


def parse_ownership_document(
    xml_bytes: bytes, *, expected_issuer_cik: str | None = None
) -> Form4Document:
    """Parse Form 4 XML into typed values.

    `expected_issuer_cik`, when given, is compared against the document's own issuer CIK
    as ten-digit strings. Owned here rather than in the network layer so the check is
    exercisable from bytes.

    Raises `MalformedDocumentError`, `NumberParseError`, or `IssuerMismatchError`.
    """
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        # ElementTree refuses external entities outright -- an external SYSTEM entity
        # raises here as "undefined entity" and fetches nothing. A test pins that, so the
        # property cannot be lost by a quiet change of parser.
        raise MalformedDocumentError(f"not well-formed XML: {exc}") from None

    if _local(root.tag) != "ownershipDocument":
        raise MalformedDocumentError(
            f"expected an <ownershipDocument> root, found <{_local(root.tag)}>"
        )

    issuer = _child(root, "issuer")
    if issuer is None:
        raise MalformedDocumentError("the document has no <issuer> element")

    issuer_cik = _required_text(issuer, "issuerCik", "issuer/issuerCik")
    issuer_name = _required_text(issuer, "issuerName", "issuer/issuerName")

    if expected_issuer_cik is not None:
        if _ten_digit(issuer_cik) != _ten_digit(expected_issuer_cik):
            raise IssuerMismatchError(
                f"the document is about issuer CIK {issuer_cik}, not the requested "
                f"{expected_issuer_cik}"
            )

    footnotes = _parse_footnotes(root)

    owners = tuple(_parse_owner(node) for node in _children(root, "reportingOwner"))
    transactions, holding_rows = _parse_tables(root)

    return Form4Document(
        document_type=_required_text(root, "documentType", "documentType"),
        schema_version=_text(root, "schemaVersion"),
        issuer_cik=issuer_cik,
        issuer_name=issuer_name,
        issuer_trading_symbol=_text(issuer, "issuerTradingSymbol"),
        period_of_report=_parse_date(_text(root, "periodOfReport"), "periodOfReport"),
        date_of_original_submission=_parse_date(
            _text(root, "dateOfOriginalSubmission"), "dateOfOriginalSubmission"
        ),
        rule_10b5_1=_parse_flag(_text(root, "aff10b5One"), "aff10b5One"),
        footnotes=footnotes,
        remarks=_text(root, "remarks"),
        owners=owners,
        transactions=transactions,
        holding_rows_skipped=holding_rows,
    )


def _parse_owner(node: ElementTree.Element) -> ReportingOwner:
    identity = _child(node, "reportingOwnerId")
    if identity is None:
        raise MalformedDocumentError("a <reportingOwner> has no <reportingOwnerId>")

    relationship = _child(node, "reportingOwnerRelationship")

    def flag(name: str) -> bool | None:
        return _parse_flag(
            _text(relationship, name), f"reportingOwnerRelationship/{name}"
        )

    return ReportingOwner(
        owner_cik=_required_text(identity, "rptOwnerCik", "reportingOwnerId/rptOwnerCik"),
        owner_name=_required_text(
            identity, "rptOwnerName", "reportingOwnerId/rptOwnerName"
        ),
        is_director=flag("isDirector"),
        is_officer=flag("isOfficer"),
        is_ten_percent_owner=flag("isTenPercentOwner"),
        is_other=flag("isOther"),
        officer_title=_text(relationship, "officerTitle"),
        other_text=_text(relationship, "otherText"),
    )


def _parse_tables(root: ElementTree.Element) -> tuple[tuple[TransactionRow, ...], int]:
    """Transactions from both tables, plus a count of holding rows left alone."""
    rows: list[TransactionRow] = []
    holdings = 0

    for table_name, transaction_element in _TRANSACTION_ELEMENT.items():
        table = _child(root, table_name)
        if table is None:
            continue

        holding_name = _HOLDING_ELEMENT[table_name]
        position = 0
        for child in table:
            name = _local(child.tag)
            if name == transaction_element:
                rows.append(_parse_transaction(child, table_name, position))
                position += 1
            elif name == holding_name:
                # A holding row reports a position that already existed; it is not a
                # trade. Counted rather than ignored so the decision is visible.
                holdings += 1
            elif name.strip():
                raise MalformedDocumentError(
                    f"unexpected <{name}> inside <{table_name}>; this parser does not "
                    "know whether it is a transaction, and will not guess"
                )

    return tuple(rows), holdings


def _parse_transaction(
    node: ElementTree.Element, table_name: str, position: int
) -> TransactionRow:
    where = f"{table_name}[{position}]"

    amounts = _child(node, "transactionAmounts")
    coding = _child(node, "transactionCoding")
    if coding is None:
        raise MalformedDocumentError(f"{where}: no <transactionCoding> element")
    if amounts is None:
        raise MalformedDocumentError(f"{where}: no <transactionAmounts> element")

    post = _child(node, "postTransactionAmounts")
    nature = _child(node, "ownershipNature")
    underlying = _child(node, "underlyingSecurity")

    exercise_price_text = None
    for element_name in _EXERCISE_PRICE_ELEMENTS:
        exercise_price_text = _value(node, element_name)
        if exercise_price_text is not None:
            break

    return TransactionRow(
        source_table=table_name,
        row_position=position,
        security_title=_required_value(node, "securityTitle", f"{where}/securityTitle"),
        transaction_date=_required_date(
            _value(node, "transactionDate"), f"{where}/transactionDate"
        ),
        # Not wrapped in <value>, unlike almost everything else in the document.
        transaction_code=_required_text(coding, "transactionCode", f"{where}/transactionCode"),
        acquired_disposed=_required_value(
            amounts, "transactionAcquiredDisposedCode",
            f"{where}/transactionAcquiredDisposedCode",
        ),
        shares=_required_decimal(
            _value(amounts, "transactionShares"), f"{where}/transactionShares"
        ),
        price_per_share=_decimal(
            _value(amounts, "transactionPricePerShare"),
            f"{where}/transactionPricePerShare",
        ),
        ownership_direct_indirect=(
            _value(nature, "directOrIndirectOwnership") if nature is not None else None
        ),
        nature_of_ownership=(
            _value(nature, "natureOfOwnership") if nature is not None else None
        ),
        shares_owned_following=(
            _decimal(
                _value(post, "sharesOwnedFollowingTransaction"),
                f"{where}/sharesOwnedFollowingTransaction",
            )
            if post is not None
            else None
        ),
        footnote_refs=_collect_footnote_refs(node),
        underlying_security_title=(
            _value(underlying, "underlyingSecurityTitle") if underlying is not None else None
        ),
        underlying_shares=(
            _decimal(
                _value(underlying, "underlyingSecurityShares"),
                f"{where}/underlyingSecurityShares",
            )
            if underlying is not None
            else None
        ),
        exercise_price=_decimal(exercise_price_text, f"{where}/exercisePrice"),
        expiration_date=_parse_date(
            _value(node, "expirationDate"), f"{where}/expirationDate"
        ),
    )


def _parse_footnotes(root: ElementTree.Element) -> Mapping[str, str]:
    container = _child(root, "footnotes")
    if container is None:
        return {}
    resolved: dict[str, str] = {}
    for node in _children(container, "footnote"):
        identifier = (node.get("id") or "").strip()
        if not identifier:
            raise MalformedDocumentError("a <footnote> has no id, so nothing can refer to it")
        text = (node.text or "").strip()
        if identifier in resolved and resolved[identifier] != text:
            raise MalformedDocumentError(
                f"footnote id {identifier!r} is used twice with different text"
            )
        resolved[identifier] = text
    return resolved


def _collect_footnote_refs(element: ElementTree.Element) -> tuple[FootnoteRef, ...]:
    """Every footnote reference under `element`, attributed to its owning field."""
    refs: list[FootnoteRef] = []
    seen: set[tuple[str, str]] = set()
    for parent in element.iter():
        field = _local(parent.tag)
        for child in parent:
            if _local(child.tag) != "footnoteId":
                continue
            identifier = (child.get("id") or "").strip()
            if not identifier or (field, identifier) in seen:
                continue
            seen.add((field, identifier))
            refs.append(FootnoteRef(field=field, footnote_id=identifier))
    return tuple(refs)


# --- element access -------------------------------------------------------------------
#
# Every lookup matches on the element's *local* name, so a document with a default
# namespace parses exactly like one without. Real filings carry no namespace; accepting
# either means a future change at the SEC cannot break this quietly.


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _child(element: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
    if element is None:
        return None
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local(child.tag) == name]


def _text(element: ElementTree.Element | None, name: str) -> str | None:
    """Trimmed text of a child, or None when the child is missing or empty.

    An empty element and a missing element both mean "not stated", so they collapse to
    the same thing here. `<value>0</value>` is neither -- it is "0".
    """
    node = _child(element, name)
    if node is None or node.text is None:
        return None
    stripped = node.text.strip()
    return stripped or None


def _value(element: ElementTree.Element | None, name: str) -> str | None:
    """Text of the `<value>` child of `name` -- Form 4's usual way of wrapping a value."""
    return _text(_child(element, name), "value")


# --- required fields ------------------------------------------------------------------


def _required_text(
    element: ElementTree.Element | None, name: str, where: str
) -> str:
    found = _text(element, name)
    if found is None:
        raise MalformedDocumentError(f"{where} is missing")
    return found


def _required_value(element: ElementTree.Element, name: str, where: str) -> str:
    found = _value(element, name)
    if found is None:
        raise MalformedDocumentError(f"{where} is missing")
    return found


# --- typed values ---------------------------------------------------------------------


def _parse_date(text: str | None, where: str) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise NumberParseError(f"{where} is not a calendar date ({text!r})") from None


def _required_date(text: str | None, where: str) -> date:
    parsed = _parse_date(text, where)
    if parsed is None:
        raise MalformedDocumentError(f"{where} is missing")
    return parsed


def _decimal(text: str | None, where: str) -> Decimal | None:
    """A Decimal straight from the source text. Never through a float.

    No decimal-place limit is applied. Step 2 established that refusing a value for
    exceeding a column's scale throws away real data; precision is preserved here and the
    storage decision is left to Step 4.
    """
    if text is None:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise NumberParseError(f"{where} is not a number ({text!r})") from None
    if not value.is_finite():
        raise NumberParseError(f"{where} is not a finite number ({text!r})")
    return value


def _required_decimal(text: str | None, where: str) -> Decimal:
    value = _decimal(text, where)
    if value is None:
        raise MalformedDocumentError(f"{where} is missing")
    return value


def _parse_flag(text: str | None, where: str) -> bool | None:
    """A 0/1 (or true/false) flag. None when the element is absent, which is not False."""
    if text is None:
        return None
    lowered = text.strip().lower()
    if lowered in _TRUE_FLAGS:
        return True
    if lowered in _FALSE_FLAGS:
        return False
    raise MalformedDocumentError(f"{where} is {text!r}, which is not a 0/1 flag")


def _ten_digit(cik: str) -> str:
    """CIKs compare as ten-digit strings, because that is how EDGAR writes them.

    `zfill` alone is enough: a document's `issuerCik` is already padded, and an unpadded
    one from a caller pads to the same value.
    """
    return cik.strip().zfill(10)