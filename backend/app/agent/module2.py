"""The Module 2 workflow: news evidence in, a priced proposal out.

Three nodes and a hard line between them.

**The News Agent is not a model.** Retrieving relevant articles is a similarity search over an
index this application built, with queries built from the portfolio's own symbols. Spending a
model call to decide *what to search for* -- or worse, to decide *what the news says* -- would
add a cost and a failure mode to a step that is already deterministic, and would put prose
between the reader and the article it came from. What this node produces is a list of articles
with their ids, publishers, URLs and publication dates, and that list is the only thing anything
downstream may cite.

**The Portfolio Agent proposes weights and reasons. It computes nothing.** It is shown the
frozen portfolio and the retrieved articles, and it returns a target weight per existing holding
plus a rationale. Every share count, every value and every total is produced afterwards by
`app.rebalance`, from those weights. A model writing a figure is a model writing a figure nobody
can check, so the reply's numbers are the only ones it is allowed to have, and they are validated
before they are used.

**An unusable reply is refused, not repaired.** Weights for a symbol that is not held, weights
outside 0..1, a missing symbol, a citation to an article that was not retrieved: each of those
ends the run with `unavailable` rather than producing a proposal that looks actionable. The one
retry exists because a malformed JSON object is a correctable mistake; a second failure is not
correctable by asking again, and asking a third time would only cost more to learn the same
thing.

**Nothing retrieved is instruction.** News text is written by publishers, stored as text, and
reproduced as text. A sentence in an article that tells the model to do something is a quotation
-- there is no code path from a retrieved passage to a decision, and the only thing a passage can
influence is which reference the rationale cites.
"""

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.agent.budget import BudgetExhausted, RunBudget
from app.agent.llm import ModelClient
from app.agent.progress import (
    KIND_STAGE,
    NULL_PROGRESS,
    Progress,
    ProgressEvent,
)
from app.agent.prompts import MODULE2_PROMPT
from app.news import excerpt
from app.news_index import STATUS_OK, search_news
from app.rebalance import (
    POLICY_CONSTRAINTS,
    POLICY_DESCRIPTION,
    POLICY_NAME,
    SCENARIO_DESCRIPTION,
    SCENARIO_SHOCK_PERCENT,
    Rebalance,
    RebalanceUnavailable,
    calculate_rebalance,
)
from app.rebalance_snapshot import Snapshot

logger = logging.getLogger(__name__)

# --- the stages a run actually has --------------------------------------------------------

STAGE_RETRIEVING = "retrieving_news"
STAGE_PROPOSING = "proposing_targets"
STAGE_CALCULATING = "calculating"

# Why no proposal was produced. Machine-readable, because some of these are conditions a reader
# fixes (no news stored) and some are not (the model would not produce a usable reply).
REASON_NO_EVIDENCE = "no_evidence"
REASON_INVALID_OUTPUT = "invalid_agent_output"
REASON_UNVERIFIABLE_CITATIONS = "unverifiable_citations"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"

# The status this workflow reports when it could not produce a proposal. Distinct from the two
# outcomes `app.rebalance` produces, which are both answers.
STATUS_UNAVAILABLE = "unavailable"

# --- how much is retrieved, and how much of it reaches the model ---------------------------

# The queries are built from the portfolio's own symbols rather than written by a model. Each is
# deliberately plain: this is a similarity search over stored passages, and a query the model
# worded would be one more thing that varies between two runs of the same portfolio.
COMPANY_QUERY = "{symbol} company news and share price"
MACRO_QUERY = "interest rates inflation employment and the economic outlook"

# Bounded on every axis. A portfolio of fifty symbols must not become fifty searches, and the
# model must not be handed a document.
MAX_SYMBOLS_RETRIEVED = 6
PER_SYMBOL_TOP_K = 3
MACRO_TOP_K = 5
MAX_ARTICLES = 12

# How recent an article has to be to count as current news rather than as older context. Not a
# filter: nothing is dropped for being old, because old macro context is exactly what a
# rate-sensitive portfolio needs. It is a label, and the model is required to use it.
RECENT_WINDOW_DAYS = 30

# How much of a passage the prompt carries. The *article* is what a citation points at, so the
# excerpt is there to tell the model whether the article is relevant, not to be quoted -- the
# article's own page is one link away and is the thing a reader should be sent to.
MAX_EVIDENCE_CHARS = 12000
EVIDENCE_EXCERPT_CHARS = 400

# A malformed reply is retried once. A second failure is not a mistake a third request fixes.
PROPOSAL_ATTEMPTS = 2

_NUMBERED_REFERENCE = re.compile(r"\bN\d+\b")


@dataclass(frozen=True)
class RetrievedArticle:
    """One article the run retrieved, with everything a citation needs.

    Read out of the retrieval result and the stored row behind it -- never out of model output.
    The reference is assigned here, by the application, so a citation in the rationale can be
    resolved to an article or rejected; there is no third possibility.
    """

    reference: str
    article_id: int
    title: str
    publisher: str
    url: str
    published_at: datetime
    category: str
    symbols: tuple[str, ...]
    excerpt: str
    similarity: float
    is_macro: bool
    recent: bool

    def as_dict(self) -> dict:
        return {
            "reference": self.reference,
            "article_id": self.article_id,
            "title": self.title,
            "publisher": self.publisher,
            "url": self.url,
            "published_at": self.published_at.isoformat(),
            "category": self.category,
            "symbols": list(self.symbols),
            "similarity": self.similarity,
            "recent": self.recent,
        }

    def render(self) -> str:
        age = "recent" if self.recent else f"older than {RECENT_WINDOW_DAYS} days"
        kind = "macro release" if self.is_macro else "company news"
        symbols = ", ".join(self.symbols) if self.symbols else "no symbols"
        return (
            f"[{self.reference}] {self.title}\n"
            f"  publisher: {self.publisher} | published: "
            f"{self.published_at.date().isoformat()} ({age}) | {kind} | {symbols}\n"
            f"  similarity (not a probability): {self.similarity:.3f}\n"
            f"  {self.excerpt}"
        )


@dataclass
class NewsEvidence:
    """What the News Agent retrieved, and what it could not.

    Mutable only while it is being built. `warnings` is not decoration: a source that could not
    be searched, a query that matched nothing and an index that does not exist are all facts the
    rationale has to be read against, and they travel to the proposal's limitations whether or
    not the model mentions them.
    """

    articles: list[RetrievedArticle] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    _by_reference: dict[str, RetrievedArticle] = field(default_factory=dict)
    _by_article: dict[int, RetrievedArticle] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.articles)

    def add(self, article: RetrievedArticle) -> str | None:
        """Add one article, deduplicated by its id. Returns its reference, or None if it was
        already there -- the same story retrieved by two queries is one piece of evidence."""
        existing = self._by_article.get(article.article_id)
        if existing is not None:
            return existing.reference
        if len(self.articles) >= MAX_ARTICLES:
            return None

        reference = f"N{len(self.articles) + 1}"
        stored = replace(article, reference=reference)
        self.articles.append(stored)
        self._by_reference[reference] = stored
        self._by_article[article.article_id] = stored
        return reference

    def get(self, reference: str) -> RetrievedArticle | None:
        return self._by_reference.get(reference)

    def references(self) -> list[str]:
        return [item.reference for item in self.articles]

    def validate(self, references: Sequence[str]) -> tuple[list[str], list[str]]:
        """Split cited references into the ones that resolve and the ones that do not.

        The same contract as the Module 1 evidence map: a reference that does not resolve is an
        invented citation, and an answer resting on one is withheld rather than shown.
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

    def as_list(self) -> list[dict]:
        return [item.as_dict() for item in self.articles]

    def render(self, *, limit: int = MAX_ARTICLES) -> str:
        if not self.articles:
            return "(no articles were retrieved)"
        blocks = [item.render() for item in self.articles[:limit]]
        if len(self.articles) > limit:
            blocks.append(
                f"({len(self.articles) - limit} further retrieved articles are not shown "
                "here. They are not evidence you may cite.)"
            )
        return "\n\n".join(blocks)

    @classmethod
    def from_state(cls, payload: Mapping[str, Any]) -> "NewsEvidence":
        """Rebuild the evidence a completed retrieve stage wrote.

        A resumed run must not search the index again: the articles the original retrieved are
        the evidence its targets were reasoned from, and retrieving a second time could return a
        different set -- which would put a citation check against articles the model never saw.
        The stored articles carry everything a `RetrievedArticle` needs, so this is a
        reconstruction rather than a re-query.
        """
        evidence = cls(warnings=list(payload.get("warnings") or []))
        evidence.queries = list(payload.get("queries") or [])
        for item in payload.get("articles") or []:
            evidence.add(
                RetrievedArticle(
                    reference="",
                    article_id=int(item["article_id"]),
                    title=str(item["title"]),
                    publisher=str(item["publisher"]),
                    url=str(item["url"]),
                    published_at=datetime.fromisoformat(str(item["published_at"])),
                    category=str(item["category"]),
                    symbols=tuple(item.get("symbols") or ()),
                    excerpt=str(item.get("excerpt", "")),
                    similarity=float(item.get("similarity", 0.0)),
                    is_macro=str(item["category"]) == "macro",
                    recent=bool(item.get("recent", True)),
                )
            )
        return evidence

    @property
    def older_count(self) -> int:
        return sum(1 for item in self.articles if not item.recent)


def retrieve_news(
    session: Any,
    symbols: Sequence[str],
    *,
    store: Any,
    embedder: Any,
    now: datetime | None = None,
) -> NewsEvidence:
    """Retrieve company and macro articles for this portfolio.

    Two kinds of query, kept apart on purpose. Company news is searched per held symbol and
    filtered to it, so a passage about a company the portfolio does not hold cannot take one of
    the places. Macro releases carry no symbols at all -- that is a fact about them, not missing
    data -- so they are searched as their own category, which is the only way to reach them.

    Every failure is a warning rather than an exception. An index that is down, a source that
    matched nothing and a model that is unavailable are all conditions a reader has to be told
    about, and none of them is a reason to lose the retrieval that did work.
    """
    retrieved_at = now or datetime.now(timezone.utc)
    evidence = NewsEvidence()

    for symbol in list(symbols)[:MAX_SYMBOLS_RETRIEVED]:
        query = COMPANY_QUERY.format(symbol=symbol)
        evidence.queries.append(query)
        result = _search(session, query, store=store, embedder=embedder, symbols=[symbol])
        _absorb(evidence, result, retrieved_at, label=f"company news for {symbol}")

    evidence.queries.append(MACRO_QUERY)
    macro = _search(session, MACRO_QUERY, store=store, embedder=embedder, category="macro")
    _absorb(evidence, macro, retrieved_at, label="macro releases", macro=True)

    if not evidence.articles:
        evidence.warnings.append(
            "No article was retrieved for this portfolio. A proposal with no evidence behind "
            "it cannot be produced, and none was."
        )
    if evidence.older_count:
        evidence.warnings.append(
            f"{evidence.older_count} of the {len(evidence)} retrieved article(s) were published "
            f"more than {RECENT_WINDOW_DAYS} days ago and are older context rather than recent "
            "news."
        )
    return evidence


def _search(
    session: Any,
    query: str,
    *,
    store: Any,
    embedder: Any,
    symbols: Sequence[str] = (),
    category: str | None = None,
) -> Any:
    return search_news(
        session,
        query=query,
        top_k=PER_SYMBOL_TOP_K if symbols else MACRO_TOP_K,
        store=store,
        embedder=embedder,
        category=category,
        symbols=symbols,
    )


def _absorb(
    evidence: NewsEvidence, result: Any, retrieved_at: datetime, *, label: str, macro: bool = False
) -> None:
    """Fold one search result into the evidence, warnings included."""
    if result.status != STATUS_OK:
        evidence.warnings.append(
            f"The {label} search returned nothing usable ({result.status}"
            + (f": {result.reason}" if result.reason else "")
            + ")."
        )
        return

    cutoff = retrieved_at - timedelta(days=RECENT_WINDOW_DAYS)
    for passage in result.passages:
        evidence.add(
            RetrievedArticle(
                reference="",
                article_id=passage.article_id,
                title=passage.title,
                publisher=passage.source,
                url=passage.canonical_url,
                published_at=passage.published_at,
                category=passage.category,
                symbols=tuple(passage.symbols),
                excerpt=excerpt(passage.text, limit=EVIDENCE_EXCERPT_CHARS),
                similarity=passage.similarity,
                is_macro=macro or passage.category == "macro",
                recent=_aware(passage.published_at) >= cutoff,
            )
        )

    # The retrieval's own warning about what similarity does and does not mean. Kept, because it
    # is true of every passage here and a reader should not have to re-derive it.
    for warning in result.warnings:
        if warning not in evidence.warnings:
            evidence.warnings.append(warning)


def _aware(value: datetime) -> datetime:
    """A stored time as an aware one. A naive value is read as UTC rather than guessed at."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# --- what the Portfolio Agent returns ------------------------------------------------------


class ProposedTarget(BaseModel):
    """One symbol's target, as the agent stated it.

    The weight, a reason, and the articles -- if any -- the reason rests on. `reason` is required
    rather than optional: a target with no stated reason is a number a reader cannot check, and
    an empty string is a clearer failure than a missing key the application might paper over.
    """

    model_config = ConfigDict(extra="ignore")

    symbol: str
    weight: Decimal
    reason: str = ""
    evidence_refs: list[str] = []

    @field_validator("symbol", mode="before")
    @classmethod
    def _symbol_is_normalised(cls, value: Any) -> Any:
        return str(value).strip().upper()

    @field_validator("weight", mode="before")
    @classmethod
    def _weight_is_a_decimal(cls, value: Any) -> Any:
        """A weight sent as a JSON number has been through a binary float by the time it gets
        here; the string form is what the prompt asks for, and a number that arrives is still
        readable rather than a reason to refuse the whole reply."""
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return value

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _references_are_extracted(cls, value: Any) -> Any:
        """Pull the `N<n>` tokens out, whether they arrive as a list or as prose."""
        if value is None:
            return []
        if isinstance(value, str):
            return _NUMBERED_REFERENCE.findall(value)
        if isinstance(value, list):
            found: list[str] = []
            for item in value:
                found.extend(_NUMBERED_REFERENCE.findall(str(item)))
            return found
        return value


class PortfolioProposal(BaseModel):
    """The agent's reply: a target per held symbol, an overall rationale, and its limitations.

    Tolerant in the two ways `Module1Findings` is -- extra keys ignored, a reference list given
    as prose read as a list -- and strict about everything else, because the weights are the only
    model output this run acts on.
    """

    model_config = ConfigDict(extra="ignore")

    targets: list[ProposedTarget] = []
    rationale: str = ""
    limitations: list[str] = []

    @field_validator("limitations", mode="before")
    @classmethod
    def _one_string_is_a_one_item_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value

    @property
    def weights(self) -> dict[str, Decimal]:
        """The targets as the mapping `app.rebalance` takes."""
        return {target.symbol: target.weight for target in self.targets}

    @property
    def reasons(self) -> dict[str, str]:
        return {target.symbol: target.reason for target in self.targets}

    @property
    def references(self) -> dict[str, list[str]]:
        return {target.symbol: list(target.evidence_refs) for target in self.targets}

    @property
    def cited(self) -> list[str]:
        """Every reference named by any target, in order, without duplicates."""
        found: list[str] = []
        for target in self.targets:
            for reference in target.evidence_refs:
                if reference not in found:
                    found.append(reference)
        return found


@dataclass
class ProposalOutcome:
    """Everything the API needs to record a proposal, and nothing else.

    `status` is `proposed`, `no_change` or `unavailable`. The first two are answers; the third
    carries a `failure` sentence and a `failure_reason` code, and no calculation at all -- a
    proposal with figures in it must be one the calculation actually produced.
    """

    status: str
    targets: dict[str, str] | None = None
    rationale: str | None = None
    # The target schedule: every holding and cash, before, requested and achieved.
    allocations: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    calculation: dict | None = None
    assumptions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    failure: str | None = None
    failure_reason: str | None = None


def policy() -> dict:
    """The policy this milestone proposes under, as it is stored and shown.

    A dict rather than a dataclass because it is stored as JSONB and read back as one, and a
    second definition of its fields would be a second thing to keep in step.
    """
    return {
        "name": POLICY_NAME,
        "description": POLICY_DESCRIPTION,
        "constraints": list(POLICY_CONSTRAINTS),
        "scenario": {
            "description": SCENARIO_DESCRIPTION,
            "shock_percent": str(SCENARIO_SHOCK_PERCENT),
        },
    }


def assumptions(snapshot: Snapshot, older_articles: int) -> list[str]:
    """Everything this proposal takes for granted, stated rather than implied.

    `older_articles` is the count rather than the evidence object, so the same assumptions can be
    assembled from the *recorded* evidence after a run as from the live object during it -- and a
    proposal read back a week later is described exactly as it was written.
    """
    notes = list(POLICY_CONSTRAINTS)
    notes.append(SCENARIO_DESCRIPTION)
    notes.append(
        f"Targets were proposed under the '{POLICY_NAME}' demo policy, which this application "
        "chose for this milestone. It is not the user's stated preference and the weights are "
        "not claimed to be optimal."
    )
    notes.append(
        "The portfolio was read once, at the start of the run, and every figure below is "
        "computed from that one reading. Nothing has been synchronised since."
    )
    if snapshot.reported_total_value is not None:
        notes.append(
            "The current allocation is a share of the broker's own equity figure, as it is "
            "everywhere else in this application. The proposed allocation is computed from the "
            "holdings and the cash."
        )
    if older_articles:
        notes.append(
            f"{older_articles} of the retrieved articles are older than "
            f"{RECENT_WINDOW_DAYS} days and are older context rather than recent news."
        )
    return notes


# --- the graph ----------------------------------------------------------------------------


class Module2State(TypedDict, total=False):
    """What travels between the graph's stages -- and, with a checkpointer, what survives the
    process.

    **Every stage's output is in here rather than in a closure.** That is the whole reason this
    shape changed: a closure lives in the process that created it, so a run that died could only
    ever be restarted from the beginning. What is here is JSON-serialisable by construction, and
    a resumed run reads it instead of re-deriving it.

    The one thing deliberately *not* here is the portfolio snapshot. It belongs to the proposal
    row, not to the graph: a resumed run must read the snapshot the original run froze, and a
    copy inside the checkpoint would be a second one to keep in step.
    """

    evidence: dict[str, Any]
    targets: list[dict[str, Any]]
    rationale: str
    limitations: list[str]
    calculation: dict[str, Any] | None
    status: str
    failure: str | None
    failure_reason: str | None
    stopped: bool


def _proposal_request(
    snapshot: Snapshot, evidence: NewsEvidence, *, problem: str | None = None
) -> str:
    """The Portfolio Agent's brief: the policy, the portfolio, and the retrieved articles."""
    parts = [
        f"This portfolio holds {len(snapshot.positions)} position(s). State a target allocation "
        "weight for every one of them, each with its own reason.",
        "",
        "The policy these targets are proposed under, which is enforced in code:",
        f"- {POLICY_DESCRIPTION}",
    ]
    parts += [f"- {constraint}" for constraint in POLICY_CONSTRAINTS]
    parts += [
        "",
        "The portfolio, frozen at the start of this run:",
        snapshot.describe_for_prompt(),
        "",
        "Articles retrieved for this portfolio. These are the only things you may cite:",
        _bounded(evidence.render(), MAX_EVIDENCE_CHARS),
    ]

    if evidence.warnings:
        parts += [
            "",
            "Limitations of that retrieval, all of which belong in your `limitations`:",
        ]
        parts += [f"- {warning}" for warning in evidence.warnings]

    if problem:
        parts += ["", f"Your previous reply could not be used: {problem}"]

    parts += [
        "",
        "For every symbol, give the reason for that specific number: what evidence moves it, or "
        "why the portfolio's current shape argues for it. Cite the retrieved articles your "
        "reason rests on, and leave the citations empty when the reason is about the policy or "
        "the portfolio rather than about an article.",
        "",
        "Reply with the JSON object described in your instructions and nothing else.",
    ]
    return "\n".join(parts)


def build_module2_graph(
    *,
    client: ModelClient,
    snapshot: Snapshot,
    budget: RunBudget,
    retrieve: Callable[[Sequence[str]], NewsEvidence],
    progress: Progress = NULL_PROGRESS,
    checkpointer: Any = None,
):
    """The retrieve / propose / calculate graph.

    `retrieve` is injected rather than called directly so a test can hand in a fixed set of
    articles without a vector store or an embedding model.

    **The graph is built per run and the state is not.** The nodes close over the client, the
    snapshot, the budget and the retriever -- none of which can be checkpointed, and none of
    which need to be: a resumed run rebuilds all four from the proposal row and the run's own
    configuration, and reads the *stages* out of the checkpoint. That division is what makes the
    same thread resumable by a process that has never seen the original.

    `checkpointer` is None when checkpointing is unavailable, and the graph then behaves exactly
    as it did before it existed -- it simply cannot be resumed.
    """

    def retrieve_node(state: Module2State) -> Module2State:
        progress(
            ProgressEvent(
                KIND_STAGE,
                {
                    "stage": STAGE_RETRIEVING,
                    "detail": f"Searching stored news for {len(snapshot.positions)} holding(s)",
                },
            )
        )
        evidence = retrieve(snapshot.symbols())
        payload = {
            "articles": evidence.as_list(),
            "warnings": list(evidence.warnings),
            "queries": list(evidence.queries),
        }

        if not len(evidence):
            # Refused here rather than proposed from nothing. A target weight with no evidence
            # behind it is a number that reads as a recommendation and is not one.
            return {
                "evidence": {**payload, "articles": []},
                "limitations": list(evidence.warnings),
                "status": STATUS_UNAVAILABLE,
                "failure_reason": REASON_NO_EVIDENCE,
                "failure": (
                    "No article could be retrieved for this portfolio, so there is nothing to "
                    "base a proposal on. " + " ".join(evidence.warnings)
                ).strip(),
                "stopped": True,
            }

        return {
            "evidence": payload,
            "limitations": list(evidence.warnings),
        }

    def propose_node(state: Module2State) -> Module2State:
        evidence = NewsEvidence.from_state(state.get("evidence") or {})
        progress(
            ProgressEvent(
                KIND_STAGE,
                {
                    "stage": STAGE_PROPOSING,
                    "detail": f"Proposing target weights from {len(evidence)} article(s)",
                },
            )
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": MODULE2_PROMPT},
            {"role": "user", "content": _proposal_request(snapshot, evidence)},
        ]
        problem: str | None = None
        # Set when the reply was refused for citing something that does not exist, which is a
        # different failure from a reply that would not parse. Tracked as a flag rather than
        # inferred from the message later: matching on wording is how behaviour starts depending
        # on prose.
        rejected_citations = False

        for attempt in range(1, PROPOSAL_ATTEMPTS + 1):
            try:
                budget.before_model_request()
            except BudgetExhausted as exhausted:
                logger.warning("no targets were proposed: %s", exhausted)
                return {
                    "status": STATUS_UNAVAILABLE,
                    "failure_reason": REASON_BUDGET_EXHAUSTED,
                    "failure": (
                        "The proposal run stopped before any target weights were proposed: "
                        f"{exhausted.detail}."
                    ),
                    "stopped": True,
                }

            response = client.complete(
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=budget.max_output_tokens,
            )
            budget.record_model(
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )

            parsed, problem = _parse_proposal(response.content)
            if parsed is None:
                logger.warning("proposal attempt %d was unusable: %s", attempt, problem)
            else:
                # Every reference any target cites, checked against what this run retrieved --
                # one invented citation fails the reply, whether it is in the overall rationale
                # or attached to a single symbol's reason.
                valid, invalid = evidence.validate(parsed.cited)
                if invalid:
                    # An invented citation is not a formatting mistake, and it is not corrected
                    # in prose: the reason would be rewritten to cite something else while
                    # resting on the same unsupported claim.
                    problem = (
                        "the reasons cited "
                        + ", ".join(invalid)
                        + ", which are not among the retrieved articles. Cite only "
                        + (", ".join(evidence.references()) or "the references given")
                    )
                    rejected_citations = True
                    logger.warning("proposal attempt %d cited %s", attempt, ", ".join(invalid))
                elif not parsed.targets:
                    problem = "no target weights were given"
                else:
                    return {
                        "targets": [
                            {
                                "symbol": target.symbol,
                                "weight": str(target.weight),
                                "reason": target.reason.strip(),
                                "evidence_refs": list(valid_for(target, evidence)),
                            }
                            for target in parsed.targets
                        ],
                        "rationale": parsed.rationale.strip(),
                        "limitations": _merged(state.get("limitations", []), parsed.limitations),
                    }

            if attempt < PROPOSAL_ATTEMPTS:
                messages = [
                    *messages,
                    response.message,
                    {
                        "role": "user",
                        "content": (
                            f"That reply could not be used: {problem}. Reply again with only "
                            "the JSON object described in your instructions."
                        ),
                    },
                ]

        return {
            "status": STATUS_UNAVAILABLE,
            "failure_reason": (
                REASON_UNVERIFIABLE_CITATIONS if rejected_citations else REASON_INVALID_OUTPUT
            ),
            "rationale": "",
            "targets": [],
            "failure": (
                "The proposal could not be produced: the model's reply could not be used "
                f"({problem}). Nothing was proposed rather than a proposal being shown with "
                "unsupported citations."
            ),
            "stopped": True,
        }

    def calculate_node(state: Module2State) -> Module2State:
        progress(
            ProgressEvent(
                KIND_STAGE,
                {"stage": STAGE_CALCULATING, "detail": "Calculating whole-share trades"},
            )
        )
        targets = state.get("targets") or []
        try:
            result = calculate_rebalance(
                snapshot.as_holdings(),
                snapshot.cash_balance,
                snapshot.prices,
                {item["symbol"]: Decimal(item["weight"]) for item in targets},
                reported_total_value=snapshot.reported_total_value,
                reasons={item["symbol"]: item.get("reason", "") for item in targets},
                references={
                    item["symbol"]: item.get("evidence_refs", []) for item in targets
                },
            )
        except RebalanceUnavailable as unavailable:
            logger.info("no proposal could be calculated: %s", unavailable.reason)
            return {
                "status": STATUS_UNAVAILABLE,
                "failure_reason": unavailable.reason,
                "failure": (
                    f"No proposal could be calculated from those target weights. "
                    f"{unavailable.detail}"
                ),
                "calculation": None,
                "stopped": True,
            }

        return {
            "status": str(result.outcome),
            "calculation": as_calculation(result),
            "limitations": _merged(state.get("limitations", []), list(result.limitations)),
        }

    def after_retrieve(state: Module2State) -> str:
        return END if state.get("stopped") else "propose"

    def after_propose(state: Module2State) -> str:
        return END if state.get("stopped") else "calculate"

    graph = StateGraph(Module2State)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("propose", propose_node)
    graph.add_node("calculate", calculate_node)
    graph.add_edge(START, "retrieve")
    graph.add_conditional_edges(
        "retrieve", after_retrieve, {"propose": "propose", END: END}
    )
    graph.add_conditional_edges(
        "propose", after_propose, {"calculate": "calculate", END: END}
    )
    graph.add_edge("calculate", END)
    return graph.compile(checkpointer=checkpointer)


def valid_for(target: ProposedTarget, evidence: NewsEvidence) -> list[str]:
    """The references one target cites that actually resolve.

    They have already been checked in bulk above, so anything invalid has ended the reply; this
    exists to put the *resolved* list on the schedule rather than the raw text, which keeps an
    unresolvable reference from ever reaching a reader even if the check above is relaxed later.
    """
    valid, _ = evidence.validate(target.evidence_refs)
    return valid


def run_proposal(
    *,
    client: ModelClient,
    snapshot: Snapshot,
    budget: RunBudget,
    retrieve: Callable[[Sequence[str]], NewsEvidence],
    progress: Progress = NULL_PROGRESS,
    checkpointer: Any = None,
    thread_id: str | None = None,
    recursion_limit: int | None = None,
    resume: bool = False,
    graph: Any = None,
) -> ProposalOutcome:
    """Retrieve, propose, and calculate -- or say why not.

    `resume=True` continues a thread from its last completed stage instead of starting one. The
    snapshot handed in on that path is the one **stored on the proposal row**, not a fresh read:
    a resumed run must finish the reasoning the original started, against the portfolio it
    started against.

    Raises nothing for a condition of the data or the model: every one of those becomes an
    outcome with `unavailable` and a sentence. What it does raise is a provider failure, which
    the caller reports as an outage rather than as a fact about the portfolio.
    """
    # A caller that has already built this graph -- recovery, which reads its state before
    # deciding whether there is anything to resume -- hands it back rather than having a second
    # one constructed over the same thread.
    if graph is None:
        graph = build_module2_graph(
            client=client,
            snapshot=snapshot,
            budget=budget,
            retrieve=retrieve,
            progress=progress,
            checkpointer=checkpointer,
        )

    config: dict[str, Any] = {
        "recursion_limit": recursion_limit or (2 * budget.max_model_requests + 6)
    }
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}

    # A resumed run is invoked with no input: everything it needs is in the checkpoint, and
    # passing an initial state would overwrite the stages that already completed.
    initial: Any = None if resume else {"stopped": False}
    final = graph.invoke(initial, config)
    return outcome_from_state(dict(final), snapshot)


def outcome_from_state(state: Mapping[str, Any], snapshot: Snapshot) -> ProposalOutcome:
    """The run's outcome, read out of its final state.

    Built from the state rather than from a closure, because on a resumed run the state is the
    only thing that survived -- and because a graph that completed before its process died would
    otherwise have to be run again just to learn what it had already decided.
    """
    evidence_payload = state.get("evidence") or {}
    older = sum(1 for item in evidence_payload.get("articles", []) if not item.get("recent", True))

    return ProposalOutcome(
        status=str(state.get("status") or STATUS_UNAVAILABLE),
        targets={
            item["symbol"]: item["weight"] for item in state.get("targets") or []
        }
        or None,
        rationale=state.get("rationale") or None,
        allocations=list((state.get("calculation") or {}).get("allocations", [])),
        evidence=list(evidence_payload.get("articles", [])),
        calculation=state.get("calculation"),
        assumptions=assumptions(snapshot, older),
        limitations=list(state.get("limitations") or []),
        failure=state.get("failure"),
        failure_reason=state.get("failure_reason"),
    )


# --- shapes --------------------------------------------------------------------------------


def as_calculation(result: Rebalance) -> dict:
    """One calculation, as the API stores and returns it.

    The allocation blocks carry every holding *and* cash, so each side is a complete allocation
    a donut can be drawn from. The two sides are separate blocks and are never merged: they have
    different denominators, and a single pie of both would be a chart of nothing.
    """
    return {
        "outcome": str(result.outcome),
        "trades": [trade.as_dict() for trade in result.trades],
        "targets": {symbol: str(weight) for symbol, weight in result.targets.items()},
        "allocations": [item.as_dict() for item in result.allocations],
        "cash_target": str(result.cash_target),
        "before": _allocation(result.before),
        "after": _allocation(result.after),
        "cash_before": str(result.cash_before),
        "cash_after": str(result.cash_after),
        "sell_proceeds": str(result.sell_proceeds),
        "buy_cost": str(result.buy_cost),
        "buys_depend_on_sells": result.buys_depend_on_sells,
        "largest_before": _concentration(result.largest_before),
        "largest_after": _concentration(result.largest_after),
        "scenario": {
            "symbol": result.scenario.symbol,
            "price_change_percent": str(SCENARIO_SHOCK_PERCENT),
            "price_before": str(result.scenario.price_before),
            "price_after": str(result.scenario.price_after),
            "holding_value_before": str(result.scenario.holding_value_before),
            "holding_value_after": str(result.scenario.holding_value_after),
            "total_value_before": str(result.scenario.total_value_before),
            "total_value_after": str(result.scenario.total_value_after),
            "change_value": str(result.scenario.change_value),
            "change_percent": (
                None
                if result.scenario.change_percent is None
                else str(result.scenario.change_percent)
            ),
        },
        "reconciliation": {
            "reported_total_value": (
                None
                if result.reconciliation.reported_total_value is None
                else str(result.reconciliation.reported_total_value)
            ),
            "summed_total_value": str(result.reconciliation.summed_total_value),
            "difference": (
                None
                if result.reconciliation.difference is None
                else str(result.reconciliation.difference)
            ),
            "note": result.reconciliation.note,
        },
    }


def _allocation(valuation: Any) -> dict:
    return {
        "total_value": str(valuation.total_value),
        "cash_balance": str(valuation.cash_balance),
        "cash_allocation_percent": (
            None
            if valuation.cash_allocation_percent is None
            else str(valuation.cash_allocation_percent)
        ),
        "holdings": [
            {
                "symbol": item.symbol,
                "quantity": str(item.quantity),
                "price": str(item.price),
                "holding_value": str(item.holding_value),
                "allocation_percent": (
                    None if item.allocation_percent is None else str(item.allocation_percent)
                ),
            }
            for item in valuation.holdings
        ],
    }


def _concentration(item: Any) -> dict:
    return {
        "symbol": item.symbol,
        "allocation_percent": (
            None if item.allocation_percent is None else str(item.allocation_percent)
        ),
    }


def _bounded(text: str, limit: int) -> str:
    """The evidence as prompt text, with a hard ceiling and the cut stated.

    Reaching this at all means the per-article excerpts already fit, which is the intent: this
    is the backstop that stops a run against an unexpectedly large retrieval from sending a
    prompt nobody budgeted for. A truncation is announced rather than silent, because a model
    reading a partial evidence list has to know it is partial.
    """
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n[This evidence list was cut off at {limit} characters. It is not complete.]"
    )


def _merged(first: Sequence[str], second: Sequence[str]) -> list[str]:
    """Two limitation lists, deduplicated in order. The application's own come first."""
    seen: set[str] = set()
    merged: list[str] = []
    for item in (*first, *second):
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            merged.append(text)
    return merged


def _parse_proposal(content: str | None) -> tuple[PortfolioProposal | None, str | None]:
    """Read the agent's reply, and say why if it could not be read."""
    if not content or not content.strip():
        return None, "the reply was empty"
    try:
        raw = json.loads(content)
    except ValueError as exc:
        return None, f"the reply was not valid JSON ({exc})"
    if not isinstance(raw, dict):
        return None, f"the reply was a JSON {type(raw).__name__}, not an object"
    try:
        parsed = PortfolioProposal.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "the reply"
        return None, f"{location}: {first['msg']}"

    if not parsed.targets:
        return None, "no target weights were given"
    return parsed, None


__all__ = [
    "MAX_ARTICLES",
    "MODULE2_PROMPT",
    "NewsEvidence",
    "PortfolioProposal",
    "ProposalOutcome",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_INVALID_OUTPUT",
    "REASON_NO_EVIDENCE",
    "REASON_UNVERIFIABLE_CITATIONS",
    "RECENT_WINDOW_DAYS",
    "RetrievedArticle",
    "STAGE_CALCULATING",
    "STAGE_PROPOSING",
    "STAGE_RETRIEVING",
    "STATUS_UNAVAILABLE",
    "as_calculation",
    "assumptions",
    "build_module2_graph",
    "policy",
    "retrieve_news",
    "run_proposal",
]
