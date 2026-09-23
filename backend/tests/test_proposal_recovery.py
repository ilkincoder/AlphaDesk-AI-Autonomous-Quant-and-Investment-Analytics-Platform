"""Checkpointing and recovery: a proposal run that a restart interrupted.

The shape of every test here is the same, and it is the shape of the failure it is about: a run
gets part of the way, the process dies, and a *newly constructed* graph over the same thread
picks it up. Only the model, the retriever and the clock are replaced -- the graph, the
checkpointer, the lease and the run's own dispatch are the real ones.

The tests that are about resumption *semantics* use an in-memory checkpointer, so they need no
server and leave nothing behind. The one that is about a checkpoint surviving a *process* uses
the real PostgreSQL saver against the test database and deletes the rows it created.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest import mock

from sqlalchemy import select, text

from app import proposals, proposal_run
from app.agent import checkpointing, module2
from app.agent.budget import RunBudget
from app.agent.module2 import RetrievedArticle, NewsEvidence
from app.models import Holding, Portfolio, RebalanceProposal
from app.portfolio_identity import DEMO_PORTFOLIO_NAME
from app.rebalance_snapshot import capture
from tests.agent_doubles import ScriptedModel, say
from tests.conversation_doubles import SessionProxy
from tests.test_tools_contracts import ToolTestCase
from tests.testdb import test_database_url

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def a_reply(**weights: str) -> tuple[str, list]:
    """A well-formed Portfolio Agent reply."""
    return say(
        __import__("json").dumps(
            {
                "targets": [
                    {"symbol": symbol, "weight": weight, "reason": f"Why {symbol}."}
                    for symbol, weight in weights.items()
                ],
                "rationale": "The evidence points this way.",
                "limitations": [],
            }
        )
    )


class CountingRetrieve:
    """A retriever that records every query it was asked for.

    The count is the point: a resumed run that searches the index again has repeated work the
    checkpoint says was already done, and this is how that would be noticed.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, symbols) -> NewsEvidence:
        self.calls.append(tuple(symbols))
        evidence = NewsEvidence()
        evidence.add(
            RetrievedArticle(
                reference="",
                article_id=7,
                title="Analyst Sees More Upside for Microsoft",
                publisher="benzinga",
                url="https://example.test/7",
                published_at=NOW - timedelta(days=1),
                category="company",
                symbols=tuple(symbols),
                excerpt="An excerpt.",
                similarity=0.71,
                is_macro=False,
                recent=True,
            )
        )
        return evidence


class BreakingModel(ScriptedModel):
    """A model that dies mid-run, the way a process does."""

    def __init__(self) -> None:
        super().__init__([])
        self.calls = 0

    def complete(self, **_: Any):
        self.calls += 1
        raise RuntimeError("the process died while the model was thinking")


class ProposalRunTestCase(ToolTestCase):
    """A portfolio, a proposal row, and the stores pointed at this transaction."""

    def setUp(self) -> None:
        super().setUp()
        self.portfolio = Portfolio(
            name=DEMO_PORTFOLIO_NAME, currency="USD", cash_balance=Decimal("1000.00")
        )
        self.session.add(self.portfolio)
        self.session.flush()
        for symbol, quantity in (("AAPL", "50"), ("MSFT", "10")):
            self.session.add(
                Holding(
                    portfolio_id=self.portfolio.id,
                    symbol=symbol,
                    quantity=Decimal(quantity),
                    average_buy_price=Decimal("100.00"),
                )
            )
        self.session.flush()
        self.snapshot = capture(self.portfolio, now=NOW)

        for target in ("app.proposals.SessionLocal", "app.rebalance_api.SessionLocal"):
            patcher = mock.patch(target, return_value=SessionProxy(self.session))
            patcher.start()
            self.addCleanup(patcher.stop)

        self.retrieve = CountingRetrieve()
        # `get_checkpointer` is a function, so it is replaced by one: patching it with the saver
        # itself would leave the callers calling an object.
        self.checkpointer = self.saver()
        for target, value in (
            ("app.proposal_run.retrieve_evidence", self.retrieve),
            ("app.agent.checkpointing.get_checkpointer", lambda: self.checkpointer),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def saver(self):
        """The checkpointer these tests run against. In-memory unless a test says otherwise."""
        return checkpointing.in_memory()

    def a_running_proposal(self, *, expired: bool = True, thread_id: str | None = None):
        """The row a crashed run leaves behind: in flight, with the snapshot it froze."""
        now = datetime.now(timezone.utc)
        identifier = proposals.new_id()
        row = RebalanceProposal(
            id=identifier,
            request_id=f"req-{identifier[:8]}",
            thread_id=thread_id or f"rebalance-{identifier}",
            status=proposals.STATUS_GENERATING,
            created_at=now - timedelta(minutes=10),
            started_at=now - timedelta(minutes=10),
            processing_deadline=(
                now - timedelta(minutes=5) if expired else now + timedelta(minutes=5)
            ),
            snapshot=self.snapshot.as_dict(),
            snapshot_fingerprint=self.snapshot.fingerprint,
            policy=module2.policy(),
            evidence=[],
            assumptions=[],
            limitations=[],
        )
        self.session.add(row)
        self.session.flush()
        return row

    def crash_the_run(self, *, client, thread_id: str, budget=None):
        """Run the graph for real until it dies, with the real checkpointer.

        This is what a process being killed looks like from the database's point of view: the
        stages that finished wrote their checkpoints, and the one that did not, did not.
        """
        graph = module2.build_module2_graph(
            client=client,
            snapshot=self.snapshot,
            budget=budget or RunBudget(),
            retrieve=self.retrieve,
            checkpointer=checkpointing.get_checkpointer(),
        )
        return graph.invoke({"stopped": False}, {"configurable": {"thread_id": thread_id}})

    def rows(self) -> list[RebalanceProposal]:
        return list(self.session.scalars(select(RebalanceProposal)).all())

    def resume(self, proposal_id: str, client) -> str:
        with mock.patch("app.proposal_run.build_client", return_value=client):
            return proposal_run.resume(proposal_id=proposal_id)


class ResumptionTest(ProposalRunTestCase):
    def test_a_run_interrupted_after_retrieval_resumes_without_searching_again(self):
        """The stage that completed is not repeated.

        The retrieval is the expensive, external half of this workflow. A resumed run that
        searched the index again could be handed a *different* set of articles -- which would put
        the citation check against evidence the model never saw, and could silently change what
        the proposal rests on.
        """
        row = self.a_running_proposal()
        with self.assertRaises(RuntimeError):
            self.crash_the_run(client=BreakingModel(), thread_id=row.thread_id)
        self.assertEqual(len(self.retrieve.calls), 1)

        status = self.resume(row.id, ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]))

        self.assertEqual(status, "proposed")
        self.assertEqual(len(self.retrieve.calls), 1, "the retrieval was repeated on resume")

    def test_a_resumed_run_keeps_the_evidence_the_original_retrieved(self):
        row = self.a_running_proposal()
        with self.assertRaises(RuntimeError):
            self.crash_the_run(client=BreakingModel(), thread_id=row.thread_id)

        self.resume(row.id, ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]))

        stored = proposals.get(row.id)
        self.assertEqual([item["reference"] for item in stored["evidence"]], ["N1"])
        self.assertEqual(stored["evidence"][0]["article_id"], 7)
        self.assertEqual(stored["evidence"][0]["publisher"], "benzinga")

    def test_a_resumed_run_does_not_create_a_second_proposal(self):
        row = self.a_running_proposal()
        with self.assertRaises(RuntimeError):
            self.crash_the_run(client=BreakingModel(), thread_id=row.thread_id)

        self.resume(row.id, ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]))

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, row.id)
        self.assertEqual(rows[0].status, "proposed")

    def test_a_resumed_run_uses_the_stored_snapshot_rather_than_a_fresh_one(self):
        """Resuming is not restarting. Completing against a newly synchronised portfolio would
        produce a proposal that is internally inconsistent and looks entirely ordinary."""
        row = self.a_running_proposal()
        with self.assertRaises(RuntimeError):
            self.crash_the_run(client=BreakingModel(), thread_id=row.thread_id)

        # The portfolio moves while the run is interrupted.
        holding = self.session.query(Holding).filter(Holding.symbol == "AAPL").one()
        holding.quantity = Decimal("900")
        self.session.flush()
        self.session.expire_all()

        self.resume(row.id, ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]))

        stored = proposals.get(row.id)
        positions = {item["symbol"]: item["quantity"] for item in stored["snapshot"]["positions"]}
        self.assertEqual(positions["AAPL"], "50.000000", "the snapshot was refreshed on resume")
        self.assertEqual(
            stored["snapshot_fingerprint"], self.snapshot.fingerprint
        )

    def test_a_resumed_proposal_says_that_it_was_resumed(self):
        """A proposal that finished after an interruption is a different article from one that
        ran straight through, and the reader is told which they have."""
        row = self.a_running_proposal()
        with self.assertRaises(RuntimeError):
            self.crash_the_run(client=BreakingModel(), thread_id=row.thread_id)

        self.resume(row.id, ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]))

        limitations = proposals.get(row.id)["limitations"]
        self.assertTrue(
            any("resumed after its run was interrupted" in item for item in limitations),
            limitations,
        )


class CompletionTest(ProposalRunTestCase):
    def test_a_graph_that_finished_before_the_crash_is_recorded_without_running_anything(self):
        """The crash landed between the last stage and the write.

        Nothing is left to compute: the finished state is in the checkpoint, and finalization
        reads it rather than running the run again. That is what makes recovering here cheap and
        idempotent instead of a second paid generation.
        """
        row = self.a_running_proposal()
        # The graph completes -- every stage ran, the checkpoints are written -- and then the
        # process dies before `finalize` is reached, which is what this invocation models.
        self.crash_the_run(
            client=ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]), thread_id=row.thread_id
        )
        self.assertEqual(proposals.get(row.id)["status"], proposals.STATUS_GENERATING)

        model = ScriptedModel([])  # no turns: any model call at all would fail the test
        status = self.resume(row.id, model)

        self.assertEqual(status, "proposed")
        self.assertEqual(model.requests, [], "the completed graph was run again")
        self.assertEqual(len(self.retrieve.calls), 1, "the retrieval was repeated")
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNotNone(proposals.get(row.id)["calculation"])

    def test_finalizing_a_completed_run_twice_is_harmless(self):
        row = self.a_running_proposal()
        self.crash_the_run(
            client=ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]), thread_id=row.thread_id
        )
        self.resume(row.id, ScriptedModel([]))
        first = proposals.get(row.id)

        # A second recovery finds a finished row and does nothing at all.
        self.assertEqual(proposal_run.resume(proposal_id=row.id), "not_applicable")

        self.assertEqual(proposals.get(row.id)["calculation"], first["calculation"])
        self.assertEqual(len(self.rows()), 1)


class RefusalTest(ProposalRunTestCase):
    def test_a_run_with_no_checkpoint_is_interrupted_rather_than_restarted(self):
        """The work cannot be continued, and saying so is the honest answer. Starting again
        would be a new run against a new snapshot, which is the user's decision."""
        row = self.a_running_proposal()
        # Nothing ran: there is no thread on the checkpointer at all.
        status = self.resume(row.id, ScriptedModel([]))

        self.assertEqual(status, proposals.STATUS_INTERRUPTED)
        stored = proposals.get(row.id)
        self.assertEqual(stored["failure_reason"], proposal_run.REASON_NOT_RESUMABLE)
        self.assertIn("before any of its stages completed", stored["failure"])
        self.assertIsNone(stored["calculation"])

    def test_a_run_without_a_snapshot_is_interrupted_rather_than_resynchronised(self):
        row = self.a_running_proposal()
        row.snapshot = {}
        self.session.flush()

        status = self.resume(row.id, ScriptedModel([]))

        self.assertEqual(status, proposals.STATUS_INTERRUPTED)
        self.assertIn(
            "recorded the portfolio", proposals.get(row.id)["failure"]
        )

    def test_a_run_with_no_checkpointing_at_all_is_interrupted_with_that_reason(self):
        """The state the codebase was in before this milestone: runnable, not resumable. A row
        left over from then must say so rather than pretend it can be picked up."""
        row = self.a_running_proposal()
        with mock.patch("app.agent.checkpointing.get_checkpointer", lambda: None):
            status = proposal_run.resume(proposal_id=row.id)

        self.assertEqual(status, proposals.STATUS_INTERRUPTED)
        self.assertEqual(
            proposals.get(row.id)["failure_reason"],
            proposal_run.REASON_CHECKPOINTING_UNAVAILABLE,
        )


class LeaseTest(ProposalRunTestCase):
    def test_a_run_that_is_still_leased_is_not_resumed(self):
        """A run genuinely in flight somewhere is left alone. Only an expired lease says the
        process that owned it is gone."""
        row = self.a_running_proposal(expired=False)

        self.assertEqual(proposal_run.resume(proposal_id=row.id), "leased")
        self.assertEqual(proposals.get(row.id)["status"], proposals.STATUS_GENERATING)

    def test_only_one_of_two_recoveries_wins_the_run(self):
        """Two processes can both notice the same abandoned row. The lease is what decides
        which one resumes it -- a check followed by an action would be a race both could win."""
        row = self.a_running_proposal()

        first = proposals.renew(row.id, seconds=60)
        second = proposals.renew(row.id, seconds=60)

        self.assertTrue(first)
        self.assertFalse(second)

    def test_the_sweep_resumes_an_expired_run_and_reports_what_it_became(self):
        from app.agent import recovery

        row = self.a_running_proposal()
        self.crash_the_run(
            client=ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]), thread_id=row.thread_id
        )
        self.retrieve.calls.clear()

        with mock.patch("app.proposal_run.build_client", return_value=ScriptedModel([])):
            outcome = recovery.recover_expired()

        self.assertEqual(outcome, {row.id: "proposed"})
        self.assertEqual(proposals.get(row.id)["status"], "proposed")

    def test_the_sweep_leaves_a_live_run_alone(self):
        from app.agent import recovery

        row = self.a_running_proposal(expired=False)

        self.assertEqual(recovery.recover_expired(), {})
        self.assertEqual(proposals.get(row.id)["status"], proposals.STATUS_GENERATING)

    def test_a_second_request_recovers_before_it_claims(self):
        """The request path asks the same question the startup sweep does, so a proposal is not
        left abandoned merely because nobody restarted anything."""
        from app.rebalance_api import _claim
        from app.schemas import RebalanceProposalRequest

        row = self.a_running_proposal()
        self.crash_the_run(
            client=ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]), thread_id=row.thread_id
        )

        with mock.patch("app.proposal_run.build_client", return_value=ScriptedModel([])):
            admission = _claim(RebalanceProposalRequest(request_id="a-new-request"))

        # The abandoned run was finished first, so the new one gets the slot.
        self.assertEqual(proposals.get(row.id)["status"], "proposed")
        self.assertIsNotNone(admission.proposal_id)
        self.assertNotEqual(admission.proposal_id, row.id)

    def test_a_retry_of_the_abandoned_request_gets_the_recovered_proposal(self):
        """The same request id, arriving after the run was recovered, is answered from the row
        rather than paying for a second generation."""
        from app.rebalance_api import _claim
        from app.schemas import RebalanceProposalRequest

        row = self.a_running_proposal()
        self.crash_the_run(
            client=ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]), thread_id=row.thread_id
        )

        with mock.patch("app.proposal_run.build_client", return_value=ScriptedModel([])):
            admission = _claim(RebalanceProposalRequest(request_id=row.request_id))

        self.assertIsNone(admission.proposal_id)
        self.assertIsNotNone(admission.response)
        self.assertEqual(admission.response.status_code, 200)
        self.assertEqual(proposals.get(row.id)["status"], "proposed")


class PostgresCheckpointTest(ProposalRunTestCase):
    """The checkpoint surviving the process that wrote it.

    Everything else here uses an in-memory saver, which proves the *graph* resumes but says
    nothing about persistence. This one writes to PostgreSQL through a first connection, reads it
    back through a second that has never seen the first, and deletes what it wrote.
    """

    def saver(self):
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg import Connection
        from psycopg.rows import dict_row

        self.dsn = checkpointing.checkpoint_dsn(test_database_url())
        self.connections = []

        def connect():
            connection = Connection.connect(
                self.dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
            )
            self.connections.append(connection)
            saver = PostgresSaver(connection)
            saver.setup()
            return saver

        self.connect = connect
        return connect()

    def tearDown(self) -> None:
        # Only this test's own thread is removed. Nothing here drops a table or touches another
        # row: a shared database is not a test's to clear out.
        thread_ids = [
            row.thread_id
            for row in self.session.scalars(select(RebalanceProposal)).all()
            if row.thread_id
        ]
        super().tearDown()
        if not thread_ids:
            return
        from sqlalchemy import create_engine

        engine = create_engine(test_database_url())
        try:
            with engine.begin() as connection:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE thread_id = ANY(:ids)"),
                        {"ids": thread_ids},
                    )
        finally:
            engine.dispose()
            for connection in getattr(self, "connections", []):
                connection.close()

    def test_the_checkpoint_outlives_the_connection_that_wrote_it(self):
        row = self.a_running_proposal()
        with self.assertRaises(RuntimeError):
            self.crash_the_run(client=BreakingModel(), thread_id=row.thread_id)
        self.assertEqual(len(self.retrieve.calls), 1)

        # A second connection, a second saver: nothing is shared with the first except the
        # database and the thread id.
        self.checkpointer = self.connect()
        with mock.patch("app.agent.checkpointing.get_checkpointer", lambda: self.checkpointer):
            status = self.resume(row.id, ScriptedModel([a_reply(AAPL="0.5", MSFT="0.3")]))

        self.assertEqual(status, "proposed")
        self.assertEqual(len(self.retrieve.calls), 1, "the retrieval was repeated on resume")
        self.assertEqual(proposals.get(row.id)["evidence"][0]["article_id"], 7)


if __name__ == "__main__":
    unittest.main()
