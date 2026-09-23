"""The proposal API: generating, retrying, refusing, and reading one back.

Every test runs the real routes against the real database, with the *work* replaced by a scripted
runner. That split is deliberate, and it is the same one `test_conversations_api.py` makes: what
this file exists to check -- duplicate suppression, the single run slot, recovery from an
interrupted run, and the currency of a stored proposal -- lives in the API and the store, and
replacing those with mocks would leave nothing under test but the mocks.

The route functions are called directly rather than through a TestClient, which is how the rest
of this suite tests handlers.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import asyncio
import json
import queue
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest import mock

from fastapi.responses import JSONResponse, StreamingResponse

from app import proposals
from app.models import Holding, Portfolio, RebalanceProposal
from app.portfolio_identity import DEMO_PORTFOLIO_NAME
from app.agent.progress import KIND_DONE, KIND_STAGE, ProgressEvent
from app.rebalance_api import (
    RETRY_AFTER_SECONDS,
    _drain,
    get_proposal,
    post_proposal,
    post_proposal_stream,
)
from app.agent.module2 import policy as proposal_policy
from app.rebalance_snapshot import capture
from app.schemas import RebalanceProposalRequest
from tests.conversation_doubles import SessionProxy
from tests.test_tools_contracts import ToolTestCase

REQUEST = "req-proposal-1"


def a_request(**overrides: Any) -> RebalanceProposalRequest:
    return RebalanceProposalRequest(**{"request_id": REQUEST, **overrides})


def a_snapshot(portfolio: Portfolio) -> dict:
    """The snapshot the real workflow would freeze, taken from this test's portfolio."""
    return capture(portfolio, now=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc))


def a_calculation() -> dict:
    """A stored calculation with the shape the real one has.

    Written out rather than produced by running the calculation, because these tests are about
    the API rather than the arithmetic -- that is checked in `test_rebalance.py`, and the
    workflow that joins the two in `test_rebalance_agent.py`.
    """
    return {
        "outcome": "proposed",
        "trades": [
            {
                "symbol": "AAPL",
                "action": "sell",
                "quantity": "5",
                "reference_price": "200.00",
                "estimated_value": "1000.00",
                "quantity_before": "50",
                "quantity_after": "45",
            }
        ],
        "targets": {"AAPL": "0.5", "MSFT": "0.3"},
        "cash_target": "3100.00",
        "before": {
            "total_value": "15500.00",
            "cash_balance": "1000.00",
            "cash_allocation_percent": "6.45",
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": "50",
                    "price": "200.00",
                    "holding_value": "10000.00",
                    "allocation_percent": "64.52",
                },
                {
                    "symbol": "MSFT",
                    "quantity": "10",
                    "price": "450.00",
                    "holding_value": "4500.00",
                    "allocation_percent": "29.03",
                },
            ],
        },
        "after": {
            "total_value": "15500.00",
            "cash_balance": "3400.00",
            "cash_allocation_percent": "21.94",
            "holdings": [
                {
                    "symbol": "AAPL",
                    "quantity": "45",
                    "price": "200.00",
                    "holding_value": "9000.00",
                    "allocation_percent": "58.06",
                },
                {
                    "symbol": "MSFT",
                    "quantity": "10",
                    "price": "450.00",
                    "holding_value": "4500.00",
                    "allocation_percent": "29.03",
                },
            ],
        },
        "cash_before": "1000.00",
        "cash_after": "3400.00",
        "sell_proceeds": "1000.00",
        "buy_cost": "0.00",
        "buys_depend_on_sells": False,
        "largest_before": {"symbol": "AAPL", "allocation_percent": "64.52"},
        "largest_after": {"symbol": "AAPL", "allocation_percent": "58.06"},
        "scenario": {
            "symbol": "AAPL",
            "price_change_percent": "-10",
            "price_before": "200.00",
            "price_after": "180.00",
            "holding_value_before": "10000.00",
            "holding_value_after": "9000.00",
            "total_value_before": "15500.00",
            "total_value_after": "14500.00",
            "change_value": "-1000.00",
            "change_percent": "-6.45",
        },
        "reconciliation": {
            "reported_total_value": None,
            "summed_total_value": "15500.00",
            "difference": None,
            "note": "No broker-reported equity, so there is nothing to reconcile against.",
        },
    }


def an_evidence_list() -> list[dict]:
    return [
        {
            "reference": "N1",
            "article_id": 7,
            "title": "Microsoft raises its data centre forecast",
            "publisher": "benzinga",
            "url": "https://example.test/7",
            "published_at": "2026-09-22T14:00:00+00:00",
            "category": "company",
            "symbols": ["MSFT"],
            "similarity": 0.71,
            "recent": True,
        }
    ]


def record(proposal_id: str, portfolio: Portfolio, *, status: str = "proposed", **overrides: Any) -> None:
    """Write the row a completed run would write, through the real store."""
    snapshot = a_snapshot(portfolio)
    fields: dict[str, Any] = {
        "status": status,
        "snapshot": snapshot.as_dict(),
        "snapshot_fingerprint": snapshot.fingerprint,
        "policy": proposal_policy(),
        "targets": {"AAPL": "0.5", "MSFT": "0.3"},
        "rationale": "The evidence points this way.",
        "evidence": an_evidence_list(),
        "calculation": None if status != "proposed" else a_calculation(),
        "assumptions": ["Existing long stock holdings plus cash only."],
        "limitations": ["Estimated only. Nothing was sent to a broker."],
        "usage": {"model_requests": 1, "total_tokens": 1200},
        "run_id": "run-abc",
    }
    fields.update(overrides)
    proposals.finalize(proposal_id=proposal_id, **fields)


class ScriptedProposal:
    """A stand-in for `rebalance_api.generate`, recording what it was asked to do.

    Deliberately not a mock. It writes through the real `proposals.finalize`, so the route's
    response is shaped from a row that really exists -- which is the property the page depends
    on, and the one a mock would hide.
    """

    def __init__(self, portfolio: Portfolio, *, status: str = "proposed", raises=None) -> None:
        self.portfolio = portfolio
        self.status = status
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, *, proposal_id: str, thread_id: str, progress=None) -> None:
        self.calls.append({"proposal_id": proposal_id, "thread_id": thread_id})
        if self.raises is not None:
            raise self.raises
        record(proposal_id, self.portfolio, status=self.status)


class ProposalApiTestCase(ToolTestCase):
    """A portfolio to propose against, and the proposal store on this transaction."""

    def setUp(self) -> None:
        super().setUp()
        self.portfolio = Portfolio(
            name=DEMO_PORTFOLIO_NAME, currency="USD", cash_balance=Decimal("1000.00")
        )
        self.session.add(self.portfolio)
        self.session.flush()
        self.add_position("AAPL", "50")
        self.add_position("MSFT", "10")

        # Both stores, and the snapshot comparison, read through `app.db.SessionLocal`. Pointing
        # them at this transaction is what keeps a test from writing proposals into the
        # development database and then asserting against rows nothing else can see.
        self.sessions = mock.patch(
            "app.proposals.SessionLocal", return_value=SessionProxy(self.session)
        )
        self.sessions.start()
        self.addCleanup(self.sessions.stop)
        self.api_sessions = mock.patch(
            "app.rebalance_api.SessionLocal", return_value=SessionProxy(self.session)
        )
        self.api_sessions.start()
        self.addCleanup(self.api_sessions.stop)

    def add_position(self, symbol: str, quantity: str) -> None:
        self.session.add(
            Holding(
                portfolio_id=self.portfolio.id,
                symbol=symbol,
                quantity=Decimal(quantity),
                average_buy_price=Decimal("100.00"),
            )
        )
        self.session.flush()

    def link_to_a_broker(self, *, equity: str = "15500.00") -> None:
        """Give this portfolio a broker link and a stored price per position.

        A synchronised portfolio is valued at the broker's own prices rather than the fictional
        demo table, which is the only basis on which a price can move at all.
        """
        self.portfolio.broker = "alpaca_paper"
        self.portfolio.broker_account_id = "acct-1"
        self.portfolio.broker_equity = Decimal(equity)
        self.portfolio.last_synced_at = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        for holding, price in zip(
            sorted(self.portfolio.holdings, key=lambda item: item.symbol),
            ("200.00", "450.00"),
        ):
            holding.market_price = Decimal(price)
        self.session.flush()
        self.session.expire_all()

    def generate(self, **runner_kwargs) -> ScriptedProposal:
        runner = ScriptedProposal(self.portfolio, **runner_kwargs)
        return runner

    def call(self, payload: RebalanceProposalRequest, runner) -> JSONResponse:
        return post_proposal(payload, runner=runner)

    def stored(self) -> list[RebalanceProposal]:
        return list(
            self.session.query(RebalanceProposal)
            .order_by(RebalanceProposal.created_at)
            .all()
        )


class GenerationTest(ProposalApiTestCase):
    def test_a_proposal_is_generated_and_returned(self):
        runner = self.generate()

        response = self.call(a_request(), runner)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(runner.call_count, 1)
        body = json.loads(response.body)
        proposal = body["proposal"]
        # Decimals travel as strings: a JSON number is a double, and the arithmetic was Decimal.
        self.assertEqual(proposal["calculation"]["trades"][0]["estimated_value"], "1000.00")
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(
            proposal["evidence"][0]["title"], "Microsoft raises its data centre forecast"
        )
        self.assertEqual(proposal["evidence"][0]["url"], "https://example.test/7")
        self.assertEqual(proposal["rationale"], "The evidence points this way.")
        self.assertTrue(proposal["limitations"])

    def test_the_thread_id_is_recorded_but_never_returned(self):
        """One run, one thread -- and it is an implementation detail the page must not show."""
        runner = self.generate()

        response = self.call(a_request(), runner)

        body = json.loads(response.body)
        self.assertNotIn("thread_id", body["proposal"])
        self.assertNotIn("thread_id", json.dumps(body))
        self.assertTrue(runner.calls[0]["thread_id"].startswith("rebalance-"))
        self.assertEqual(
            self.session.query(RebalanceProposal).one().thread_id,
            runner.calls[0]["thread_id"],
        )

    def test_the_snapshot_the_proposal_was_computed_from_is_stored_and_returned(self):
        runner = self.generate()

        proposal = json.loads(self.call(a_request(), runner).body)["proposal"]

        self.assertEqual(proposal["snapshot"]["cash_balance"], "1000.00")
        self.assertEqual(proposal["snapshot"]["total_value"], "15500.00")
        self.assertEqual(
            [item["symbol"] for item in proposal["snapshot"]["positions"]], ["AAPL", "MSFT"]
        )

    def test_the_policy_the_targets_were_proposed_under_is_on_the_record(self):
        runner = self.generate()

        proposal = json.loads(self.call(a_request(), runner).body)["proposal"]

        self.assertEqual(proposal["policy"]["name"], "alphadesk_demo_whole_share")


class DuplicateTest(ProposalApiTestCase):
    def test_the_same_request_id_twice_generates_once(self):
        """The request id is the whole duplicate-suppression story, and the second call must
        not pay for a second workflow."""
        runner = self.generate()

        first = self.call(a_request(), runner)
        second = self.call(a_request(), runner)

        self.assertEqual(runner.call_count, 1)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            json.loads(first.body)["proposal"]["proposal_id"],
            json.loads(second.body)["proposal"]["proposal_id"],
        )
        self.assertEqual(len(self.stored()), 1)

    def test_a_request_already_in_flight_is_answered_as_still_running(self):
        """The *same* request id, still running: 202 rather than 200, because there is nothing
        to read yet and a page that treated an empty proposal as a finished one would show an
        empty section."""
        now = datetime.now(timezone.utc)
        self.session.add(
            RebalanceProposal(
                id="a" * 32,
                request_id=REQUEST,
                status=proposals.STATUS_GENERATING,
                created_at=now,
                started_at=now,
                processing_deadline=now + timedelta(minutes=5),
                snapshot={},
                snapshot_fingerprint="",
                policy={},
                evidence=[],
                assumptions=[],
                limitations=[],
            )
        )
        self.session.flush()
        runner = self.generate()

        response = self.call(a_request(), runner)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(json.loads(response.body)["proposal"]["status"], "generating")
        self.assertEqual(runner.call_count, 0)

    def test_a_second_request_while_one_runs_is_refused_rather_than_queued(self):
        """A *different* request id while one is running. Refused rather than queued: a queue
        would turn a slow provider into a backlog, and both runs would be reasoning about the
        same portfolio at once."""
        now = datetime.now(timezone.utc)
        self.session.add(
            RebalanceProposal(
                id="b" * 32,
                request_id="someone-else",
                status=proposals.STATUS_GENERATING,
                created_at=now,
                started_at=now,
                processing_deadline=now + timedelta(minutes=5),
                snapshot={},
                snapshot_fingerprint="",
                policy={},
                evidence=[],
                assumptions=[],
                limitations=[],
            )
        )
        self.session.flush()
        runner = self.generate()

        response = self.call(a_request(request_id="a-different-request"), runner)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.headers["Retry-After"], str(RETRY_AFTER_SECONDS))
        self.assertIn("One runs at a time", json.loads(response.body)["detail"])
        self.assertEqual(runner.call_count, 0)

    def test_an_interrupted_run_does_not_block_the_slot_for_ever(self):
        """The row is recovered lazily, by the next request that touches the table. Nothing is
        replayed: the abandoned run is marked, and this request starts a new one."""
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        self.session.add(
            RebalanceProposal(
                id="c" * 32,
                request_id="abandoned",
                status=proposals.STATUS_GENERATING,
                created_at=long_ago,
                started_at=long_ago,
                processing_deadline=long_ago,
                snapshot={},
                snapshot_fingerprint="",
                policy={},
                evidence=[],
                assumptions=[],
                limitations=[],
            )
        )
        self.session.flush()
        runner = self.generate()

        response = self.call(a_request(), runner)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(runner.call_count, 1)
        abandoned = self.session.get(RebalanceProposal, "c" * 32)
        self.assertEqual(abandoned.status, proposals.STATUS_INTERRUPTED)
        self.assertIsNotNone(abandoned.failure)

    def test_a_runner_that_raises_leaves_no_run_stuck(self):
        """A run that raised has recorded nothing, and a row left saying `generating` would hold
        the only slot for ever."""
        runner = self.generate(raises=RuntimeError("something broke inside the run"))

        response = self.call(a_request(), runner)

        self.assertEqual(response.status_code, 200)
        proposal = json.loads(response.body)["proposal"]
        self.assertEqual(proposal["status"], "unavailable")
        self.assertEqual(proposal["failure_reason"], "service_failed")
        self.assertIn("fault in the application", proposal["failure"])
        stored = self.session.query(RebalanceProposal).one()
        self.assertNotEqual(stored.status, proposals.STATUS_GENERATING)

    def test_an_unavailable_proposal_is_still_returned_and_readable(self):
        runner = self.generate(status="unavailable")

        response = self.call(a_request(), runner)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["proposal"]["status"], "unavailable")


class ReadingTest(ProposalApiTestCase):
    def test_nothing_generated_is_an_explicit_null_not_an_error(self):
        """A page restoring itself is asking "is there one?", and "no" is an answer."""
        view = get_proposal()

        self.assertIsNone(view.proposal)

    def test_the_latest_proposal_is_returned_after_a_reload(self):
        runner = self.generate()
        self.call(a_request(), runner)

        view = get_proposal()

        self.assertIsNotNone(view.proposal)
        self.assertEqual(view.proposal.status, "proposed")
        self.assertEqual(view.proposal.calculation.trades[0].estimated_value, Decimal("1000.00"))
        self.assertEqual(view.proposal.evidence[0].publisher, "benzinga")

    def test_a_proposal_whose_basis_has_not_moved_is_current(self):
        runner = self.generate()
        self.call(a_request(), runner)

        view = get_proposal()

        self.assertEqual(view.proposal.freshness, "current")
        self.assertIsNone(view.proposal.freshness_reason)
        self.assertEqual(view.proposal.freshness_changed, [])

    def test_a_price_only_change_is_not_a_changed_portfolio(self):
        """The distinction this whole comparison exists for.

        A synchronised portfolio is re-priced whenever the broker is read. Every figure in the
        proposal was computed at the prices of its own snapshot, and they are still the prices
        that portfolio is valued at -- so the proposal is a faithful estimate of what was
        proposed against what was held, and saying it "needs regenerating" would be saying
        something untrue about the holdings.
        """
        self.link_to_a_broker()
        runner = self.generate()
        self.call(a_request(), runner)
        holding = self.session.query(Holding).filter(Holding.symbol == "MSFT").one()
        holding.market_price = Decimal("500.00")
        self.session.flush()
        self.session.expire_all()

        view = get_proposal()

        self.assertEqual(view.proposal.freshness, "prices_updated")
        self.assertIn("MSFT from 450.00 to 500.00", view.proposal.freshness_reason)
        self.assertIn("same positions in the same quantities", view.proposal.freshness_reason)
        # Still readable as a proposal, and every number is the one it was generated with.
        self.assertIsNotNone(view.proposal.calculation)
        self.assertEqual(
            view.proposal.calculation.trades[0].estimated_value, Decimal("1000.00")
        )

    def test_a_quantity_change_is_a_changed_portfolio(self):
        runner = self.generate()
        self.call(a_request(), runner)
        holding = self.session.query(Holding).filter(Holding.symbol == "AAPL").one()
        holding.quantity = Decimal("60")
        self.session.flush()
        self.session.expire_all()

        view = get_proposal()

        self.assertEqual(view.proposal.freshness, "portfolio_changed")
        self.assertIn("AAPL's quantity moved", view.proposal.freshness_reason)
        self.assertIn("needs regenerating", view.proposal.freshness_reason)

    def test_a_position_closed_since_the_proposal_is_named(self):
        runner = self.generate()
        self.call(a_request(), runner)
        self.session.delete(
            self.session.query(Holding).filter(Holding.symbol == "MSFT").one()
        )
        self.session.flush()
        self.session.expire_all()

        view = get_proposal()

        self.assertEqual(view.proposal.freshness, "portfolio_changed")
        self.assertIn("MSFT is no longer held", view.proposal.freshness_reason)

    def test_a_cash_change_alone_is_a_changed_portfolio(self):
        """Cash is not a price. It moves only when the account moves."""
        runner = self.generate()
        self.call(a_request(), runner)
        self.portfolio.cash_balance = Decimal("2000.00")
        self.session.flush()
        self.session.expire_all()

        view = get_proposal()

        self.assertEqual(view.proposal.freshness, "portfolio_changed")
        self.assertIn("cash moved", view.proposal.freshness_reason)

    def test_a_resync_that_changed_nothing_leaves_the_proposal_current(self):
        """A timestamp is not a change. `last_synced_at` moves every time the broker is read,
        including when it re-reads an account that has not moved -- so the comparison is on the
        values, and this is what that means in practice."""
        self.link_to_a_broker()
        runner = self.generate()
        self.call(a_request(), runner)

        self.portfolio.last_synced_at = datetime.now(timezone.utc)
        self.session.flush()
        self.session.expire_all()

        view = get_proposal()

        self.assertEqual(view.proposal.freshness, "current")

    def test_equity_moving_alone_is_not_a_changed_portfolio(self):
        """The broker's equity is a share of the *current* allocation, so it rises and falls with
        prices. Treating it as structural would report a portfolio that has merely been
        re-priced as one whose holdings had changed."""
        self.link_to_a_broker()
        runner = self.generate()
        self.call(a_request(), runner)

        self.portfolio.broker_equity = Decimal("16000.00")
        self.session.flush()
        self.session.expire_all()

        view = get_proposal()

        self.assertEqual(view.proposal.freshness, "current")

    def test_a_portfolio_that_cannot_be_read_is_unknown_not_changed(self):
        """An outage is not a fact about the holdings. Reporting "the portfolio changed" on the
        strength of not being able to look at it is the one answer that must never be given."""
        runner = self.generate()
        self.call(a_request(), runner)

        with mock.patch(
            "app.rebalance_api.compare", side_effect=RuntimeError("connection reset")
        ):
            view = get_proposal()

        self.assertEqual(view.proposal.freshness, "unknown")
        self.assertIn("could not be read", view.proposal.freshness_reason)
        self.assertNotIn("changed", view.proposal.freshness_reason.split("not")[-1][:9])

    def test_a_portfolio_that_cannot_be_priced_is_unknown(self):
        """Held but unpriceable: neither current nor changed, and said so."""
        self.link_to_a_broker()
        runner = self.generate()
        self.call(a_request(), runner)
        holding = self.session.query(Holding).filter(Holding.symbol == "MSFT").one()
        holding.market_price = None
        self.session.flush()
        self.session.expire_all()

        view = get_proposal()

        self.assertEqual(view.proposal.freshness, "unknown")
        self.assertIn("MSFT", view.proposal.freshness_changed)

    def test_an_unavailable_proposal_is_returned_with_its_reason(self):
        runner = self.generate(
            status="unavailable"
        )
        self.call(a_request(), runner)
        proposals.finalize(
            proposal_id=self.session.query(RebalanceProposal).one().id,
            status="unavailable",
            failure="No article could be retrieved for this portfolio.",
            failure_reason="no_evidence",
        )

        view = get_proposal()

        self.assertEqual(view.proposal.status, "unavailable")
        self.assertEqual(view.proposal.failure_reason, "no_evidence")
        self.assertIsNone(view.proposal.calculation)


class StreamTest(ProposalApiTestCase):
    def test_a_duplicate_is_answered_as_json_rather_than_as_a_stream(self):
        """The stream only begins once a run has genuinely been claimed, so an error never
        arrives as a half-open event stream."""
        runner = self.generate()
        self.call(a_request(), runner)

        response = post_proposal_stream(a_request(), runner=runner)

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(runner.call_count, 1)

    def test_a_refused_run_is_answered_as_json_too(self):
        now = datetime.now(timezone.utc)
        self.session.add(
            RebalanceProposal(
                id="d" * 32,
                request_id="someone-else",
                status=proposals.STATUS_GENERATING,
                created_at=now,
                started_at=now,
                processing_deadline=now + timedelta(minutes=5),
                snapshot={},
                snapshot_fingerprint="",
                policy={},
                evidence=[],
                assumptions=[],
                limitations=[],
            )
        )
        self.session.flush()
        runner = self.generate()

        response = post_proposal_stream(
            a_request(request_id="other"), runner=runner
        )

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 409)

    def test_a_claimed_run_streams_events_and_the_proposal_itself(self):
        """The stream reports what happened, and its last event is the proposal -- so a client
        whose stream broke can still read the outcome from the store."""
        runner = self.generate()

        response = post_proposal_stream(a_request(), runner=runner)

        self.assertEqual(response.media_type, "text/event-stream")
        # A buffered event stream arrives all at once at the end, which is the behaviour this
        # route exists to avoid.
        self.assertIn("no-transform", response.headers["Cache-Control"])
        self.assertEqual(runner.call_count, 1)

        frames = asyncio.run(_read(response)).decode()
        last = json.loads(
            [line[5:] for line in frames.splitlines() if line.startswith("data:")][-1]
        )
        self.assertEqual(last["event"], "done")
        self.assertEqual(last["proposal"]["status"], "proposed")
        self.assertEqual(
            last["proposal"]["calculation"]["trades"][0]["symbol"], "AAPL"
        )

    def test_a_stage_event_names_a_stage_the_run_actually_has(self):
        """The same rule the analysis stream follows: a real step, emitted by the code that did
        it, never a timer or a percentage. Checking the framing here rather than through a live
        worker keeps the assertion about the wire format and nothing else."""
        events: "queue.Queue[ProgressEvent]" = queue.Queue()

        def feed() -> None:
            events.put(
                ProgressEvent(KIND_STAGE, {"stage": "calculating", "detail": "Pricing trades"})
            )
            events.put(ProgressEvent(KIND_DONE, {"proposal": {"status": "no_change"}}))

        threading.Thread(target=feed, daemon=True).start()

        frames = "".join(_drain(events))

        self.assertEqual(
            frames,
            'event: stage\n'
            'data: {"event": "stage", "stage": "calculating", "detail": "Pricing trades"}\n'
            "\n"
            "event: done\n"
            'data: {"event": "done", "proposal": {"status": "no_change"}}\n'
            "\n",
        )


async def _read(response: StreamingResponse) -> bytes:
    """Consume a streaming response to completion.

    Starlette wraps a synchronous generator in an async iterator, so the body is read the way a
    client's HTTP library would rather than by joining the generator directly.
    """
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    return b"".join(chunks)


if __name__ == "__main__":
    unittest.main()
