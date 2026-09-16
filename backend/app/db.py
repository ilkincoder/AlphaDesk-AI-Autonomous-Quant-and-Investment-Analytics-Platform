"""Database engine and session handling."""

from collections.abc import Generator

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