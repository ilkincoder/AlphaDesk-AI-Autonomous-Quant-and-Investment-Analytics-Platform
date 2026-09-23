"""Finding proposal runs a restart interrupted, and continuing them.

A proposal run can die in three places, and they are not the same thing:

* **Between two stages.** The last completed stage is on the checkpoint. Resuming picks up after
  it: the retrieved evidence is not searched for again, and the targets that were already proposed
  are not proposed again. This is the case the checkpointer exists for.
* **After the last stage, before the row was written.** The graph finished and the process died
  before anything was recorded. There is nothing left to run -- the completed state is read out
  of the checkpoint and written to the *same* proposal, which is what makes finalization
  idempotent rather than a second proposal.
* **Before anything completed.** No checkpoint. The work cannot be continued, and it is reported
  as interrupted rather than restarted. Re-running it would be a new run with a new snapshot,
  which is the user's decision and not this module's.

**Resuming is not restarting, and the difference is the snapshot.** A resumed run completes
against the portfolio the interrupted run froze -- the one stored on its own row -- and never a
freshly synchronised one. Computing trades for a portfolio the original run never saw would
produce a proposal that is internally inconsistent and looks entirely ordinary. What has moved
since is reported separately, by the freshness comparison on the proposal the reader is looking
at.

**Two places call this, and neither is a background loop.** The application's lifespan asks once,
on a thread of its own, so a process that is restarted recovers what the last one left; and the
request path asks before it claims a new run, so a proposal is not left abandoned because nobody
restarted anything. Both go through `app.proposal_run.resume`, and the lease decides which of
them -- if either -- actually gets the run.
"""

import logging
import threading

from app import proposals
from app.agent.progress import NULL_PROGRESS, Progress
from app.proposal_run import resume

logger = logging.getLogger(__name__)

# One recovery at a time in this process. The database lease is what makes recovery safe *across*
# processes; this stops the startup thread and a request from both walking the same list and
# doing the same reads for an answer only one of them can act on.
_sweep = threading.Lock()


def expired(now=None) -> list[str]:
    """Every proposal still marked as running whose lease has passed.

    Reading is deliberately unguarded: noticing a row twice costs a query, and whoever acts on an
    id still has to win the lease before touching it. What this must not do is mark anything --
    the row is left exactly as it is, so a run that is genuinely still in flight somewhere is not
    disturbed by somebody looking at it.
    """
    from datetime import datetime, timezone

    cutoff = now or datetime.now(timezone.utc)
    found: list[str] = []
    for row in proposals.running():
        deadline = row.get("processing_deadline")
        if deadline is None:
            continue
        if datetime.fromisoformat(str(deadline)) <= cutoff:
            found.append(row["proposal_id"])
    return found


def recover_expired(progress: Progress = NULL_PROGRESS) -> dict[str, str]:
    """Resume every abandoned run, and say what became of each.

    Returns a mapping of proposal id to the status it ended at, which is what the tests assert
    on and what the log reports. Never raises: a run that cannot be recovered is a proposal with
    an honest status, not a fault that should take the caller down with it.
    """
    if not _sweep.acquire(blocking=False):
        return {}

    try:
        resumed: dict[str, str] = {}
        for proposal_id in expired():
            try:
                status = resume(proposal_id=proposal_id, progress=progress)
            except Exception:  # noqa: BLE001 - one bad row must not stop the others
                logger.exception("proposal %s could not be recovered", proposal_id)
                status = proposals.STATUS_INTERRUPTED
            resumed[proposal_id] = status
            logger.info("proposal %s came back as %s", proposal_id, status)
        return resumed
    finally:
        _sweep.release()


def resume_interrupted_in_background() -> threading.Thread:
    """Look for interrupted runs once, off the startup path.

    A daemon thread because the work can include a model call: a process that is shutting down
    must not be held open by a recovery it no longer needs, and the run's own budget bounds how
    long it can take either way. Nothing waits for this thread, and nothing depends on it having
    finished -- the request path asks the same question again.
    """

    def sweep() -> None:
        try:
            recover_expired()
        except Exception:  # noqa: BLE001 - startup must not be able to fail from here
            logger.exception("the startup proposal recovery sweep failed")

    thread = threading.Thread(target=sweep, name="proposal-recovery", daemon=True)
    thread.start()
    return thread


__all__ = ["expired", "recover_expired", "resume_interrupted_in_background"]
