"""AlphaDesk AI backend."""

import logging
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from app.analysis_api import router as analysis_router
from app.db import engine, get_session
from app.models import Portfolio
from app.schemas import (
    PortfolioOut,
    PortfolioValuationOut,
    ScenarioOut,
    ScenarioRequest,
    ValuationHoldingOut,
)
from app.seed import DEMO_PORTFOLIO_NAME
from app.valuation import (
    DEMO_PRICE_SOURCE,
    DEMO_PRICES,
    HoldingNotFoundError,
    MissingPriceError,
    calculate_scenario,
    calculate_valuation,
)

_HUNDRED = Decimal("100")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaDesk AI")

# The analysis chat lives in its own module: three routes, their own persistence, and a
# concurrency ceiling that has nothing to do with portfolios.
app.include_router(analysis_router)


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


@app.get("/portfolio/valuation", response_model=PortfolioValuationOut)
def get_portfolio_valuation(
    session: Session = Depends(get_session),
) -> PortfolioValuationOut:
    """Value the demo portfolio's holdings and cash against the demo prices.

    Read-only: the same rows /portfolio returns, plus arithmetic on top. The prices are
    the fictional constants in app.valuation, which is what `price_source: "demo"` says
    -- they are not live quotes, so no market timestamp is reported.
    """
    portfolio = session.scalar(
        select(Portfolio)
        .where(Portfolio.name == DEMO_PORTFOLIO_NAME)
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

    try:
        valuation = calculate_valuation(
            portfolio.holdings, portfolio.cash_balance, DEMO_PRICES
        )
    except MissingPriceError as exc:
        # 503, not 200 with a partial total: a valuation that silently omits a holding
        # would look complete while being wrong.
        raise HTTPException(
            status_code=503,
            detail=(
                f"No demo price available for held symbol(s): {', '.join(exc.symbols)}. "
                "Add them to DEMO_PRICES in app/valuation.py."
            ),
        ) from exc

    return PortfolioValuationOut(
        portfolio_id=portfolio.id,
        currency=portfolio.currency,
        price_source=DEMO_PRICE_SOURCE,
        cash_balance=valuation.cash_balance,
        holdings_value=valuation.holdings_value,
        total_value=valuation.total_value,
        cash_allocation_percent=valuation.cash_allocation_percent,
        holdings=[
            ValuationHoldingOut.model_validate(item) for item in valuation.holdings
        ],
    )


@app.post("/portfolio/scenario", response_model=ScenarioOut)
def post_portfolio_scenario(
    payload: ScenarioRequest,
    session: Session = Depends(get_session),
) -> ScenarioOut:
    """Re-price one holding and report the hypothetical impact.

    POST rather than GET because the question has a body, not a path: "what if *this*
    symbol moved by *this* much". Nothing is stored — the same rows are read, arithmetic
    is applied, and the answer is returned.

    The percentage becomes a fraction here, not in the calculation: `calculate_scenario`
    takes a fraction, so the parsing and the -100..+100 bound (enforced by
    `ScenarioRequest`) stay on this side of the boundary.
    """
    portfolio = session.scalar(
        select(Portfolio)
        .where(Portfolio.name == DEMO_PORTFOLIO_NAME)
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

    try:
        scenario = calculate_scenario(
            portfolio.holdings,
            portfolio.cash_balance,
            DEMO_PRICES,
            payload.symbol,
            payload.price_change_percent / _HUNDRED,
        )
    except HoldingNotFoundError as exc:
        held = ", ".join(sorted(holding.symbol for holding in portfolio.holdings))
        raise HTTPException(
            status_code=404,
            detail=(
                f"{exc.symbol} is not held by {DEMO_PORTFOLIO_NAME!r}. "
                f"Held: {held or 'nothing'}."
            ),
        ) from exc
    except MissingPriceError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"No demo price available for held symbol(s): {', '.join(exc.symbols)}. "
                "Add them to DEMO_PRICES in app/valuation.py."
            ),
        ) from exc

    return ScenarioOut(
        portfolio_id=portfolio.id,
        currency=portfolio.currency,
        price_source=DEMO_PRICE_SOURCE,
        symbol=scenario.symbol,
        price_change_percent=payload.price_change_percent,
        price_before=scenario.price_before,
        price_after=scenario.price_after,
        holding_value_before=scenario.holding_value_before,
        holding_value_after=scenario.holding_value_after,
        total_value_before=scenario.total_value_before,
        total_value_after=scenario.total_value_after,
        change_value=scenario.change_value,
        change_percent=scenario.change_percent,
    )
