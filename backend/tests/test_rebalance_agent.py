"""Tests for the Module 2 workflow: retrieval, the Portfolio Agent, and the calculation.

Run from the backend directory (inside the container, /app):

    python -m unittest discover -s tests -t .

The model is scripted and the retrieval is injected; the graph, the budget, the snapshot and the
calculation are the real ones. No database is touched -- a snapshot is captured from a stub
portfolio over the demo price table, so these run without PostgreSQL and without Qdrant.
"""

import json
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.agent.budget import RunBudget
from app.agent.module2 import (
    REASON_BUDGET_EXHAUSTED,
    REASON_INVALID_OUTPUT,
    REASON_NO_EVIDENCE,
    REASON_UNVERIFIABLE_CITATIONS,
    STATUS_UNAVAILABLE,
    NewsEvidence,
    RetrievedArticle,
    run_proposal,
)
from app.rebalance import CASH_REASON, Action
from app.rebalance_snapshot import capture
from tests.agent_doubles import ScriptedModel, say

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class StubHolding:
    symbol: str
    quantity: Decimal
    market_price: Decimal | None = None


@dataclass(frozen=True)
class StubPortfolio:
    """The portfolio `app.portfolio_prices` and `capture` need, and nothing more.

    `broker` is None, so the basis is the demo price table -- which is what makes this test
    database-free: the real `capture` runs, over the real demo prices.
    """

    holdings: list[StubHolding]
    cash_balance: Decimal
    currency: str = "USD"
    broker: str | None = None
    broker_equity: Decimal | None = None
    last_synced_at: datetime | None = None


# The demo price table is AAPL 200, MSFT 450, NVDA 150.
def snapshot(*holdings: tuple[str, str], cash: str = "0"):
    return capture(
        StubPortfolio(
            holdings=[StubHolding(symbol, Decimal(quantity)) for symbol, quantity in holdings],
            cash_balance=Decimal(cash),
        ),
        now=NOW,
    )


def broker_snapshot(
    *holdings: tuple[str, str],
    cash: str = "0",
    prices: dict[str, str] | None = None,
):
    """The same portfolio on a broker basis: stored market prices, and an equity figure.

    A synchronised portfolio is valued at the prices the broker reported, so a stored
    `market_price` is what moves here -- which is why a price-basis test needs one.
    """
    quoted = prices or {}
    return capture(
        StubPortfolio(
            holdings=[
                StubHolding(symbol, Decimal(quantity), Decimal(quoted.get(symbol, "100.00")))
                for symbol, quantity in holdings
            ],
            cash_balance=Decimal(cash),
            broker="alpaca_paper",
            broker_equity=Decimal("12345.00"),
            last_synced_at=NOW,
        ),
        now=NOW,
    )


def article(
    reference: str,
    *,
    title: str = "A stored article",
    publisher: str = "benzinga",
    article_id: int = 1,
    symbols: tuple[str, ...] = ("AAPL",),
    days_old: int = 1,
    category: str = "company",
) -> RetrievedArticle:
    return RetrievedArticle(
        reference=reference,
        article_id=article_id,
        title=title,
        publisher=publisher,
        url=f"https://example.test/{article_id}",
        published_at=NOW - timedelta(days=days_old),
        category=category,
        symbols=symbols,
        excerpt="An excerpt of the stored article.",
        similarity=0.71,
        is_macro=category == "macro",
        recent=days_old <= 30,
    )


def evidence(*articles: RetrievedArticle, warnings: list[str] | None = None) -> NewsEvidence:
    found = NewsEvidence(warnings=list(warnings or []))
    for item in articles:
        found.add(item)
    return found


def retrieval(*articles: RetrievedArticle, warnings: list[str] | None = None):
    """A `retrieve` callable, so no vector store and no embedding model is involved."""
    return lambda symbols: evidence(*articles, warnings=warnings)


def weights(**values: str) -> tuple[str, list]:
    """A well-formed Portfolio Agent reply: one target per symbol, each with a reason."""
    return say(
        json.dumps(
            {
                "targets": [
                    {
                        "symbol": symbol,
                        "weight": weight,
                        "reason": f"Why {symbol} moves to {weight}.",
                        "evidence_refs": ["N1"],
                    }
                    for symbol, weight in values.items()
                ],
                "rationale": "The evidence points this way.",
                "limitations": [],
            }
        )
    )


def targets(*entries: dict) -> tuple[str, list]:
    """A reply built from explicit entries, for the cases about a single target's shape."""
    return say(
        json.dumps(
            {
                "targets": list(entries),
                "rationale": "The evidence points this way.",
                "limitations": [],
            }
        )
    )


def a_reply(**values: str) -> tuple[str, list]:
    """A reply with a per-symbol reason, as the prompt asks for."""
    import json

    return say(
        json.dumps(
            {
                "targets": [
                    {
                        "symbol": symbol,
                        "weight": weight,
                        "reason": f"Why {symbol} moves to {weight}.",
                        "evidence_refs": ["N1"],
                    }
                    for symbol, weight in values.items()
                ],
                "rationale": "The evidence points this way.",
                "limitations": [],
            }
        )
    )


def run(*, script, snapshot_, retrieve, budget=None):
    client = ScriptedModel(script)
    outcome = run_proposal(
        client=client,
        snapshot=snapshot_,
        budget=budget or RunBudget(),
        retrieve=retrieve,
    )
    return outcome, client


class ProposalTest(unittest.TestCase):
    """A run that produces one."""

    def setUp(self):
        # AAPL 50 at 200 is 10,000 and MSFT 10 at 450 is 4,500, plus 1,000 cash: 15,500.
        self.snapshot = snapshot(("AAPL", "50"), ("MSFT", "10"), cash="1000")

    def test_a_proposal_is_calculated_from_the_weights_and_nothing_else(self):
        outcome, client = run(
            script=[weights(AAPL="0.5", MSFT="0.3")],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, "proposed")
        self.assertEqual(outcome.targets, {"AAPL": "0.5", "MSFT": "0.3"})
        self.assertEqual(outcome.rationale, "The evidence points this way.")
        self.assertEqual(len(client.requests), 1, "one model call for one proposal")

        trades = outcome.calculation["trades"]
        self.assertEqual([trade["symbol"] for trade in trades], ["AAPL"])
        self.assertEqual(trades[0]["action"], str(Action.SELL))
        # 15,500 * 0.5 is 7,750, which is 38.75 shares against the 50 held -- sold up, to 12.
        self.assertEqual(trades[0]["quantity"], "12")
        self.assertEqual(trades[0]["estimated_value"], "2400.00")
        # MSFT's target is 10.33 shares against the 10 held, which rounds to no purchase.
        self.assertEqual(outcome.calculation["outcome"], "proposed")

    def test_the_evidence_is_recorded_as_articles_with_their_links(self):
        outcome, _ = run(
            script=[weights(AAPL="0.5", MSFT="0.3")],
            snapshot_=self.snapshot,
            retrieve=retrieval(
                article("N1", title="Microsoft raises its data centre forecast", article_id=7)
            ),
        )

        self.assertEqual(len(outcome.evidence), 1)
        recorded = outcome.evidence[0]
        self.assertEqual(recorded["reference"], "N1")
        self.assertEqual(recorded["article_id"], 7)
        self.assertEqual(recorded["url"], "https://example.test/7")
        self.assertEqual(recorded["publisher"], "benzinga")
        self.assertTrue(recorded["published_at"].startswith("2026-09-22"))

    def test_weights_that_match_the_portfolio_are_reported_as_no_change(self):
        """A valid outcome, and the one the page has to render without an empty trade table."""
        outcome, _ = run(
            script=[weights(AAPL="1")],
            snapshot_=snapshot(("AAPL", "50")),
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, "no_change")
        self.assertEqual(outcome.calculation["trades"], [])
        self.assertTrue(
            any("No change is recommended" in note for note in outcome.limitations)
        )

    def test_retrieval_warnings_reach_the_limitations_whether_or_not_the_model_says_so(self):
        """The application's own record of what went wrong is carried, not the model's summary
        of it -- which is exactly the failure the Module 1 flow was fixed for."""
        outcome, _ = run(
            script=[weights(AAPL="0.5", MSFT="0.3")],
            snapshot_=self.snapshot,
            retrieve=retrieval(
                article("N1"), warnings=["2 retrieved passage(s) were dropped."]
            ),
        )

        self.assertIn("2 retrieved passage(s) were dropped.", outcome.limitations)

    def test_an_old_article_is_labelled_rather_than_dropped(self):
        outcome, _ = run(
            script=[weights(AAPL="0.5", MSFT="0.3")],
            snapshot_=self.snapshot,
            retrieve=retrieval(
                article("N1"), article("N2", article_id=2, days_old=200, category="macro")
            ),
        )

        self.assertEqual(len(outcome.evidence), 2)
        self.assertTrue(outcome.evidence[0]["recent"])
        self.assertFalse(outcome.evidence[1]["recent"])
        self.assertTrue(
            any("older context rather than recent news" in note for note in outcome.assumptions)
        )

    def test_the_policy_is_stated_on_the_proposal(self):
        outcome, _ = run(
            script=[weights(AAPL="0.5", MSFT="0.3")],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertTrue(
            any("demo policy" in note and "not the user's stated preference" in note
                for note in outcome.assumptions)
        )


class RefusalTest(unittest.TestCase):
    """Every way this produces no proposal, and what each one says."""

    def setUp(self):
        self.snapshot = snapshot(("AAPL", "50"), ("MSFT", "10"), cash="1000")

    def test_no_evidence_stops_before_the_model_is_asked(self):
        """A target weight with nothing behind it reads as a recommendation. Refused, and the
        model is never called -- there is nothing to ask it about."""
        outcome, client = run(
            script=[],
            snapshot_=self.snapshot,
            retrieve=retrieval(),
        )

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.failure_reason, REASON_NO_EVIDENCE)
        self.assertEqual(client.requests, [])
        self.assertIsNone(outcome.calculation)

    def test_a_malformed_reply_is_retried_once_and_then_refused(self):
        outcome, client = run(
            script=[say("not json at all"), say("{still not json")],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.failure_reason, REASON_INVALID_OUTPUT)
        self.assertEqual(len(client.requests), 2, "one try and one correction, not three")
        self.assertIsNone(outcome.targets)
        self.assertIsNone(outcome.calculation)

    def test_a_malformed_reply_followed_by_a_good_one_succeeds(self):
        outcome, client = run(
            script=[say("not json"), weights(AAPL="0.5", MSFT="0.3")],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, "proposed")
        self.assertEqual(len(client.requests), 2)
        self.assertIn("could not be used", client.requests[1]["messages"][-1]["content"])

    def test_a_reply_with_no_weights_is_refused(self):
        outcome, _ = run(
            script=[
                say(json.dumps({"rationale": "I have no opinion.", "evidence_refs": ["N1"]})),
                say(json.dumps({"target_weights": {}, "rationale": "Still none."})),
            ],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.failure_reason, REASON_INVALID_OUTPUT)

    def test_an_invented_citation_is_refused_rather_than_shown(self):
        """A rationale resting on an article that was never retrieved is the one thing that
        must not reach a reader as though it were evidence."""
        invented = say(
            json.dumps(
                {
                    "targets": [
                        {
                            "symbol": "AAPL",
                            "weight": "0.5",
                            "reason": "As N9 says, the outlook has changed.",
                            "evidence_refs": ["N9"],
                        },
                        {"symbol": "MSFT", "weight": "0.3", "reason": "Held steady."},
                    ],
                    "rationale": "The evidence points this way.",
                }
            )
        )
        outcome, client = run(
            script=[invented, invented],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.failure_reason, REASON_UNVERIFIABLE_CITATIONS)
        self.assertEqual(len(client.requests), 2)
        self.assertIn("N9", outcome.failure)
        self.assertIsNone(outcome.calculation)

    def test_a_citation_that_resolves_is_accepted(self):
        outcome, _ = run(
            script=[weights(AAPL="0.5", MSFT="0.3")],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1"), article("N2", article_id=2)),
        )

        self.assertEqual(outcome.status, "proposed")

    def test_a_target_for_a_symbol_that_is_not_held_is_refused(self):
        """The calculation refuses it, and the refusal is what the page shows -- no proposal
        with a quiet extra position in it."""
        outcome, _ = run(
            script=[weights(AAPL="0.5", MSFT="0.3", TSLA="0.2")],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.failure_reason, "unknown_target_symbol")
        self.assertIsNone(outcome.calculation)

    def test_a_target_that_omits_a_held_symbol_is_refused(self):
        """A truncated reply must not be able to liquidate a position by omission."""
        outcome, _ = run(
            script=[weights(AAPL="0.5")],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.failure_reason, "incomplete_targets")

    def test_weights_that_add_up_to_more_than_the_portfolio_are_refused(self):
        outcome, _ = run(
            script=[weights(AAPL="0.9", MSFT="0.9")],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.failure_reason, "targets_exceed_total")

    def test_targets_that_cannot_be_funded_are_refused(self):
        """Where a refusal has to arrive as a *status* rather than as an exception.

        AAPL 10.5 at 200 and NVDA 0.5 at 150 is 2,175, with no cash. Asking for all of it in
        NVDA sells AAPL and buys 14 NVDA. The sale wants 10.5 shares and the position can only
        make 10, so it raises 2,000 against a 2,100 purchase and the account is 100 short.
        """
        outcome, _ = run(
            script=[weights(AAPL="0", NVDA="1")],
            snapshot_=snapshot(("AAPL", "10.5"), ("NVDA", "0.5")),
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.failure_reason, "unfundable")
        self.assertIsNone(outcome.calculation)
        self.assertIn("Nothing was adjusted", outcome.failure)

    def test_running_out_of_budget_before_proposing_is_reported_as_such(self):
        outcome, client = run(
            script=[],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
            budget=RunBudget(max_model_requests=0),
        )

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.failure_reason, REASON_BUDGET_EXHAUSTED)
        self.assertEqual(client.requests, [])


class ParsingTest(unittest.TestCase):
    """The reply's shape, and what is tolerated in it."""

    def setUp(self):
        self.snapshot = snapshot(("AAPL", "50"), ("MSFT", "10"), cash="1000")

    def _targets(self, payload: dict) -> dict | None:
        """Run one reply through the agent. Given twice, because an unusable reply is retried
        and the scripted model raises rather than repeating itself."""
        reply = say(json.dumps(payload))
        outcome, _ = run(
            script=[reply, reply],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )
        return outcome.targets

    def test_a_weight_sent_as_a_json_number_is_read(self):
        """The prompt asks for a string, and a number that arrives anyway is a formatting
        difference rather than a reason to refuse the whole reply."""
        self.assertEqual(
            self._targets(
                {
                    "targets": [
                        {"symbol": "AAPL", "weight": 0.5, "reason": "a"},
                        {"symbol": "MSFT", "weight": 0.3, "reason": "b"},
                    ]
                }
            ),
            {"AAPL": "0.5", "MSFT": "0.3"},
        )

    def test_symbols_are_upper_cased(self):
        self.assertEqual(
            self._targets(
                {
                    "targets": [
                        {"symbol": "aapl", "weight": "0.5", "reason": "a"},
                        {"symbol": "msft", "weight": "0.3", "reason": "b"},
                    ]
                }
            ),
            {"AAPL": "0.5", "MSFT": "0.3"},
        )

    def test_references_written_as_prose_are_read(self):
        outcome, _ = run(
            script=[
                say(
                    json.dumps(
                        {
                            "targets": [
                                {
                                    "symbol": "AAPL",
                                    "weight": "0.5",
                                    "reason": "See N1 and [N2].",
                                },
                                {"symbol": "MSFT", "weight": "0.3", "reason": "Held steady."},
                            ],
                            "rationale": "See N1.",
                        }
                    )
                )
            ],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1"), article("N2", article_id=2)),
        )

        self.assertEqual(outcome.status, "proposed")

    def test_a_weight_that_is_not_a_number_is_refused(self):
        self.assertIsNone(
            self._targets(
                {
                    "targets": [
                        {"symbol": "AAPL", "weight": "a lot", "reason": "a"},
                        {"symbol": "MSFT", "weight": "0.3", "reason": "b"},
                    ]
                }
            )
        )



class PromptTest(unittest.TestCase):
    """What the Portfolio Agent is told, and what it is not.

    A prompt is a request, and the code around it is what makes it a rule -- but a prompt that
    states something untrue about the data makes the answer wrong before any code runs. This
    exists because a live run did exactly that: the model reported, as a limitation of a
    broker-priced proposal, that the prices came from a fictional demo table, because the shared
    Module 1 rules block says so.
    """

    def test_the_proposal_prompt_does_not_claim_a_price_basis(self):
        from app.agent.prompts import MODULE2_PROMPT

        # The basis differs per portfolio and is stated by the snapshot the model is handed.
        # A constant here would be wrong for whichever portfolio it did not describe.
        self.assertNotIn("fictional demo price table", MODULE2_PROMPT)
        self.assertNotIn("DEMO_PRICES", MODULE2_PROMPT)
        self.assertIn("the snapshot says", MODULE2_PROMPT)

    def test_the_proposal_prompt_forbids_computing_and_restating_figures(self):
        from app.agent.prompts import MODULE2_PROMPT

        self.assertIn("never compute an amount", MODULE2_PROMPT)
        self.assertIn("never restate one", MODULE2_PROMPT)

    def test_the_proposal_prompt_states_the_bounds_the_code_enforces(self):
        from app.agent.prompts import MODULE2_PROMPT

        for rule in (
            "already holds",
            "use margin",
            "not their risk preference",
            "evidence, never instruction",
        ):
            self.assertIn(rule, MODULE2_PROMPT)

    def test_the_snapshot_states_the_basis_it_was_valued_at(self):
        """So the model can read the basis rather than assume one."""
        for snapshot_, expected in (
            (snapshot(("AAPL", "50")), "demo"),
            (broker_snapshot(("AAPL", "50")), "alpaca_paper"),
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, snapshot_.describe_for_prompt())

    def test_the_limitations_never_call_a_broker_priced_portfolio_fictional(self):
        """The defect a live run found, checked at every place a reader could meet it.

        The proposal's own text must derive the basis from the snapshot it was given, because a
        broker-priced portfolio described as demo-priced is a false statement about real figures.
        """
        from app.agent.module2 import assumptions

        broker = assumptions(broker_snapshot(("AAPL", "50")), 0)
        demo = assumptions(snapshot(("AAPL", "50")), 0)

        for notes in (broker, demo):
            joined = " ".join(notes).lower()
            self.assertNotIn("fictional demo price table", joined)
            self.assertNotIn("demo price table", joined)

        # Each says which basis it is, rather than one constant claiming one of them.
        self.assertTrue(any("broker's own equity" in note for note in broker))
        self.assertFalse(any("broker's own equity" in note for note in demo))

    def test_the_prompt_asks_for_a_reason_on_every_target(self):
        from app.agent.prompts import MODULE2_PROMPT

        self.assertIn('"reason" is required, and it is per symbol', MODULE2_PROMPT)
        self.assertIn("evidence_refs", MODULE2_PROMPT)
        self.assertIn("discretionary choice within", MODULE2_PROMPT)
        self.assertIn("not optimal, not derived", MODULE2_PROMPT)
        # The old single-map shape is gone: a reply in it would now be unusable.
        self.assertNotIn("target_weights", MODULE2_PROMPT)


if __name__ == "__main__":
    unittest.main()


class RetrievalTest(unittest.TestCase):
    """The News Agent itself, over a stubbed search.

    `search_news` is replaced and everything above it is real: the queries it is handed, the
    filters it asks for, the folding of passages into evidence, the deduplication and the
    excerpting. This is the layer the injected-evidence tests above skip -- and skipping it is
    how a wrong `excerpt()` call reached a live run, because a test that hands in ready-made
    articles never executes the code that builds them.
    """

    def setUp(self):
        self.snapshot = snapshot(("AAPL", "50"), ("MSFT", "10"), cash="1000")
        self.queries: list[dict] = []

    def a_passage(self, article_id: int, **overrides):
        from app.news_index import RetrievedNews

        fields: dict = {
            "text": "Microsoft raised its data centre spending forecast. " * 30,
            "similarity": 0.71,
            "chunk_index": 0,
            "article_id": article_id,
            "provider": "alpaca_news",
            "source": "benzinga",
            "title": f"A stored article {article_id}",
            "canonical_url": f"https://example.test/{article_id}",
            "symbols": ("MSFT",),
            "category": "company",
            "published_at": NOW - timedelta(days=1),
            "ingested_at": NOW,
        }
        fields.update(overrides)
        return RetrievedNews(**fields)

    def search(self, *, macro_returns: int = 0, company_returns: int = 2):
        """A `search_news` that records what it was asked for and answers with articles."""
        from app.news_index import STATUS_OK, SearchResult

        def fake(session, *, query, symbols=(), category=None, **kwargs):
            self.queries.append({"query": query, "symbols": tuple(symbols), "category": category})
            if category == "macro":
                passages = tuple(self.a_passage(100 + index) for index in range(macro_returns))
            else:
                # Always article 7 first, so two symbols' queries overlap on it.
                passages = (self.a_passage(7),) + tuple(
                    self.a_passage(20 + index) for index in range(company_returns - 1)
                )
            return SearchResult(
                status=STATUS_OK, query=query, returned=len(passages), passages=passages
            )

        return fake

    def retrieve(self, symbols=("AAPL", "MSFT")):
        from unittest import mock

        from app.agent.module2 import retrieve_news

        with mock.patch("app.agent.module2.search_news", side_effect=self.search()):
            return retrieve_news(None, list(symbols), store=None, embedder=None)

    def test_each_held_symbol_is_searched_and_macro_is_searched_separately(self):
        self.retrieve()

        # Macro releases carry no symbols, so they cannot be reached by a symbol filter -- the
        # category is the only way in, which is why it is its own query.
        self.assertEqual(
            [(item["symbols"], item["category"]) for item in self.queries],
            [(('AAPL',), None), (('MSFT',), None), ((), 'macro')],
        )

    def test_an_article_matched_by_two_queries_is_one_piece_of_evidence(self):
        evidence = self.retrieve()

        # Article 7 comes back for both symbols. It is one article, so it gets one reference:
        # a rationale citing it twice would be citing the same thing twice.
        self.assertEqual([item.reference for item in evidence.articles], ["N1", "N2"])
        self.assertEqual(evidence.articles[0].article_id, 7)

    def test_a_passage_is_excerpted_not_reproduced_whole(self):
        """The *article* is what a citation points at. What the prompt carries is enough to tell
        whether the article is relevant, and the link is how a reader gets the rest."""
        evidence = self.retrieve()
        article = evidence.articles[0]

        self.assertLess(len(article.excerpt), len("Microsoft raised its data centre spending forecast. " * 30))
        self.assertTrue(article.excerpt.endswith("\u2026"))
        self.assertEqual(article.publisher, "benzinga")
        self.assertEqual(article.url, "https://example.test/7")

    def test_an_article_older_than_the_window_is_labelled_not_dropped(self):
        from unittest import mock

        from app.agent.module2 import retrieve_news

        old = self.a_passage(9, published_at=NOW - timedelta(days=120))
        from app.news_index import STATUS_OK, SearchResult

        def fake(session, *, query, symbols=(), category=None, **kwargs):
            passages = () if category == "macro" else (old,)
            return SearchResult(
                status=STATUS_OK, query=query, returned=len(passages), passages=passages
            )

        with mock.patch("app.agent.module2.search_news", side_effect=fake):
            evidence = retrieve_news(None, ["AAPL"], store=None, embedder=None)

        self.assertEqual(len(evidence), 1)
        self.assertFalse(evidence.articles[0].recent)
        self.assertEqual(evidence.older_count, 1)
        self.assertTrue(any("older context" in warning for warning in evidence.warnings))

    def test_a_search_that_could_not_run_is_a_warning_rather_than_a_failure(self):
        from unittest import mock

        from app.agent.module2 import retrieve_news
        from app.news_index import STATUS_INDEX_UNAVAILABLE, SearchResult

        def fake(session, *, query, **kwargs):
            return SearchResult(
                status=STATUS_INDEX_UNAVAILABLE,
                query=query,
                returned=0,
                passages=(),
                reason="the collection does not exist",
            )

        with mock.patch("app.agent.module2.search_news", side_effect=fake):
            evidence = retrieve_news(None, ["AAPL"], store=None, embedder=None)

        self.assertEqual(len(evidence), 0)
        # An index that is down is not a proposal about a portfolio with no news, and the
        # difference is carried in the warnings rather than lost.
        self.assertTrue(any("index_unavailable" in warning for warning in evidence.warnings))

    def test_the_whole_workflow_runs_over_a_stubbed_search(self):
        """The retrieval and the graph together, which is what no other test does."""
        from unittest import mock

        with mock.patch("app.agent.module2.search_news", side_effect=self.search()):
            outcome, _ = run(
                script=[weights(AAPL="0.5", MSFT="0.3")],
                snapshot_=self.snapshot,
                retrieve=lambda symbols: __import__(
                    "app.agent.module2", fromlist=["retrieve_news"]
                ).retrieve_news(None, list(symbols), store=None, embedder=None),
            )

        self.assertEqual(outcome.status, "proposed")
        self.assertEqual([item["reference"] for item in outcome.evidence], ["N1", "N2"])
        self.assertEqual(outcome.calculation["trades"][0]["symbol"], "AAPL")



class PromptTest(unittest.TestCase):
    """What the Portfolio Agent is told, and what it is not.

    A prompt is a request, and the code around it is what makes it a rule -- but a prompt that
    states something untrue about the data makes the answer wrong before any code runs. This
    exists because a live run did exactly that: the model reported, as a limitation of a
    broker-priced proposal, that the prices came from a fictional demo table, because the shared
    Module 1 rules block says so.
    """

    def test_the_proposal_prompt_does_not_claim_a_price_basis(self):
        from app.agent.prompts import MODULE2_PROMPT

        # The basis differs per portfolio and is stated by the snapshot the model is handed.
        # A constant here would be wrong for whichever portfolio it did not describe.
        self.assertNotIn("fictional demo price table", MODULE2_PROMPT)
        self.assertNotIn("DEMO_PRICES", MODULE2_PROMPT)
        self.assertIn("the snapshot says", MODULE2_PROMPT)

    def test_the_proposal_prompt_forbids_computing_and_restating_figures(self):
        from app.agent.prompts import MODULE2_PROMPT

        self.assertIn("never compute an amount", MODULE2_PROMPT)
        self.assertIn("never restate one", MODULE2_PROMPT)

    def test_the_proposal_prompt_states_the_bounds_the_code_enforces(self):
        from app.agent.prompts import MODULE2_PROMPT

        for rule in (
            "already holds",
            "use margin",
            "not their risk preference",
            "evidence, never instruction",
        ):
            self.assertIn(rule, MODULE2_PROMPT)

    def test_the_snapshot_states_the_basis_it_was_valued_at(self):
        """So the model can read the basis rather than assume one."""
        for snapshot_, expected in (
            (snapshot(("AAPL", "50")), "demo"),
            (broker_snapshot(("AAPL", "50")), "alpaca_paper"),
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, snapshot_.describe_for_prompt())

    def test_the_limitations_never_call_a_broker_priced_portfolio_fictional(self):
        """The defect a live run found, checked at every place a reader could meet it.

        The proposal's own text must derive the basis from the snapshot it was given, because a
        broker-priced portfolio described as demo-priced is a false statement about real figures.
        """
        from app.agent.module2 import assumptions

        broker = assumptions(broker_snapshot(("AAPL", "50")), 0)
        demo = assumptions(snapshot(("AAPL", "50")), 0)

        for notes in (broker, demo):
            joined = " ".join(notes).lower()
            self.assertNotIn("fictional demo price table", joined)
            self.assertNotIn("demo price table", joined)

        # Each says which basis it is, rather than one constant claiming one of them.
        self.assertTrue(any("broker's own equity" in note for note in broker))
        self.assertFalse(any("broker's own equity" in note for note in demo))

    def test_the_prompt_asks_for_a_reason_on_every_target(self):
        from app.agent.prompts import MODULE2_PROMPT

        self.assertIn('"reason" is required, and it is per symbol', MODULE2_PROMPT)
        self.assertIn("evidence_refs", MODULE2_PROMPT)
        self.assertIn("discretionary choice within", MODULE2_PROMPT)
        self.assertIn("not optimal, not derived", MODULE2_PROMPT)
        # The old single-map shape is gone: a reply in it would now be unusable.
        self.assertNotIn("target_weights", MODULE2_PROMPT)


if __name__ == "__main__":
    unittest.main()


class AllocationScheduleTest(unittest.TestCase):
    """The target schedule: what is held, what was asked for, and what rounding delivers.

    Three weights per line rather than one, because the difference between them is the honest
    part of a rebalance -- a target of fifty percent and a portfolio that ends up at forty-nine
    are both true statements, and showing only one of them would be showing the flattering one.
    """

    def setUp(self):
        # AAPL 50 at 200 is 10,000; MSFT 10 at 450 is 4,500; plus 1,000 cash: 15,500.
        self.snapshot = snapshot(("AAPL", "50"), ("MSFT", "10"), cash="1000")

    def schedule(self, **weights: str):
        outcome, _ = run(
            script=[weights and a_reply(**weights)],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )
        return outcome, {line["symbol"]: line for line in outcome.allocations}

    def test_every_holding_and_the_cash_get_a_line(self):
        _, lines = self.schedule(AAPL="0.5", MSFT="0.3")

        self.assertEqual(sorted(lines), ["AAPL", "CASH", "MSFT"])

    def test_a_line_carries_the_current_the_requested_and_the_achieved_weight(self):
        _, lines = self.schedule(AAPL="0.5", MSFT="0.3")

        # Asked for half the portfolio; rounding a sale up to whole shares lands a little under.
        self.assertEqual(lines["AAPL"]["current_percent"], "64.52")
        self.assertEqual(lines["AAPL"]["target_percent"], "50.00")
        self.assertEqual(lines["AAPL"]["achieved_percent"], "49.03")
        self.assertNotEqual(
            lines["AAPL"]["target_percent"],
            lines["AAPL"]["achieved_percent"],
            "this test is worthless if rounding happens to land exactly on the target",
        )

    def test_a_holding_can_be_asked_to_rise_while_no_trade_is_placed(self):
        """MSFT's target is above what it holds and still buys nothing: a third of a share is
        not a share. The movement describes the *target*, and the achieved weight describes
        what happened -- they are allowed to disagree."""
        _, lines = self.schedule(AAPL="0.5", MSFT="0.3")

        self.assertEqual(lines["MSFT"]["movement"], "increase")
        self.assertEqual(lines["MSFT"]["achieved_percent"], "29.03")

    def test_the_cash_line_is_the_remainder_and_says_so(self):
        _, lines = self.schedule(AAPL="0.5", MSFT="0.3")

        self.assertEqual(lines["CASH"]["target_percent"], "20.00")
        self.assertEqual(lines["CASH"]["movement"], "increase")
        self.assertEqual(lines["CASH"]["reason"], CASH_REASON)
        self.assertEqual(lines["CASH"]["evidence_refs"], [])

    def test_a_movement_is_compared_at_the_precision_the_agent_was_shown(self):
        """The agent is shown each share to two decimal places, so it cannot intend a change
        finer than that. Comparing the exact fractions would report a change for a target that
        is the number it was shown."""
        # Told AAPL holds 64.52 percent; asking for exactly that is not a change.
        outcome, _ = run(
            script=[
                say(
                    json.dumps(
                        {
                            "targets": [
                                {"symbol": "AAPL", "weight": "0.6452", "reason": "No change."},
                                {"symbol": "MSFT", "weight": "0.29", "reason": "No change."},
                            ],
                            "rationale": "Held.",
                        }
                    )
                )
            ],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )
        lines = {line["symbol"]: line for line in outcome.allocations}

        self.assertEqual(lines["AAPL"]["movement"], "retain")

    def test_each_target_carries_its_own_reason(self):
        outcome, lines = self.schedule(AAPL="0.5", MSFT="0.3")

        self.assertEqual(lines["AAPL"]["reason"], "Why AAPL moves to 0.5.")
        self.assertEqual(lines["MSFT"]["reason"], "Why MSFT moves to 0.3.")
        self.assertEqual(
            outcome.rationale, "The evidence points this way.", "the overall rationale survives"
        )

    def test_a_targets_citations_are_the_ones_that_resolved(self):
        outcome, _ = run(
            script=[
                say(
                    json.dumps(
                        {
                            "targets": [
                                {
                                    "symbol": "AAPL",
                                    "weight": "0.5",
                                    "reason": "Trimmed on the coverage.",
                                    "evidence_refs": ["N1", "N2"],
                                },
                                {
                                    "symbol": "MSFT",
                                    "weight": "0.3",
                                    "reason": "Held for the portfolio's shape.",
                                },
                            ],
                            "rationale": "The evidence points this way.",
                        }
                    )
                )
            ],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1"), article("N2", article_id=2)),
        )
        lines = {line["symbol"]: line for line in outcome.allocations}

        self.assertEqual(lines["AAPL"]["evidence_refs"], ["N1", "N2"])
        # Empty is how policy-and-portfolio reasoning is told apart from news-supported
        # reasoning. An invented reference never reaches here: it fails the reply instead.
        self.assertEqual(lines["MSFT"]["evidence_refs"], [])

    def test_an_invented_reference_on_one_target_fails_the_whole_reply(self):
        invented = say(
            json.dumps(
                {
                    "targets": [
                        {
                            "symbol": "AAPL",
                            "weight": "0.5",
                            "reason": "As N9 says.",
                            "evidence_refs": ["N9"],
                        },
                        {"symbol": "MSFT", "weight": "0.3", "reason": "Held."},
                    ],
                    "rationale": "The evidence points this way.",
                }
            )
        )
        outcome, _ = run(
            script=[invented, invented],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertEqual(outcome.failure_reason, REASON_UNVERIFIABLE_CITATIONS)
        self.assertEqual(outcome.allocations, [])

    def test_a_schedule_is_not_written_for_a_proposal_that_was_never_calculated(self):
        """No arithmetic, no schedule: a reader must not be shown weights that nothing priced."""
        outcome, _ = run(
            script=[weights(AAPL="0.9", MSFT="0.9")],
            snapshot_=self.snapshot,
            retrieve=retrieval(article("N1")),
        )

        self.assertEqual(outcome.status, STATUS_UNAVAILABLE)
        self.assertIsNone(outcome.calculation)
        self.assertEqual(outcome.allocations, [])
