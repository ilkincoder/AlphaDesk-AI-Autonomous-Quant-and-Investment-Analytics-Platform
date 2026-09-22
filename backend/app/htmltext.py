"""HTML to readable text, and finding the named sections inside it.

Pure: a string in, text and section spans out. No database, no network, no settings. It uses
the standard library's `html.parser`, so nothing new has to be installed.

**What comes out, and what does not.** A SEC filing is machine-generated HTML carrying three
kinds of content that are not prose:

* Layout scaffolding -- `<script>`, `<style>`, and elements hidden with `display:none`. NVDA's
  stored 10-K has 398 of the last kind.
* The XBRL context and unit declarations in `<ix:header>` and `<ix:hidden>`, which exist so
  the tagging works and are never meant to be read.
* Inline XBRL tags *around* numbers that are already displayed. Those must be kept: the tag
  carries the fact, but the number is visible text and dropping it would delete the figure
  from the document.

So the hidden sections are removed whole, while inline-XBRL text is left exactly where it
appears. The point is that a figure shows up once, not twice.

**Tables keep their shape.** Rows end with a newline and cells are separated by a tab, because
a financial table flattened into a single line of words is no longer a table and a number in it
can no longer be attributed to its column.

**Sections are located, not guessed at.** Every large filing states its Item headings twice:
once on the contents page and once where the section actually begins. A rule that takes the
first match takes the contents page. The real NVDA 10-K is unambiguous about this --
`Item 1A. Risk Factors` matches eight times, four of them inside 500 characters of each other
around offset 25,000. `_find_section_starts` therefore treats the contents page as what it is,
the densest cluster of headings in the document, and removes that cluster as a block before
choosing.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

# Bumped whenever the extraction changes in a way that would produce different text, so a
# stored document can be told apart from one processed by an earlier version.
EXTRACTION_VERSION = "1"

# Elements whose content is never readable text. `ix:hidden` and `ix:header` are the XBRL
# context and unit blocks; the rest are ordinary web page furniture.
_UNREADABLE_TAGS = frozenset(
    {"script", "style", "ix:hidden", "ix:header", "head", "title", "meta", "link"}
)

# Elements that never have a closing tag, so they must not be counted as opening one.
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

# Where readable content ends. Each closing tag below emits a boundary, so paragraph and row
# structure survives into the text.
_BLOCK_END_TAGS = frozenset(
    {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
)
_CELL_END_TAGS = frozenset({"td", "th"})
_LINE_BREAK_TAGS = frozenset({"br"})

_DISPLAY_NONE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
# Tabs are excluded alongside newlines: they are the column separator, and collapsing them
# to spaces would flatten every financial table into an unreadable run of words.
_INLINE_WHITESPACE = re.compile(r"[^\S\n\t]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# How close two section headings must be to count as neighbours. The contents page packs its
# entries within a few hundred characters of each other; real sections are thousands apart.
# Tuned against the stored NVDA 10-K and 10-Q, and pinned by a test named for that.
_TOC_NEIGHBOUR_WINDOW = 800

# A heading needs at least this many neighbours before its cluster is treated as a contents
# page. Two adjacent headings are a coincidence; four are a table of contents.
_TOC_MIN_NEIGHBOURS = 2


@dataclass(frozen=True)
class SectionPattern:
    """A named section and the heading that introduces it."""

    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class SectionSpan:
    """Where a section's heading was found, as an offset into the extracted text."""

    name: str
    start: int
    heading: str

    @property
    def end(self) -> int | None:
        """Not known: sections run to the next one, which the caller can work out."""
        return None


@dataclass(frozen=True)
class ExtractedDocument:
    """The readable text, the sections found in it, and what could not be determined."""

    text: str
    sections: Mapping[str, SectionSpan]
    limitations: tuple[str, ...]
    extraction_version: str = EXTRACTION_VERSION


# Heading patterns per form type. Deliberately narrow: matching the wrong heading is worse
# than matching none, because an undetected section is reported while a wrong one is not.
_COMMON_SECTIONS = (
    SectionPattern("risk_factors", re.compile(r"Item\s*1A\.\s*Risk\s*Factors", re.I)),
)

SECTIONS_BY_FORM: Mapping[str, Sequence[SectionPattern]] = {
    "10-K": (
        SectionPattern("business", re.compile(r"Item\s*1\.\s*Business\b", re.I)),
        *_COMMON_SECTIONS,
        SectionPattern(
            "management_discussion",
            re.compile(r"Item\s*7\.\s*Management.{0,40}?Discussion", re.I),
        ),
        SectionPattern(
            "financial_statements",
            re.compile(r"Item\s*8\.\s*Financial\s*Statements", re.I),
        ),
    ),
    "10-Q": (
        SectionPattern(
            "financial_statements",
            re.compile(r"Item\s*1\.\s*Financial\s*Statements", re.I),
        ),
        SectionPattern(
            "management_discussion",
            re.compile(r"Item\s*2\.\s*Management.{0,40}?Discussion", re.I),
        ),
        *_COMMON_SECTIONS,
    ),
}


def extract(html: str, *, form_type: str) -> ExtractedDocument:
    """Turn a filing's HTML into readable text and locate its named sections.

    `form_type` selects which headings to look for; an unknown form type yields no sections
    and says so, rather than searching for headings that do not belong to it.
    """
    limitations: list[str] = []

    if not html or not html.strip():
        return ExtractedDocument(
            text="",
            sections={},
            limitations=("The document was empty, so there was no text to extract.",),
        )

    text = to_text(html)
    if not text.strip():
        return ExtractedDocument(
            text=text,
            sections={},
            limitations=(
                "No readable text survived extraction. The document may be a scan or an "
                "image-based rendering rather than marked-up text.",
            ),
        )

    patterns = SECTIONS_BY_FORM.get(form_type.strip().upper())
    if patterns is None:
        limitations.append(
            f"No section patterns are defined for form type {form_type!r}, so no sections "
            "were looked for. The full extracted text is stored."
        )
        return ExtractedDocument(text=text, sections={}, limitations=tuple(limitations))

    sections = _find_sections(text, patterns)

    missing = [pattern.name for pattern in patterns if pattern.name not in sections]
    if missing:
        limitations.append(
            "These sections could not be located and are not claimed to be present: "
            f"{', '.join(sorted(missing))}. The full extracted text is stored, so nothing "
            "was discarded."
        )
    limitations.append(
        "Section positions are approximate: they mark where a heading was found, not a "
        "verified span. A heading inside a table of contents that survived detection would "
        "place a section too early."
    )

    return ExtractedDocument(text=text, sections=sections, limitations=tuple(limitations))


def to_text(html: str) -> str:
    """Readable text from markup, with tags removed and structure kept.

    The general-purpose half of this module: `extract` calls it to get a filing's text, and
    the news ingestion calls it on a release page or a feed summary. Both want the same
    thing -- the words, with scripts and styles gone and paragraph and table structure
    intact -- because text that still contains markup is not text, and text with its rows
    flattened cannot say which column a number was in.

    Input is untrusted in both cases, and is treated as text throughout: nothing here
    evaluates, resolves or fetches anything the markup refers to.
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return _tidy("".join(parser.chunks))


def _tidy(raw: str) -> str:
    """Collapse whitespace without destroying line or cell structure.

    A trailing tab is trimmed because every cell ends with one, so the last cell of every
    row would otherwise leave one behind. A *leading* tab is kept: it is an empty first
    cell, and in a financial table that is a column that exists.
    """
    lines = [
        _INLINE_WHITESPACE.sub(" ", line).strip(" ").rstrip("\t")
        for line in raw.split("\n")
    ]
    text = "\n".join(lines)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


class _TextExtractor(HTMLParser):
    """Accumulates readable text, skipping anything that is not meant to be read."""

    def __init__(self) -> None:
        # convert_charrefs resolves entities in handle_data, so `&amp;` and `&#8217;`
        # arrive already turned into `&` and the right single quote.
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._depth = 0
        # The element depth at which the current unreadable element opened, or None when
        # everything being read is readable.
        self._suppressed_from: int | None = None

    # -- element boundaries ------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _VOID_TAGS:
            if self._suppressed_from is None and tag in _LINE_BREAK_TAGS:
                self.chunks.append("\n")
            return

        if self._suppressed_from is None and self._is_unreadable(tag, attrs):
            self._suppressed_from = self._depth

        self._depth += 1

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _LINE_BREAK_TAGS and self._suppressed_from is None:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        self._depth -= 1

        if self._suppressed_from is not None:
            if self._depth <= self._suppressed_from:
                self._suppressed_from = None
            return

        if tag in _BLOCK_END_TAGS:
            self.chunks.append("\n")
        elif tag in _CELL_END_TAGS:
            # A tab, not a space: it is what keeps a table's columns apart so a number can
            # still be attributed to its column.
            self.chunks.append("\t")

    # -- text --------------------------------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._suppressed_from is not None or not data:
            return
        if data.strip():
            self.chunks.append(data)
        elif self.chunks and not self.chunks[-1].endswith((" ", "\n", "\t")):
            # A run of whitespace between two words is a word boundary and has to survive.
            self.chunks.append(" ")

    @staticmethod
    def _is_unreadable(tag: str, attrs) -> bool:
        if tag in _UNREADABLE_TAGS:
            return True
        style = ""
        for name, value in attrs:
            if name == "style" and value:
                style = value
                break
        return bool(_DISPLAY_NONE.search(style))


def _find_sections(
    text: str, patterns: Sequence[SectionPattern]
) -> Mapping[str, SectionSpan]:
    """Locate each section's heading, ignoring the contents page it also appears on."""
    candidates: list[tuple[int, SectionPattern, str]] = []
    for pattern in patterns:
        for match in pattern.pattern.finditer(text):
            candidates.append((match.start(), pattern, match.group(0)))

    if not candidates:
        return {}

    offsets = [start for start, _, _ in candidates]
    suppressed = _contents_page_offsets(offsets)

    chosen: dict[str, SectionSpan] = {}
    for start, pattern, heading in sorted(candidates, key=lambda item: item[0]):
        if start in suppressed or pattern.name in chosen:
            continue
        chosen[pattern.name] = SectionSpan(
            name=pattern.name, start=start, heading=" ".join(heading.split())
        )
    return chosen


def _contents_page_offsets(offsets: Sequence[int]) -> frozenset[int]:
    """Offsets belonging to the densest cluster of headings -- the contents page.

    A filing states every Item heading twice: once in its table of contents and once where
    the section starts. The contents page is recognisable as the place where several
    headings sit within a few hundred characters of each other, which real sections never do.
    The densest such cluster is removed as a single block.

    Returns an empty set when nothing is clustered, which is the right answer for a form
    that has no contents page at all.
    """
    neighbours = {
        offset: sum(
            1
            for other in offsets
            if other != offset and abs(other - offset) <= _TOC_NEIGHBOUR_WINDOW
        )
        for offset in offsets
    }

    anchor_count = max(neighbours.values(), default=0)
    if anchor_count < _TOC_MIN_NEIGHBOURS:
        return frozenset()

    anchor = min(
        offset for offset, count in neighbours.items() if count == anchor_count
    )
    return frozenset(
        offset
        for offset in offsets
        if abs(offset - anchor) <= _TOC_NEIGHBOUR_WINDOW
    )
