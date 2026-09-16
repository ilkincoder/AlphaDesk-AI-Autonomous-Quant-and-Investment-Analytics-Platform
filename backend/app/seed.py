"""Insert the AlphaDesk demo portfolio.

Run by hand inside the backend container:

    docker compose exec backend python -m app.seed

This is deliberately *not* run at API startup: seeding is a decision a person makes,
not something that should happen silently every time the app boots.

Safe to run repeatedly. Existing records are left exactly as they are, so a later
run never overwrites a cash balance or a holding value that has since changed.
"""

from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Holding, Portfolio

DEMO_PORTFOLIO_NAME = "AlphaDesk Demo"
DEMO_CURRENCY = "USD"
DEMO_CASH_BALANCE = Decimal("10000.00")

# These are fictional purchase prices recorded on the demo portfolio, not current
# market prices, and nothing here is derived from live data.
DEMO_HOLDINGS: list[tuple[str, Decimal, Decimal]] = [
    ("NVDA", Decimal("10"), Decimal("120.00")),
    ("AAPL", Decimal("5"), Decimal("180.00")),
    ("MSFT", Decimal("8"), Decimal("400.00")),
]


def seed() -> None:
    """Insert any missing demo records, leaving existing ones untouched."""
    with SessionLocal() as session:
        # One transaction: everything below is committed together, or none of it is.
        with session.begin():
            portfolio = session.scalar(
                select(Portfolio).where(Portfolio.name == DEMO_PORTFOLIO_NAME)
            )

            if portfolio is None:
                portfolio = Portfolio(
                    name=DEMO_PORTFOLIO_NAME,
                    currency=DEMO_CURRENCY,
                    cash_balance=DEMO_CASH_BALANCE,
                )
                session.add(portfolio)
                session.flush()  # assigns portfolio.id
                print(f"created portfolio {DEMO_PORTFOLIO_NAME!r}")
            else:
                print(f"portfolio {DEMO_PORTFOLIO_NAME!r} already exists, leaving it unchanged")

            existing_symbols = set(
                session.scalars(
                    select(Holding.symbol).where(Holding.portfolio_id == portfolio.id)
                )
            )

            for symbol, quantity, average_buy_price in DEMO_HOLDINGS:
                if symbol in existing_symbols:
                    print(f"  {symbol}: already present, leaving it unchanged")
                    continue
                session.add(
                    Holding(
                        portfolio_id=portfolio.id,
                        symbol=symbol,
                        quantity=quantity,
                        average_buy_price=average_buy_price,
                    )
                )
                print(f"  {symbol}: added")


if __name__ == "__main__":
    seed()