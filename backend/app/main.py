"""AlphaDesk AI backend."""

import logging

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from app.db import engine, get_session
from app.models import Portfolio
from app.schemas import PortfolioOut
from app.seed import DEMO_PORTFOLIO_NAME

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaDesk AI")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check: is the API process up?

    Deliberately does not touch the database, so it keeps returning 200 even when
    PostgreSQL is down.
    """
    return {"status": "ok"}


@app.get("/health/db")
def health_db():
    """Readiness check: can the API actually reach PostgreSQL?"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        # The full diagnostic detail goes to the server log only. The response body
        # below is a constant, so no connection detail can ever reach the client.
        logger.exception("Database health check failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "unavailable"},
        )
    return {"status": "ok", "database": "connected"}


@app.get("/portfolio", response_model=PortfolioOut)
def get_portfolio(session: Session = Depends(get_session)) -> PortfolioOut:
    """Return the demo portfolio with its holdings, ordered by symbol.

    Only stored values are returned. Nothing here is derived: without current market
    prices there is no market value, profit/loss, or exposure to report.
    """
    portfolio = session.scalar(
        select(Portfolio)
        .where(Portfolio.name == DEMO_PORTFOLIO_NAME)
        # Fetch the holdings in this same query rather than letting them load lazily
        # during serialisation, which could happen after the session has closed.
        .options(selectinload(Portfolio.holdings))
    )

    if portfolio is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Portfolio {DEMO_PORTFOLIO_NAME!r} has not been seeded. "
                "Run: docker compose exec backend python -m app.seed"
            ),
        )

    return PortfolioOut.model_validate(portfolio)
