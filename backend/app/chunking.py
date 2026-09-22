"""Turning a filing's extracted text into embeddable passages. Pure.

Text, section boundaries and a token counter go in; chunks come out. No database, no network,
no model. The counter is a `Protocol`, so a test supplies a deterministic double and
production supplies the model's own tokenizer.

**Counting is a measurement, and it has to be an honest one.** FastEmbed's tokenizer is
configured to truncate at 512 tokens, so `len(tokenizer.encode(anything_long).ids)` returns
exactly 512 — it would report every over-long passage as fitting perfectly. The counter used
here must therefore be one with truncation disabled, or the limit below is unenforceable
rather than merely unchecked. `app.embeddings` builds that counter.

Why it matters: the model embeds what it is given, up to 512 tokens. A 900-token passage would
be embedded as its first 512 while the stored text claimed all 900, and nothing anywhere would
say so. The passage a reader sees and the passage the vector represents have to be the same
one.

**Offsets are exact.** Every chunk records the character range it came from, and
`text[start:end]` is the passage. The heading is prepended for embedding only, and is
bracketed, so it cannot be read as something the filing actually said.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

# Bumped whenever a change here would produce different chunks for the same text. Stored on
# every chunk and compared at index time, so a stale index is detected rather than mixed.
CHUNKING_VERSION = "1"

# The model reads at most 512 tokens. Nothing downstream truncates in a way we would notice,
# so this is the ceiling the chunker guarantees.
MAX_TOKENS = 512

# Aim for the middle of the 350-450 range. The soft limit is where a chunk stops growing; the
# hard limit is where a passage is split whether it wants to be or not.
TARGET_TOKENS = 400
HARD_LIMIT_TOKENS = MAX_TOKENS

# A paragraph at most this long may be repeated at the start of the next chunk. Bounded so
# overlap can never crowd out the passage it is meant to help.
OVERLAP_MAX_TOKENS = 100

# Blank lines separate paragraphs in the extracted text; a single newline is a line break
# inside one. That distinction is what keeps table rows together.
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
# Sentence ends, used only to split a paragraph too long to keep whole. Requiring whitespace
# after the punctuation means "1.5" is not treated as a sentence boundary.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


class TokenCounter(Protocol):
    """Counts the tokens a model will receive for a piece of text, specials included.

    Implementations must not truncate. A counter that capped its answer would make every
    limit in this module unenforceable while appearing to work.
    """

    def count_tokens(self, text: str) -> int: ...


@dataclass(frozen=True)
class SectionBoundary:
    """Where a named section starts, as an offset into the extracted text."""

    name: str
    start: int


@dataclass(frozen=True)
class Chunk:
    """One passage, with everything needed to embed it and to cite it.

    `text` and the offsets describe the filing. `embedding_input` is what goes to the model:
    the same passage with a bracketed heading in front, which is never part of `text` and so
    can never be mistaken for the filing's own words.
    """

    text: str
    start: int
    end: int
    section: str
    heading: str
    embedding_input: str
    token_count: int
    chunking_version: str = CHUNKING_VERSION


@dataclass(frozen=True)
class _Span:
    start: int
    end: int


def chunk_document(
    text: str,
    *,
    form_type: str,
    sections: Sequence[SectionBoundary] = (),
    counter: TokenCounter,
    target_tokens: int = TARGET_TOKENS,
    max_tokens: int = HARD_LIMIT_TOKENS,
    heading_label: str | None = None,
) -> tuple[Chunk, ...]:
    """Split `text` into embeddable passages, in document order.

    `sections` may be empty, and usually is for an 8-K or an exhibit. Passages outside every
    known section are labelled `unknown`, which reports a limit of detection rather than
    claiming the filing has no structure.

    `heading_label` replaces that generated heading entirely, for a corpus whose text is not
    a filing and has no sections to name. News passes something like `news | AAPL, MSFT`.
    The default is None, which produces exactly the heading this function produced before
    the parameter existed -- so every stored SEC chunk is unaffected and `CHUNKING_VERSION`
    does not move.
    """
    if not text or not text.strip():
        return ()

    ordered = sorted(sections, key=lambda item: item.start)

    def heading_for(offset: int) -> str:
        if heading_label is not None:
            return _bracket(heading_label)
        return _heading(form_type, _section_at(offset, ordered))

    def measure(span: _Span) -> int:
        """Tokens for a span exactly as the model would receive it.

        The heading is included, which matters more than it looks: everything below bounds a
        span against a token limit, and a bound applied to the passage alone would let the
        heading push the finished input past 512.
        """
        heading = heading_for(span.start)
        return counter.count_tokens(_embedding_input(heading, text[span.start : span.end]))

    pieces: list[_Span] = []
    for paragraph in _paragraphs(text):
        pieces.extend(_split_if_too_long(text, paragraph, measure, max_tokens))

    spans = _group(pieces, measure, target_tokens)

    chunks: list[Chunk] = []
    for index, span in enumerate(spans):
        if index > 0:
            carry = _overlap(text, spans[index - 1], counter)
            if carry is not None:
                candidate = _joined(carry, span)
                if measure(candidate) <= max_tokens:
                    span = candidate

        heading = heading_for(span.start)
        passage = text[span.start : span.end]
        embedding_input = _embedding_input(heading, passage)
        measured = counter.count_tokens(embedding_input)

        if measured > max_tokens:
            # Unreachable if the splitting above is correct, and loud if it ever is not:
            # emitting this would mean the vector represented less than the text claims.
            raise ValueError(
                f"a {measured}-token passage exceeds the {max_tokens}-token limit and "
                "could not be split further; the model would silently truncate it"
            )

        chunks.append(
            Chunk(
                text=passage,
                start=span.start,
                end=span.end,
                section=_section_at(span.start, ordered),
                heading=heading,
                embedding_input=embedding_input,
                token_count=measured,
            )
        )

    return tuple(chunks)


# --- grouping --------------------------------------------------------------------------------


def _group(
    pieces: Sequence[_Span], measure: Callable[[_Span], int], target_tokens: int
) -> list[_Span]:
    """Greedily pack pieces into spans that stay within the target."""
    spans: list[_Span] = []
    current: _Span | None = None

    for piece in pieces:
        if current is None:
            current = piece
            continue
        merged = _joined(current, piece)
        if measure(merged) <= target_tokens:
            current = merged
        else:
            spans.append(current)
            current = piece

    if current is not None:
        spans.append(current)
    return spans


def _overlap(text: str, previous: _Span, counter: TokenCounter) -> _Span | None:
    """The previous span's last paragraph, when it is small enough to repeat.

    Returns None when the previous span has only one paragraph or its last one is too long —
    in which case the boundary simply has no overlap, which is the honest default.
    """
    paragraphs = [
        paragraph
        for paragraph in _paragraphs(text[previous.start : previous.end])
        if paragraph.end > paragraph.start
    ]
    if len(paragraphs) < 2:
        return None

    last = paragraphs[-1]
    rebased = _Span(
        start=previous.start + last.start, end=previous.start + last.end
    )
    if counter.count_tokens(text[rebased.start : rebased.end]) > OVERLAP_MAX_TOKENS:
        return None
    return rebased


# --- splitting ---------------------------------------------------------------------------------


def _paragraphs(text: str) -> list[_Span]:
    """Non-overlapping paragraph spans, in order, trimmed of surrounding whitespace."""
    spans: list[_Span] = []
    position = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        spans.append(_trim(text, position, match.start()))
        position = match.end()
    spans.append(_trim(text, position, len(text)))
    return [span for span in spans if span.end > span.start]


def _split_if_too_long(
    text: str, span: _Span, measure: Callable[[_Span], int], max_tokens: int
) -> list[_Span]:
    """Break up a paragraph that cannot be embedded whole.

    Tables first, split at row boundaries, because a financial table cut mid-row loses the
    association between a number and its column. Then sentences. Only a single sentence
    longer than the limit is hard-split, and then only because the alternative is handing the
    model something it will silently truncate.
    """
    if measure(span) <= max_tokens:
        return [span]

    lines = _split_on(text, span, "\n")
    if len(lines) > 1 and any("\t" in text[line.start : line.end] for line in lines):
        return _pack(text, lines, measure, max_tokens)

    sentences = _split_sentences(text, span)
    if len(sentences) > 1:
        return _pack(text, sentences, measure, max_tokens)

    return _hard_split(text, span, measure, max_tokens)


def _pack(
    text: str, pieces: Sequence[_Span], measure: Callable[[_Span], int], max_tokens: int
) -> list[_Span]:
    """Group small pieces back up to the limit, hard-splitting any that still exceed it."""
    packed: list[_Span] = []
    current: _Span | None = None

    for piece in pieces:
        if current is None:
            current = piece
            continue
        merged = _joined(current, piece)
        if measure(merged) <= max_tokens:
            current = merged
        else:
            packed.append(current)
            current = piece
    if current is not None:
        packed.append(current)

    result: list[_Span] = []
    for span in packed:
        if measure(span) > max_tokens:
            result.extend(_hard_split(text, span, measure, max_tokens))
        else:
            result.append(span)
    return result


def _hard_split(
    text: str, span: _Span, measure: Callable[[_Span], int], max_tokens: int
) -> list[_Span]:
    """Last resort for one unbroken run longer than the limit.

    Finds the longest prefix that fits by binary search, backs off to the last space so a word
    is not cut in half, and repeats on what is left. Measured throughout rather than estimated.
    """
    spans: list[_Span] = []
    position = span.start

    while position < span.end:
        end = _longest_prefix(text, position, span.end, measure, max_tokens)
        if end <= position:
            # The limit is smaller than a single character can be. Refusing to loop forever
            # matters more than the outcome here, and the caller's check will catch it.
            end = min(position + 1, span.end)
        trimmed = _trim(text, position, end)
        if trimmed.end > trimmed.start:
            spans.append(trimmed)
        position = end

    return spans


def _longest_prefix(
    text: str, start: int, end: int, measure: Callable[[_Span], int], max_tokens: int
) -> int:
    """The furthest offset such that `text[start:offset]` fits, preferring a word boundary."""
    low, high, best = start + 1, end, start
    while low <= high:
        middle = (low + high) // 2
        if measure(_Span(start=start, end=middle)) <= max_tokens:
            best = middle
            low = middle + 1
        else:
            high = middle - 1

    if best <= start:
        return start
    if best < end:
        space = text.rfind(" ", start, best)
        if space > start:
            return space
    return best


def _split_sentences(text: str, span: _Span) -> list[_Span]:
    body = text[span.start : span.end]
    spans: list[_Span] = []
    position = 0
    for match in _SENTENCE_BREAK.finditer(body):
        # Keep the terminating punctuation with the sentence it ends.
        spans.append(_Span(start=position, end=match.start() + 1))
        position = match.end()
    spans.append(_Span(start=position, end=len(body)))
    return _rebased_spans(text, span, spans)


def _split_on(text: str, span: _Span, separator: str) -> list[_Span]:
    body = text[span.start : span.end]
    spans: list[_Span] = []
    position = 0
    for part in body.split(separator):
        spans.append(_Span(start=position, end=position + len(part)))
        position += len(part) + len(separator)
    return _rebased_spans(text, span, spans)


def _rebased_spans(text: str, parent: _Span, spans: Sequence[_Span]) -> list[_Span]:
    """Trim each span against the document and lift it back to document offsets."""
    trimmed = []
    for span in spans:
        absolute = _Span(start=parent.start + span.start, end=parent.start + span.end)
        clean = _trim(text, absolute.start, absolute.end)
        if clean.end > clean.start:
            trimmed.append(clean)
    return trimmed


def _trim(text: str, start: int, end: int) -> _Span:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return _Span(start=start, end=end)


def _joined(first: _Span, second: _Span) -> _Span:
    return _Span(start=min(first.start, second.start), end=max(first.end, second.end))


# --- labelling ----------------------------------------------------------------------------------


def _heading(form_type: str, section: str) -> str:
    return _bracket(f"{form_type} | section: {section}")


def _bracket(label: str) -> str:
    """The one place a heading is wrapped, so both callers bracket identically.

    Bracketing is not decoration: the heading is prepended for the model to read and is
    never part of the passage, and the brackets are what stop it being mistaken for
    something the document actually said.
    """
    return f"[{label}]"


def _embedding_input(heading: str, passage: str) -> str:
    return f"{heading}\n\n{passage}"


def _section_at(offset: int, sections: Sequence[SectionBoundary]) -> str:
    """The last section to have started at or before `offset`, or `unknown`."""
    name = "unknown"
    for section in sections:
        if section.start <= offset:
            name = section.name
        else:
            break
    return name
