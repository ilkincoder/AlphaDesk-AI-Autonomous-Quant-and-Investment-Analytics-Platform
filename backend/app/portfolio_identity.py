"""The one portfolio this application has, and the two names it can have.

**One row, two names, and the name is a statement about what the row is.** Before anything
is connected, the row holds the placeholder portfolio a fresh install starts from, and
calling it "Alpaca Paper" would label made-up figures as a real account's. The first
successful sync makes it that account's, and renames it then -- which is why the rename
lives in `app.broker_sync` and not in a migration. A migration runs against a database it
knows nothing about; a sync knows it has just read a real account.

Everything that needs to find the row goes through `find`, so no caller has to know which
of the two names it currently has.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Portfolio

# What the row is called until it is linked to anything. Created by `python -m app.seed`.
DEMO_PORTFOLIO_NAME = "AlphaDesk Demo"

# What it is called from its first successful sync onwards.
BROKER_PORTFOLIO_NAME = "Alpaca Paper"

PORTFOLIO_NAMES = (DEMO_PORTFOLIO_NAME, BROKER_PORTFOLIO_NAME)


def find(session: Session, *, for_update: bool = False) -> Portfolio | None:
    """The portfolio row, under whichever of its two names it currently has.

    A linked row wins if both names somehow exist at once -- that can only happen if a
    fresh demo row was seeded beside one this application is already syncing, and the
    linked row is the one with real figures in it. The alternative is `scalar` raising
    `MultipleResultsFound` and the endpoints returning 500 for a state a person could
    reach by running the seed twice across a first sync.

    `for_update` locks the row for the rest of the caller's transaction. A sync asks for
    it: two syncs racing here would each read the holdings, each decide what is missing,
    and each write the result.

    The holdings come back loaded, in the same call. Every caller but one reads them, and
    the alternative is a collection that loads lazily during serialisation -- after the
    session that owns it may already have closed. One extra SELECT on a table with a
    handful of rows is the cheaper side of that trade.
    """
    statement = (
        select(Portfolio)
        .where(Portfolio.name.in_(PORTFOLIO_NAMES))
        .order_by(Portfolio.broker.is_(None), Portfolio.id)
        .limit(1)
        .options(selectinload(Portfolio.holdings))
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalars(statement).first()


__all__ = [
    "BROKER_PORTFOLIO_NAME",
    "DEMO_PORTFOLIO_NAME",
    "PORTFOLIO_NAMES",
    "find",
]
