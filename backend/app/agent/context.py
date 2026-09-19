"""What one run is about: the question, and the one company and period it may act on.

Two rules shape this module, and both exist because the alternative is a model quietly
answering a different question than the one it was asked.

**Dates are resolved here, not by the model.** A model asked to work out "the last 30 days
before 2026-09-17" will usually get it right and will occasionally be confidently wrong, and a
price comparison over the wrong window looks exactly like one over the right window. So the
model returns a *token* -- `last_30_days` -- and the arithmetic happens in the table below,
against a reference date the caller supplied.

**The stored sample's dates are never substituted for the requested ones.** Our prices cover a
particular span; a question about "now" is not a question about that span. If the requested
window has little or nothing in it, the tools say so and the answer has to carry that
limitation. Widening the window to meet the data would turn a thin answer into a confident
wrong one.

`as_of` is the information cutoff -- what was knowable -- and it is deliberately a separate
field from the market window. A financial reporting period routinely *precedes* the window
being asked about (a Q2 figure reported in August, discussed in September), so the two are
never conflated and the financial-facts tool's period filters are never constrained by the
window.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

# The period tokens a routing decision may return. A closed set, so an unrecognised one is a
# validation failure rather than a silently ignored instruction.
PERIOD_NONE = "none"
PERIOD_EXPLICIT = "explicit"
PERIOD_LAST_7_DAYS = "last_7_days"
PERIOD_LAST_30_DAYS = "last_30_days"
PERIOD_LAST_90_DAYS = "last_90_days"
PERIOD_YEAR_TO_DATE = "year_to_date"
PERIOD_LAST_QUARTER = "last_quarter"

PERIOD_TOKENS: tuple[str, ...] = (
    PERIOD_NONE,
    PERIOD_EXPLICIT,
    PERIOD_LAST_7_DAYS,
    PERIOD_LAST_30_DAYS,
    PERIOD_LAST_90_DAYS,
    PERIOD_YEAR_TO_DATE,
    PERIOD_LAST_QUARTER,
)

_TRAILING_DAYS: Mapping[str, int] = {
    PERIOD_LAST_7_DAYS: 7,
    PERIOD_LAST_30_DAYS: 30,
    PERIOD_LAST_90_DAYS: 90,
}

# How the tokens are described to the model and to a reader of the result. One place, so the
# prompt and the README cannot describe the convention differently.
PERIOD_CONVENTIONS: Mapping[str, str] = {
    PERIOD_LAST_7_DAYS: "the 7 calendar days ending on the reference date, inclusive",
    PERIOD_LAST_30_DAYS: "the 30 calendar days ending on the reference date, inclusive",
    PERIOD_LAST_90_DAYS: "the 90 calendar days ending on the reference date, inclusive",
    PERIOD_YEAR_TO_DATE: "1 January of the reference date's year through the reference date",
    PERIOD_LAST_QUARTER: "the previous complete calendar quarter",
}


def resolve_period(token: str, reference_date: date) -> tuple[date, date] | None:
    """The window a period token means, or None when it means "no window".

    Raises `ValueError` for a token outside the closed set. That is a programming error --
    the routing model is validated against `PERIOD_TOKENS` before it gets here.
    """
    if token == PERIOD_NONE:
        return None

    if token in _TRAILING_DAYS:
        days = _TRAILING_DAYS[token]
        # Inclusive of the reference date, so "last 7 days" is 7 dates and not 8.
        return reference_date - timedelta(days=days - 1), reference_date

    if token == PERIOD_YEAR_TO_DATE:
        return date(reference_date.year, 1, 1), reference_date

    if token == PERIOD_LAST_QUARTER:
        return _previous_quarter(reference_date)

    raise ValueError(f"{token!r} is not a period token")


def _previous_quarter(reference_date: date) -> tuple[date, date]:
    """The complete calendar quarter before the one `reference_date` falls in."""
    current_quarter_start_month = 3 * ((reference_date.month - 1) // 3) + 1
    end_of_previous = date(reference_date.year, current_quarter_start_month, 1) - timedelta(
        days=1
    )
    start_month = 3 * ((end_of_previous.month - 1) // 3) + 1
    return date(end_of_previous.year, start_month, 1), end_of_previous


@dataclass(frozen=True)
class ExplicitArguments:
    """What the caller stated outright, which is authoritative over anything inferred."""

    symbol: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    as_of: date | None = None

    @property
    def any(self) -> bool:
        return any(
            value is not None
            for value in (self.symbol, self.start_date, self.end_date, self.as_of)
        )

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "start_date": _iso(self.start_date),
            "end_date": _iso(self.end_date),
            "as_of": _iso(self.as_of),
        }


@dataclass(frozen=True)
class ResolvedRequest:
    """The one company and the dates this run may act on.

    `start_date` and `end_date` are None when the question genuinely needs no market window --
    a filing question, a reported figure, a portfolio question. `as_of` is always set, because
    every tool that reads a filing needs to know what was knowable.
    """

    symbol: str
    start_date: date | None
    end_date: date | None
    as_of: date
    period: str

    @property
    def has_window(self) -> bool:
        return self.start_date is not None and self.end_date is not None

    @property
    def window_description(self) -> str:
        if not self.has_window:
            return "no market window (this question does not need one)"
        return f"{self.start_date.isoformat()} to {self.end_date.isoformat()}"

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "start_date": _iso(self.start_date),
            "end_date": _iso(self.end_date),
            "as_of": self.as_of.isoformat(),
            "period": self.period,
            "period_convention": PERIOD_CONVENTIONS.get(self.period),
        }


@dataclass(frozen=True)
class KnownSymbols:
    """Which tickers this system knows anything about, and in what sense.

    Two lists rather than one, and the difference is not cosmetic. `companies` is what has been
    **ingested** -- market data, insider filings, disclosures, financial facts. `holdings` is
    what the demo portfolio **owns**. They overlap but neither contains the other: MSFT is held
    and has never been ingested, so a question about owning MSFT is answerable while a question
    about MSFT's price is not.

    A single merged list would answer "do we know this symbol" correctly and mislead about
    everything else -- the Supervisor would be told MSFT has stored data, and an availability
    check built on the union alone could not tell a held symbol from an ingested one.
    """

    companies: tuple[str, ...] = ()
    holdings: tuple[str, ...] = ()

    @property
    def any(self) -> frozenset[str]:
        """Every symbol that is recognised at all, uppercased."""
        return frozenset(
            symbol.strip().upper()
            for symbol in (*self.companies, *self.holdings)
            if symbol and symbol.strip()
        )

    def recognises(self, symbol: str | None) -> bool:
        return bool(symbol) and symbol.strip().upper() in self.any  # type: ignore[union-attr]

    @property
    def held_only(self) -> tuple[str, ...]:
        """Held, and never ingested: portfolio questions only.

        The set that makes the two lists worth keeping apart. For these symbols this system
        knows what the portfolio owns and nothing else -- no prices, no insider filings, no
        disclosures, no financial facts -- so a question about how one of them has *moved* has
        no data behind it even though the ticker is recognised.
        """
        ingested = {symbol.strip().upper() for symbol in self.companies}
        return tuple(
            symbol for symbol in self.holdings if symbol.strip().upper() not in ingested
        )

    def describe_for_prompt(self) -> str:
        """The two lists, told apart, and what each one licenses.

        The third line is the one that matters. Two overlapping lists invite the reading that
        the union is the capability set, and it is not: being held says nothing about whether
        market, insider, financial-fact or filing data exists.
        """
        ingested = ", ".join(self.companies) if self.companies else "none"
        held = ", ".join(self.holdings) if self.holdings else "none"
        lines = [
            f"Companies with ingested data (market, insiders, financial facts, filing text): "
            f"{ingested}",
            f"Symbols held in the demo portfolio (ownership questions only): {held}",
        ]
        if self.held_only:
            lines.append(
                "  Of the held symbols, these are held but NOT ingested, so no market, "
                "insider, financial-fact or filing data exists for them: "
                + ", ".join(self.held_only)
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class SettledContext:
    """What earlier turns in a conversation settled, carried forward as a default.

    Deliberately *not* authoritative. The request's own arguments are the authoritative ones,
    the same way `--symbol` is on the command line; this is what the conversation has agreed so
    far, and a later turn naming a different company overrides it rather than colliding with it.
    """

    symbol: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    as_of: date | None = None

    @property
    def any(self) -> bool:
        return any(
            value is not None
            for value in (self.symbol, self.start_date, self.end_date, self.as_of)
        )

    def describe(self) -> str:
        if not self.any:
            return "nothing settled yet"
        window = (
            f"{self.start_date.isoformat()} to {self.end_date.isoformat()}"
            if self.start_date and self.end_date
            else "no market window"
        )
        return (
            f"company {self.symbol or 'not settled'}, window {window}, "
            f"information cutoff {self.as_of.isoformat() if self.as_of else 'not settled'}"
        )


@dataclass(frozen=True)
class PendingClarification:
    """A question the Supervisor asked, waiting for the user to answer it.

    `original_question` is the *analytical* question that could not be settled, not the reply.
    A reply of "August 6 through September 17" means nothing on its own; what it answers is the
    question before it, and resuming means putting the two back together.

    `reference_date` is the date that original question was asked against, kept so a reply
    arriving days later still resolves "last quarter" against the date it was actually asked
    about rather than silently sliding to today.
    """

    original_question: str
    reference_date: date
    asked_for: str | None = None

    def describe(self) -> str:
        lines = [
            f"An earlier turn asked: {self.original_question!r}",
            f"That question was asked against the reference date "
            f"{self.reference_date.isoformat()}.",
        ]
        if self.asked_for:
            lines.append(f"You asked the user for: {self.asked_for}")
        return "\n".join(lines)


@dataclass(frozen=True)
class PriorTurn:
    """One earlier exchange, clipped for the prompt.

    The answer is a *digest*, not the full prose and certainly not the tool payloads. What a
    follow-up needs is what was discussed; what it must not do is answer from it. Prior
    assistant prose is conversational context, never verified financial evidence.
    """

    user_message: str
    answer: str | None
    status: str


# How many earlier turns are carried, and how much of each answer. Both are deliberately small:
# the context rides in the routing request, which is sent on every turn, so every character here
# is paid for on every question. The full transcript stays available through pagination.
MAX_CONTEXT_TURNS = 6
MAX_CONTEXT_ANSWER_CHARS = 600


@dataclass(frozen=True)
class ConversationContext:
    """What the backend loaded from the conversation tables for this turn.

    Trusted because the backend built it from its own rows -- never from the request body.
    """

    settled: SettledContext | None = None
    pending: PendingClarification | None = None
    recent: tuple[PriorTurn, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.pending or (self.settled and self.settled.any) or self.recent)

    def describe_for_prompt(self) -> str:
        """The conversation so far, bounded, for the routing prompt.

        Assembled here rather than in the prompt strings so the bound is a property of the code
        that can be tested, not a sentence in a prompt that can be ignored.
        """
        if not self.any:
            return ""

        parts = ["This is a continuing conversation."]

        if self.settled and self.settled.any:
            parts.append(
                f"Already settled by earlier turns: {self.settled.describe()}. Carry these "
                "forward unless the user changes them."
            )

        if self.pending:
            parts.append(
                "The previous turn ended by asking the user something, and this message is "
                "their reply to it:\n" + self.pending.describe()
            )
            parts.append(
                "Resolve the ORIGINAL question above using this reply. Do not treat the reply "
                "as a new request on its own -- a bare date range means the dates for the "
                "question you asked about, not a question about dates."
            )

        if self.recent:
            clipped = self.recent[-MAX_CONTEXT_TURNS:]
            parts.append("The last few exchanges, most recent last:")
            for turn in clipped:
                answer = (turn.answer or "(no answer)").strip()
                if len(answer) > MAX_CONTEXT_ANSWER_CHARS:
                    answer = answer[:MAX_CONTEXT_ANSWER_CHARS] + " ... [shortened]"
                parts.append(
                    f"- User: {turn.user_message.strip()}\n"
                    f"  You replied ({turn.status}): {answer}"
                )
            parts.append(
                "That earlier prose is conversational context only. It is not evidence: it was "
                "not verified, and this run's citations must come from this run's tool results."
            )

        if not self.pending:
            parts.append(
                "If the user is correcting a company or a period, take the correction. If what "
                "they are asking about cannot be worked out from the above, ask rather than "
                "guess."
            )
        return "\n\n".join(parts)


@dataclass(frozen=True)
class RequestInputs:
    """What a run is asked, before anything about it has been settled.

    This is what the Supervisor is given: a question, the date to resolve relative periods
    against, whatever the caller stated outright, which symbols this system knows, and what
    earlier turns settled. The company and the period for *this* turn are produced by routing,
    so they are not here -- a field that is always empty would be worse than no field.
    """

    question: str
    reference_date: date
    explicit: ExplicitArguments
    known: KnownSymbols = KnownSymbols()
    conversation: ConversationContext | None = None

    def describe_for_prompt(self) -> str:
        lines = [f"Reference date: {self.reference_date.isoformat()}"]
        if self.explicit.any:
            lines.append(f"Arguments supplied by the caller: {self.explicit.as_dict()}")
        lines.append(self.known.describe_for_prompt())
        if self.conversation is not None and self.conversation.any:
            lines.append("")
            lines.append(self.conversation.describe_for_prompt())
        return "\n".join(lines)


@dataclass(frozen=True)
class RunContext:
    """Everything a run needs to know about what it was asked, once that is settled.

    Immutable, and created fresh for each run: nothing here can be carried from one run into
    the next, which is one of the ways runs are kept independent. A `RunContext` only exists
    after `resolve_request` has succeeded, so anything holding one is holding a request that
    was actually settled.
    """

    question: str
    reference_date: date
    resolved: ResolvedRequest
    explicit: ExplicitArguments
    # Which tickers this system knows, and in what sense. Given to the Supervisor so a question
    # about a company we hold nothing for is refused honestly instead of answered from model
    # memory, and checked in code before any answer is attempted.
    known: KnownSymbols = KnownSymbols()

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "reference_date": self.reference_date.isoformat(),
            "resolved": self.resolved.as_dict(),
            "explicit_arguments": self.explicit.as_dict(),
            "known_symbols": {
                "companies": list(self.known.companies),
                "holdings": list(self.known.holdings),
            },
        }

    def dates_for_prompt(self) -> str:
        """The two dates this run settled, labelled by what each one bounds.

        They are different claims and the difference is load-bearing. The market comparison
        period bounds the price comparison and the transactions it counts. The overall
        information date bounds what was knowable for the run as a whole, the filing discussion
        included -- and it is routinely *wider*, because a question asked today about a window
        that ended days ago was still asked today.

        A third date exists and is deliberately not listed here: the cutoff a tool reports
        inside its own payload, which is that tool's and narrower still. Stating it here would
        mean reading it back out of a payload, and the two values above are ones this
        application resolved itself.
        """
        window = (
            f"{self.resolved.start_date.isoformat()} to "
            f"{self.resolved.end_date.isoformat()}"
            if self.resolved.has_window
            else "none -- this question does not compare a market window"
        )
        return "\n".join(
            [
                "Dates this run distinguishes:",
                f"  Market comparison period: {window}",
                "    Bounds the price comparison and every transaction it counts.",
                f"  Overall information date (as_of): {self.resolved.as_of.isoformat()}",
                "    Bounds what was knowable for the whole run, including filing discussion.",
            ]
        )

    def describe_for_prompt(self) -> str:
        """The resolved request, stated plainly for the prompts.

        Written out rather than dumped as JSON because both prompts need to *read* it, and
        because the wording is where the "these are fixed, do not change them" instruction
        lives.
        """
        lines = [
            f"Reference date: {self.reference_date.isoformat()}",
            f"Company: {self.resolved.symbol}",
            self.dates_for_prompt(),
            self.known.describe_for_prompt(),
        ]
        return "\n".join(lines)


@dataclass(frozen=True)
class ResolutionOutcome:
    """Either the request this run may act on, or why it cannot be settled.

    A conflict is not an error. "The question says last quarter and you passed
    `--start-date 2026-08-06`" is a question only the person who typed it can answer, so it
    becomes a clarification rather than a guess in either direction.
    """

    resolved: ResolvedRequest | None
    conflict: str | None = None

    @property
    def settled(self) -> bool:
        return self.resolved is not None


def resolve_request(
    *,
    reference_date: date,
    explicit: ExplicitArguments,
    symbol: str | None,
    period: str,
    start_date: date | None = None,
    end_date: date | None = None,
    as_of: date | None = None,
) -> ResolutionOutcome:
    """Settle the company, the market window and the information cutoff for one run.

    Explicit arguments are authoritative. A routing decision that disagrees with them is a
    conflict, not something to reconcile silently in either direction.
    """
    if explicit.start_date is not None and explicit.end_date is None:
        return ResolutionOutcome(None, "an explicit start date was given without an end date")
    if explicit.end_date is not None and explicit.start_date is None:
        return ResolutionOutcome(None, "an explicit end date was given without a start date")

    # --- the company -----------------------------------------------------------------
    explicit_symbol = explicit.symbol.strip().upper() if explicit.symbol else None
    routed_symbol = symbol.strip().upper() if symbol else None

    if explicit_symbol and routed_symbol and explicit_symbol != routed_symbol:
        return ResolutionOutcome(
            None,
            f"you asked about {routed_symbol}, but this run was started for "
            f"{explicit_symbol}. One company per run -- say which one you want.",
        )
    resolved_symbol = explicit_symbol or routed_symbol
    if not resolved_symbol:
        return ResolutionOutcome(
            None,
            "which company should this be about? Name one ticker, for example NVDA.",
        )

    # --- the market window ------------------------------------------------------------
    if period not in PERIOD_TOKENS:
        raise ValueError(f"{period!r} is not a period token")

    if period == PERIOD_EXPLICIT:
        if start_date is None or end_date is None:
            return ResolutionOutcome(
                None, "an explicit period was named without both a start and an end date"
            )
        routed_window: tuple[date, date] | None = (start_date, end_date)
    else:
        routed_window = resolve_period(period, reference_date)

    if routed_window is not None and routed_window[0] > routed_window[1]:
        return ResolutionOutcome(
            None,
            f"the resolved period starts after it ends ({routed_window[0]} to "
            f"{routed_window[1]})",
        )

    explicit_window: tuple[date, date] | None = (
        (explicit.start_date, explicit.end_date)
        if explicit.start_date is not None and explicit.end_date is not None
        else None
    )

    if explicit_window is not None:
        # An explicit window is authoritative. A *different* window inferred from the
        # question is a conflict; the routing model returning no window at all is not.
        if routed_window is not None and routed_window != explicit_window:
            return ResolutionOutcome(
                None,
                f"the question implies {routed_window[0]} to {routed_window[1]}, but this "
                f"run was started for {explicit_window[0]} to {explicit_window[1]}. "
                "Which period should I use?",
            )
        window = explicit_window
    else:
        window = routed_window

    # --- the information cutoff --------------------------------------------------------
    if explicit.as_of is not None:
        if as_of is not None and as_of > explicit.as_of:
            return ResolutionOutcome(
                None,
                f"the question asks what was knowable on {as_of}, which is after the "
                f"cutoff this run was started with ({explicit.as_of}).",
            )
        resolved_as_of = explicit.as_of
    elif as_of is not None:
        resolved_as_of = as_of
    elif window is not None:
        resolved_as_of = window[1]
    else:
        # No market window: a question about a filing or a reported figure still has to say
        # what was knowable, and the reference date is the only honest answer.
        resolved_as_of = reference_date

    return ResolutionOutcome(
        ResolvedRequest(
            symbol=resolved_symbol,
            start_date=window[0] if window else None,
            end_date=window[1] if window else None,
            as_of=resolved_as_of,
            period=period,
        )
    )


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def known_symbols(session) -> KnownSymbols:
    """Which tickers this system knows, read from the two places that decide it.

    Read-only and deliberately small. Two reads rather than one, because they answer different
    questions: `companies` is what can be analysed, `holdings` is what can be asked about as
    owned. A symbol in either is one this system can say something about; a symbol in neither
    is one it must decline rather than guess at.

    Raises whatever SQLAlchemy raises if the database cannot be read. The caller turns that into
    a service failure -- it must never become "this company is not stored", which would state a
    confident falsehood about the data on the strength of an outage.
    """
    from sqlalchemy import select

    from app.models import Company, Holding

    companies = tuple(session.scalars(select(Company.ticker).order_by(Company.ticker)))
    holdings = tuple(
        session.scalars(select(Holding.symbol).distinct().order_by(Holding.symbol))
    )
    return KnownSymbols(companies=companies, holdings=holdings)


def describe_symbols(symbols: Sequence[str]) -> str:
    return ", ".join(symbols) if symbols else "none"


__all__ = [
    "ExplicitArguments",
    "ResolutionOutcome",
    "PERIOD_CONVENTIONS",
    "PERIOD_EXPLICIT",
    "PERIOD_LAST_7_DAYS",
    "PERIOD_LAST_30_DAYS",
    "PERIOD_LAST_90_DAYS",
    "PERIOD_LAST_QUARTER",
    "PERIOD_NONE",
    "PERIOD_TOKENS",
    "PERIOD_YEAR_TO_DATE",
    "RequestInputs",
    "ResolvedRequest",
    "RunContext",
    "ConversationContext",
    "KnownSymbols",
    "MAX_CONTEXT_ANSWER_CHARS",
    "MAX_CONTEXT_TURNS",
    "PendingClarification",
    "PriorTurn",
    "SettledContext",
    "describe_symbols",
    "known_symbols",
    "resolve_period",
    "resolve_request",
]
