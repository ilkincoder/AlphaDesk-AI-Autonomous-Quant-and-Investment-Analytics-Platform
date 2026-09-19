"""What the tools actually returned, and the identifiers used to cite it.

The point of this module is that the answer's supporting material is assembled by the
*application* from real tool output, not written by the model. A model that has read a filing
passage can cite it; it cannot invent an accession number, a section or a URL, because those
are never taken from its prose. Every citation in a run result is read back out of this map.

**Identifiers are flat and assigned in execution order.** One `E<n>` per citable thing: a
market analysis, a set of financial observations, one filing passage, a portfolio context.
A flat space rather than a nested one because the model has to write these by hand, and
`[E3]` is harder to get wrong than `[E2.1]`.

**A tool that found nothing is still evidence.** An `unavailable` result is recorded, with no
payload and its status shown, because "we searched and this system holds nothing" is a fact an
answer is entitled to state -- and grounding it in a reference is better than asserting it.
What a passage-less search contributes is nothing at all: its citable things are the passages,
and there were none.

**Every entry is bounded.** A filing search returns passages of up to a few thousand
characters each; a run that cites twenty of them cannot put all of that in front of the model
that has to write the answer. Entries are trimmed by *detail*, never by dropping an entry
silently -- a trimmed entry says it was trimmed.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# What kind of thing an entry is. Not a taxonomy for its own sake: the two are trimmed
# differently, because a filing passage is prose and a structured result is fields.
KIND_TOOL_RESULT = "tool_result"
KIND_FILING_PASSAGE = "filing_passage"

# How much of one entry's detail is carried.
#
# A passage gets a much larger allowance than a structured result, because for a passage the
# quoted text *is* the evidence -- trimming it to a couple of sentences would leave the model
# composing an answer from a citation it cannot read. The figure matches the retrieval tool's
# own per-passage cap, so nothing is lost twice. A structured result is a handful of numbers
# and period strings that do not need the room.
MAX_SUMMARY_CHARS = 1500
MAX_PASSAGE_CHARS = 3000

# A hard ceiling on entries, so one run cannot build an unbounded map. Reaching it is
# reported rather than silent.
MAX_ENTRIES = 80

TRUNCATION_MARKER = " ... [trimmed]"


@dataclass(frozen=True)
class EvidenceItem:
    """One citable thing: what it is, where it came from, and how to check it."""

    reference: str
    kind: str
    tool: str
    symbol: str
    label: str
    status: str
    summary: dict[str, Any] | None = None
    citation: dict[str, Any] | None = None
    trimmed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "kind": self.kind,
            "tool": self.tool,
            "symbol": self.symbol,
            "label": self.label,
            "status": self.status,
            "summary": self.summary,
            "citation": self.citation,
            "trimmed": self.trimmed,
        }

    def render(self, *, excerpt_chars: int | None = None) -> str:
        """One block for a prompt.

        `excerpt_chars` shortens a quoted passage. A step that only has to *know what was
        found* does not need the filing's prose in full, and sending it costs both tokens and
        reliability -- a model given 48,000 characters of passages to digest answers with a
        report rather than the digest it was asked for. The step that writes the answer is
        the one that gets the passages whole.
        """
        parts = [f"[{self.reference}] {self.label}"]
        if self.status != "ok":
            # Shown because `unavailable` and `partial` mean an answer may say something
            # about a gap, and the reader has to be able to tell that from a finding.
            parts.append(f"  status: {self.status}")
        if self.citation:
            citation = self.citation
            location = ", ".join(
                str(citation[key])
                for key in ("accession_number", "form_type", "section")
                if citation.get(key)
            )
            if location:
                parts.append(f"  filing: {location}")
            if citation.get("source_url"):
                parts.append(f"  source: {citation['source_url']}")
            if citation.get("similarity") is not None:
                # Named a similarity, never a confidence. The two are not the same number and
                # presenting one as the other invites a reading it cannot support.
                parts.append(f"  similarity (not a probability): {citation['similarity']}")
        if self.summary:
            summary = self.summary
            if excerpt_chars is not None and isinstance(summary.get("quoted_text"), str):
                quoted = summary["quoted_text"]
                summary = dict(summary)
                summary["quoted_text"] = (
                    quoted
                    if len(quoted) <= excerpt_chars
                    else quoted[:excerpt_chars] + " ... [excerpt]"
                )
            parts.append(f"  {_compact(summary)}")
        if self.trimmed:
            parts.append("  (this entry was trimmed to fit the context budget)")
        return "\n".join(parts)


@dataclass
class EvidenceMap:
    """The run's evidence, built only from tool results.

    Mutable and per-run. A new instance per run is what makes one run's citations
    un-addressable from another's.
    """

    max_entries: int = MAX_ENTRIES
    _items: list[EvidenceItem] = field(default_factory=list)
    _by_reference: dict[str, EvidenceItem] = field(default_factory=dict)
    _overflowed: int = 0

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, reference: object) -> bool:
        return reference in self._by_reference

    def get(self, reference: str) -> EvidenceItem | None:
        return self._by_reference.get(reference)

    def references(self) -> list[str]:
        return [item.reference for item in self._items]

    @property
    def overflowed(self) -> int:
        """How many citable things were dropped because the map was full."""
        return self._overflowed

    # --- building ----------------------------------------------------------------------

    def add_filing_passages(
        self, *, tool: str, symbol: str, status: str, data: dict[str, Any]
    ) -> list[str]:
        """One entry per passage, because a passage is what a citation points at."""
        passages = data.get("passages") or []
        added: list[str] = []
        for index, passage in enumerate(passages, start=1):
            filing = passage.get("filing") or {}
            document = passage.get("document") or {}
            reference = self._append(
                EvidenceItem(
                    reference="",
                    kind=KIND_FILING_PASSAGE,
                    tool=tool,
                    symbol=symbol,
                    label=(
                        f"filing passage {index} from "
                        f"{filing.get('accession_number') or 'an unnamed filing'}"
                    ),
                    status=status,
                    summary=_passage_summary(passage),
                    # Read out of the retrieval result, which read it out of the index and
                    # the manifest. Nothing here is ever taken from model output.
                    citation={
                        "accession_number": filing.get("accession_number"),
                        "form_type": filing.get("form_type"),
                        "acceptance_datetime": filing.get("acceptance_datetime"),
                        "report_date": filing.get("report_date"),
                        "source_url": filing.get("source_url"),
                        "section": passage.get("section"),
                        "similarity": passage.get("similarity"),
                        "document_name": document.get("name"),
                        "document_role": document.get("role"),
                        "offsets": passage.get("offsets"),
                        "content_sha256": document.get("content_sha256"),
                    },
                )
            )
            if reference:
                added.append(reference)
        return added

    def add_tool_result(
        self,
        *,
        tool: str,
        symbol: str,
        status: str,
        label: str,
        summary: dict[str, Any] | None,
        citation: dict[str, Any] | None = None,
    ) -> str | None:
        """One entry for a whole structured result."""
        reference = self._append(
            EvidenceItem(
                reference="",
                kind=KIND_TOOL_RESULT,
                tool=tool,
                symbol=symbol,
                label=label,
                status=status,
                summary=summary,
                citation=citation,
            )
        )
        return reference or None

    def _append(self, item: EvidenceItem) -> str:
        if len(self._items) >= self.max_entries:
            # Reported, not silent: the run result says how many citable things were left out
            # so a short citation list is never mistaken for a complete one.
            self._overflowed += 1
            return ""

        reference = f"E{len(self._items) + 1}"
        allowance = (
            MAX_PASSAGE_CHARS
            if item.kind == KIND_FILING_PASSAGE
            else MAX_SUMMARY_CHARS
        )
        stored = EvidenceItem(
            reference=reference,
            kind=item.kind,
            tool=item.tool,
            symbol=item.symbol,
            label=item.label,
            status=item.status,
            summary=_trim_summary(item.summary, allowance),
            citation=item.citation,
            trimmed=_summary_overflows(item.summary, allowance),
        )
        self._items.append(stored)
        self._by_reference[reference] = stored
        return reference

    # --- using -------------------------------------------------------------------------

    def validate(self, references: Sequence[str]) -> tuple[list[str], list[str]]:
        """Split cited references into the ones that exist and the ones that do not.

        Order is preserved and duplicates are dropped, so the caller can report exactly which
        invented identifier to correct.
        """
        valid: list[str] = []
        invalid: list[str] = []
        for reference in references:
            if reference in self._by_reference:
                if reference not in valid:
                    valid.append(reference)
            elif reference not in invalid:
                invalid.append(reference)
        return valid, invalid

    def citations_for(self, references: Sequence[str]) -> list[dict[str, Any]]:
        """The citation block for a run result, resolved entirely from this map."""
        resolved: list[dict[str, Any]] = []
        for reference in references:
            item = self._by_reference.get(reference)
            if item is None:
                continue
            resolved.append(
                {
                    "reference": item.reference,
                    "tool": item.tool,
                    "label": item.label,
                    "filing": item.citation,
                }
            )
        return resolved

    def as_list(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self._items]

    def render(
        self, *, limit: int | None = None, excerpt_chars: int | None = None
    ) -> str:
        """The evidence, as prompt text, bounded.

        `limit` caps how many entries are shown. When it bites, the visible entries are the
        first ones -- which are the ones the run gathered first, and are as likely to be the
        ones the findings rest on as any other. The caller is told how many were hidden so it
        can pass that on: a model composing an answer from a partial evidence list must know
        the list is partial.

        `excerpt_chars` shortens quoted passages; see `EvidenceItem.render`.
        """
        items = self._items if limit is None else self._items[:limit]
        if not items:
            return "(no evidence was gathered)"
        blocks = [item.render(excerpt_chars=excerpt_chars) for item in items]
        if limit is not None and len(self._items) > limit:
            blocks.append(
                f"({len(self._items) - limit} further evidence entries are not shown here.)"
            )
        return "\n\n".join(blocks)


def _passage_summary(passage: dict[str, Any]) -> dict[str, Any]:
    text = passage.get("text") or ""
    summary = {"quoted_text": text}
    if passage.get("text_truncated"):
        summary["note"] = "the quoted text was shortened by the retrieval tool"
    return summary


def _summary_overflows(summary: dict[str, Any] | None, allowance: int) -> bool:
    return any(
        isinstance(value, str) and len(value) > allowance
        for value in (summary or {}).values()
    )


def _trim_summary(
    summary: dict[str, Any] | None, allowance: int
) -> dict[str, Any] | None:
    """Bound one entry's detail, keeping the shape readable.

    Trimming the *values* rather than the serialised result matters: half a JSON document is
    not JSON, and the model reading this has to be able to make sense of it. Whatever is cut
    is marked, so a shortened quote is never mistaken for the whole passage.
    """
    if not summary:
        return summary
    trimmed: dict[str, Any] = {}
    for key, value in summary.items():
        if isinstance(value, str) and len(value) > allowance:
            trimmed[key] = value[:allowance] + TRUNCATION_MARKER
        else:
            trimmed[key] = value
    return trimmed


def _compact(value: Any, *, depth: int = 0) -> str:
    """A short, readable rendering of a nested structure."""
    if isinstance(value, dict):
        if depth > 2:
            return "{...}"
        return "{" + ", ".join(f"{k}: {_compact(v, depth=depth + 1)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        if depth > 2:
            return "[...]"
        return "[" + ", ".join(_compact(v, depth=depth + 1) for v in value) + "]"
    if isinstance(value, str):
        return value
    return str(value)


__all__ = [
    "EvidenceItem",
    "EvidenceMap",
    "KIND_FILING_PASSAGE",
    "KIND_TOOL_RESULT",
    "MAX_ENTRIES",
    "MAX_PASSAGE_CHARS",
    "MAX_SUMMARY_CHARS",
]
