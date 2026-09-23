"""The PostgreSQL checkpointer a proposal run writes its stages into.

**What a checkpoint is for here, and what it is not.** It is not the proposal record: the row in
`rebalance_proposals` is the durable result, and it holds the frozen snapshot, the retrieved
evidence and the calculated trades. The checkpoint is the *graph's* state at each completed
stage, so a run that dies between two stages can pick up from the last one instead of beginning
again. The two are kept apart on purpose -- a proposal row is what a reader looks at, and a
thread of checkpoints is what a resumed run reads.

**One pool, opened once, for the life of the process.** LangGraph's `PostgresSaver` is happy to
take a single connection, but the API runs blocking handlers in a threadpool, so two runs can
touch the saver at once. A pool is the honest shape for that, and it is created in the
application's lifespan rather than per request: a pool per request would be a connection storm
that looks fine in development and exhausts the server under any real use.

**A second Postgres driver, deliberately.** psycopg 3 is what this package is written against;
SQLAlchemy uses psycopg2. `requirements.txt` says why the two coexist.

**Unavailable is a state, not a crash.** If the database cannot be reached or the checkpointer
cannot be built, `get_checkpointer()` returns None and proposal runs continue without
checkpointing -- they work exactly as they did before, and simply cannot be resumed. That is the
same direction `app.embeddings` and `app.vector_store` fail in: an optional capability that is
missing must not stop the application from serving what it can.

**The tables are the checkpointer's, not Alembic's.** `setup()` creates them and is idempotent.
They are deliberately not in a migration: they belong to the library, its own migration table
versions them, and a hand-written copy of its DDL would be a second definition that drifts the
first time the library changes.
"""

import logging
import threading
from typing import Any

from sqlalchemy.engine import make_url

from app.config import settings

logger = logging.getLogger(__name__)

# How many checkpoint connections the process keeps. Small: a proposal run holds one at a time
# and only ever while a stage is being written, and there is one run in flight by design.
MIN_POOL_SIZE = 1
MAX_POOL_SIZE = 4

# Seconds to wait for a connection before giving up on *building* the checkpointer. Bounded
# because this runs during startup, and a startup that hangs on an unreachable database is worse
# than one that starts without checkpointing and says so.
CONNECT_TIMEOUT_SECONDS = 5.0


def checkpoint_dsn(database_url: str | None = None) -> str:
    """The application's database URL as a psycopg 3 connection string.

    SQLAlchemy spells the driver in the scheme -- `postgresql+psycopg2://` -- and psycopg 3 does
    not understand that spelling, so the dialect is dropped and the rest is passed through
    unchanged. The password is *not* masked: this string is handed to a connection pool, and a
    masked one would fail to authenticate with a message about the wrong thing.
    """
    url = make_url(database_url or settings.database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


_lock = threading.Lock()
_pool: Any = None
_saver: Any = None


def start() -> Any:
    """Build the pool, create the checkpointer's tables, and return the saver.

    Called once, from the application's lifespan. Safe to call twice: the second call returns
    what the first built.

    Returns None rather than raising when the checkpointer cannot be brought up. A proposal run
    without it still works; it simply cannot be resumed after a crash, and `app.proposals`
    reports that honestly rather than pretending otherwise.
    """
    global _pool, _saver
    with _lock:
        if _saver is not None:
            return _saver

        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - a broken image, not a data condition
            logger.error("the checkpointer is not installed: %s", exc)
            return None

        try:
            pool = ConnectionPool(
                checkpoint_dsn(),
                min_size=MIN_POOL_SIZE,
                max_size=MAX_POOL_SIZE,
                # The three settings `PostgresSaver.from_conn_string` applies to a single
                # connection, applied to every connection the pool hands out. Autocommit because
                # the saver manages its own statements; `prepare_threshold=0` because a pooled
                # connection is reused across processes that may have changed the schema; and
                # `dict_row` because the saver reads rows by column name.
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
                # Not `open=True`: a pool that opens eagerly raises here when the database is
                # down, and this function has already decided to return None instead.
                open=False,
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
            pool.open(wait=True, timeout=CONNECT_TIMEOUT_SECONDS)
            saver = PostgresSaver(pool)
            saver.setup()
        except Exception as exc:  # noqa: BLE001 - any failure here is "no checkpointing"
            logger.error(
                "the proposal checkpointer could not be started, so proposals will run "
                "without one and cannot be resumed: %s",
                type(exc).__name__,
            )
            return None

        _pool = pool
        _saver = saver
        logger.info("proposal checkpointing is on")
        return _saver


def stop() -> None:
    """Close the pool. Called from the lifespan's shutdown."""
    global _pool, _saver
    with _lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.exception("the checkpointer pool did not close cleanly")
        _pool = None
        _saver = None


def get_checkpointer() -> Any | None:
    """The saver, or None when checkpointing is unavailable.

    None is a supported answer, not an error: `app.agent.recovery` treats a run without a
    checkpoint as one that cannot be resumed and says so.
    """
    return _saver


def in_memory():
    """A checkpointer that lives in this process, for tests.

    The real one is PostgreSQL. Tests that are about *graph* resumption rather than about the
    database use this, so they do not need a running server, a migration, or cleanup of rows
    they would otherwise leave in a shared database. The tests that are about persistence
    across processes use the real one against the test database.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


__all__ = [
    "checkpoint_dsn",
    "get_checkpointer",
    "in_memory",
    "start",
    "stop",
]
