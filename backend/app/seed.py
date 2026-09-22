"""Insert the AlphaDesk portfolio row and its starting contents.

Run by hand inside the backend container:

    docker compose exec backend python -m app.seed

This is deliberately *not* run at API startup: seeding is a decision a person makes,
not something that should happen silently every time the app boots.

Safe to run repeatedly. Existing records are left exactly as they are, so a later
run never overwrites a cash balance or a holding value that has since changed.

**Once the portfolio is synchronised with a broker account, this command stops there.**
The placeholder holdings below are fictional and the columns they sit in are the same ones
the sync writes, so inserting one into a linked portfolio would put a made-up position
beside real ones -- and re-running this after a position was closed would resurrect it.
The row is left untouched instead, which is what "not overwritten" has to mean.
"""

from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Holding, Portfolio
from app.portfolio_identity import DEMO_PORTFOLIO_NAME, find

PORTFOLIO_CURRENCY = "USD"

# What the portfolio holds until the first successful sync. Real values replace both,
# which is why nothing downstream is allowed to treat them as market data.
PLACEHOLDER_CASH_BALANCE = Decimal("10000.00")

# These are fictional purchase prices, not current market prices, and nothing here is
# derived from live data.
PLACEHOLDER_HOLDINGS: list[tuple[str, Decimal, Decimal]] = [
    ("NVDA", Decimal("10"), Decimal("120.00")),
    ("AAPL", Decimal("5"), Decimal("180.00")),
    ("MSFT", Decimal("8"), Decimal("400.00")),
]


def seed() -> None:
    """Insert any missing records, leaving existing ones untouched."""
    with SessionLocal() as session:
        # One transaction: everything below is committed together, or none of it is.
        with session.begin():
            portfolio = find(session)

            if portfolio is None:
                portfolio = Portfolio(
                    name=DEMO_PORTFOLIO_NAME,
                    currency=PORTFOLIO_CURRENCY,
                    cash_balance=PLACEHOLDER_CASH_BALANCE,
                )
                session.add(portfolio)
                session.flush()  # assigns portfolio.id
                print(f"created portfolio {DEMO_PORTFOLIO_NAME!r}")
            elif portfolio.broker is not None:
                print(
                    f"portfolio {portfolio.name!r} is linked to {portfolio.broker} "
                    f"(account {portfolio.broker_account_id}), leaving it unchanged"
                )
                return
            else:
                print(
                    f"portfolio {portfolio.name!r} already exists, leaving it unchanged"
                )

            existing_symbols = set(
                session.scalars(
                    select(Holding.symbol).where(Holding.portfolio_id == portfolio.id)
                )
            )

            for symbol, quantity, average_buy_price in PLACEHOLDER_HOLDINGS:
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
