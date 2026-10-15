"""The database's conventions, in one place.

The engine and the session lifecycle, plus the three things every stored record in this
application is written with: an opaque id, the UTC clock, and a transaction that joins a
caller's rather than starting its own. Each of those was duplicated across the four modules
that own a table -- `backtests`, `conversations`, `orders` and `proposals` -- and each copy
carried its own version of the reason. They are one rule, so they are written once.
"""

import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    # Test a pooled connection before handing it out. Without this, a connection
    # that died while Postgres was restarting would still be handed to /health/db,
    # and the endpoint would keep failing until the backend itself was restarted.
    pool_pre_ping=True,
    # Seconds. Bounds how long establishing a connection may take, so an
    # unreachable database yields a 503 promptly instead of hanging on the
    # operating system's TCP timeout.
    connect_args={"connect_timeout": 5},
)

SessionLocal = sessionmaker(bind=engine)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, always closed.

    The `with` block closes the session even when the request raises, so the
    connection goes back to the pool instead of leaking.
    """
    with SessionLocal() as session:
        yield session


def new_id() -> str:
    """A server-generated identifier.

    Random rather than a sequence: a small integer would let anyone holding one id enumerate
    the records beside it, and how many of them there are.
    """
    return uuid.uuid4().hex


def utc_now() -> datetime:
    """The clock every stored timestamp is written from.

    UTC and timezone-aware, so a record written here and one written by a provider can be
    compared without either being guessed at.
    """
    return datetime.now(timezone.utc)


@contextmanager
def transaction(session: Session) -> Iterator[None]:
    """One atomic unit of work, inside a caller's transaction if there is one.

    Normally this opens and commits its own. If the session is *already* in a transaction --
    because a caller opened one and handed the session in -- this joins it and leaves the
    commit to whoever owns it. Committing a transaction somebody else began would end work
    they were still assembling, and rolling it back would discard work they had already done.
    """
    if session.in_transaction():
        yield
    else:
        with session.begin():
            yield