"""AlphaDesk AI backend."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import broker_sync, portfolio_prices
from app.alpaca import (
    AlpacaError,
    DataValidationError,
    MalformedResponseError,
    MissingCredentialsError,
)
from app.agent import checkpointing, recovery
from app.analysis_api import router as analysis_router
from app.db import engine, get_session
from app.news_api import router as news_router
from app.rebalance_api import router as rebalance_router
from app.models import Portfolio
from app.schemas import (
    PortfolioOut,
    PortfolioSyncOut,
    PortfolioValuationOut,
    ScenarioOut,
    ScenarioRequest,
    ValuationHoldingOut,
)
from app.portfolio_identity import DEMO_PORTFOLIO_NAME, find
from app.valuation import (
    HoldingNotFoundError,
    MissingPriceError,
    calculate_scenario,
    calculate_valuation,
)

_HUNDRED = Decimal("100")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """What the process owns for its whole life, rather than per request.

    Two things, and both are deliberate about *when* they happen.

    **The proposal checkpointer's pool is opened once, here.** A pool per request would be a
    connection storm; a pool built lazily inside the first run would be created on a request
    thread with no one to close it. It is created before the app serves anything and closed
    after it stops. If it cannot be started, the application still serves -- see
    `app.agent.checkpointing`.

    **Interrupted proposals are looked for once the app is up, on a thread of their own.** A
    run that was in flight when the process died is recovered by being *resumed*, which can mean
    a model call; doing that inline would hold the server's own startup hostage to a provider.
    The thread is a daemon and the work is bounded by the run's budget, so a process that is
    shutting down is not kept alive by it, and a second recovery cannot start while one is
    running -- see `app.agent.recovery`.
    """
    checkpointing.start()
    recovery.resume_interrupted_in_background()
    try:
        yield
    finally:
        checkpointing.stop()


app = FastAPI(title="AlphaDesk AI", lifespan=lifespan)

# The analysis chat lives in its own module: three routes, their own persistence, and a
# concurrency ceiling that has nothing to do with portfolios.
app.include_router(analysis_router)

# News is a second corpus with its own store, its own collection and its own ingestion.
app.include_router(news_router)

# The rebalance proposal. Its own table, its own single-run slot, and no route that could reach
# a broker -- it reads the portfolio, proposes targets and prices trades, and stops there.
app.include_router(rebalance_router)


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


def _load_portfolio(session: Session) -> Portfolio:
    """The portfolio, with its holdings, or a 404 naming the command that creates it.

    One lookup for all four routes below, under either of the portfolio's two names.
    """
    portfolio = find(session)

    if portfolio is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Portfolio {DEMO_PORTFOLIO_NAME!r} has not been seeded. "
                "Run: docker compose exec backend python -m app.seed"
            ),
        )
    return portfolio


@app.get("/portfolio", response_model=PortfolioOut)
def get_portfolio(session: Session = Depends(get_session)) -> PortfolioOut:
    """Return the portfolio with its holdings, ordered by symbol.

    Only stored values are returned. Nothing here is derived: the broker's own price and
    market value are stored columns, and there is no profit/loss or exposure to report
    from them here. Both are null until the first successful sync.
    """
    return PortfolioOut.model_validate(_load_portfolio(session))


@app.post("/portfolio/sync", response_model=PortfolioSyncOut)
def post_portfolio_sync(session: Session = Depends(get_session)) -> PortfolioSyncOut:
    """Read the Alpaca paper account and store it as this portfolio's snapshot.

    The one route that writes. It sends nothing to the broker -- two GETs, an account and
    its positions -- and it places no orders, because no order endpoint exists in
    `app.alpaca`.

    Everything is validated before anything is written, and the whole update is one
    transaction, so a failure anywhere leaves the previous snapshot and its timestamp
    exactly as they were. Concurrent calls are rejected rather than queued.
    """
    try:
        outcome = broker_sync.sync(session)
    except broker_sync.SyncInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except broker_sync.AccountMismatchError as exc:
        # 409, not 503: nothing is broken, and nothing will be until a person decides
        # which account this portfolio belongs to.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except broker_sync.PortfolioNotStoredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (MissingCredentialsError, DataValidationError, MalformedResponseError) as exc:
        # The credential is missing, or Alpaca answered something this build cannot store.
        # Both are "cannot sync right now, and here is what would fix it", which is what
        # 503 says; the message is the client's, and never contains a key.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AlpacaError as exc:
        # Unreachable, timed out, rate-limited, credentials refused.
        logger.warning("portfolio sync failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except broker_sync.SyncError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return PortfolioSyncOut(**vars(outcome))


@app.get("/portfolio/valuation", response_model=PortfolioValuationOut)
def get_portfolio_valuation(
    session: Session = Depends(get_session),
) -> PortfolioValuationOut:
    """Value the portfolio's holdings and cash against the prices that describe it.

    Read-only: the same rows /portfolio returns, plus arithmetic on top. Which prices
    those are is the portfolio's business, not this handler's -- `app.portfolio_prices`
    answers it, and `price_source` reports the answer. A synchronised portfolio is valued
    at the broker's own prices; one that has never been synchronised is valued at the
    fictional demo table, and says so.
    """
    portfolio = _load_portfolio(session)
    basis = _price_basis(portfolio)

    valuation = calculate_valuation(
        portfolio.holdings,
        portfolio.cash_balance,
        basis.prices,
        reported_total_value=basis.reported_total_value,
    )

    return PortfolioValuationOut(
        portfolio_id=portfolio.id,
        currency=portfolio.currency,
        price_source=basis.source,
        last_synced_at=basis.last_synced_at,
        cash_balance=valuation.cash_balance,
        holdings_value=valuation.holdings_value,
        total_value=valuation.total_value,
        cash_allocation_percent=valuation.cash_allocation_percent,
        holdings=[
            ValuationHoldingOut.model_validate(item) for item in valuation.holdings
        ],
    )


def _price_basis(portfolio: Portfolio) -> portfolio_prices.PriceBasis:
    """Which prices describe this portfolio, or a 503 naming what is missing.

    503 rather than 200 with a partial total: a valuation that silently omits a holding
    would look complete while being wrong. Resolving here also means the arithmetic below
    cannot be handed an incomplete table -- `calculate_valuation` would raise for the same
    reason, and there is no second path to reach.
    """
    try:
        return portfolio_prices.resolve(portfolio)
    except MissingPriceError as exc:
        raise HTTPException(
            status_code=503,
            detail=_missing_price_detail(portfolio, exc),
        ) from exc


def _missing_price_detail(portfolio: Portfolio, exc: MissingPriceError) -> str:
    """What to say when a held symbol has no price. The fix differs by price source, so
    the message does too."""
    symbols = ", ".join(exc.symbols)
    if portfolio.broker is None:
        return (
            f"No demo price available for held symbol(s): {symbols}. "
            "Add them to DEMO_PRICES in app/valuation.py."
        )
    return (
        f"No stored market price for held symbol(s): {symbols}, which this portfolio's "
        f"{portfolio.broker} snapshot should have supplied. Re-run the sync "
        "(POST /portfolio/sync); no demo price is used as a substitute."
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

    The prices are the same ones `GET /portfolio/valuation` would use, through the same
    `app.portfolio_prices` call -- so a scenario over a synchronised portfolio moves the
    broker's prices, never a fictional one, and its "before" column is the valuation's.
    """
    portfolio = _load_portfolio(session)
    basis = _price_basis(portfolio)

    try:
        scenario = calculate_scenario(
            portfolio.holdings,
            portfolio.cash_balance,
            basis.prices,
            payload.symbol,
            payload.price_change_percent / _HUNDRED,
            reported_total_value=basis.reported_total_value,
        )
    except HoldingNotFoundError as exc:
        held = ", ".join(sorted(holding.symbol for holding in portfolio.holdings))
        raise HTTPException(
            status_code=404,
            detail=(
                f"{exc.symbol} is not held by {portfolio.name!r}. "
                f"Held: {held or 'nothing'}."
            ),
        ) from exc

    return ScenarioOut(
        portfolio_id=portfolio.id,
        currency=portfolio.currency,
        price_source=basis.source,
        last_synced_at=basis.last_synced_at,
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
