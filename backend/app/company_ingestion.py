"""Adding a company: its identity, and fetching its prices and Form 4 filings.

Nothing here is specific to any issuer. A caller names the company — ticker, exchange and
issuer CIK — and this module establishes that both sources agree it is that company before a
single row is written.

**Identity is checked against three things, and all three have to line up:**

1. **What the command asked for.** Normalized and shape-checked before anything is fetched, so
   a malformed CIK costs no request.
2. **What the SEC says.** The issuer's own submission metadata, which lists every ticker filed
   under that CIK and every exchange it lists on.
3. **What the price provider says.** The symbol and exchange it actually answered about.

**The schema holds one listing per issuer.** `companies` is unique on `sec_issuer_cik`, so an
issuer with several listed classes — Alphabet reports GOOGL, GOOG, GOOGM and GOOGN — cannot be
stored without losing one of them to the other. That is refused up front rather than discovered
as a constraint violation halfway through writing, and it is refused against the SEC feed, so a
first attempt fails before anything exists to overwrite.

Everything here runs before the write transaction opens. A refusal leaves the database exactly
as it was.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.ingestion import IngestionIdentityError, IngestionSummary, ingest
from app.models import Company
from app.sec_edgar import DiscoveryResult, FilingRecord, SecEdgarClient, normalize_cik
from app.twelvedata import DailyPriceSeries, fetch_daily_bars


@dataclass(frozen=True)
class CompanyIdentity:
    """Who a run is about, as the command stated it."""

    symbol: str
    exchange: str
    cik: str


@dataclass(frozen=True)
class CompanyDataset:
    """Everything one run fetched, before any of it is written."""

    identity: CompanyIdentity
    series: DailyPriceSeries
    discovery: DiscoveryResult
    records: tuple[FilingRecord, ...]


def parse_identity(symbol: str, exchange: str, cik: str) -> CompanyIdentity:
    """Normalize and shape-check what the command was given.

    The CIK is normalized to ten digits, which is what preserves a leading zero: a CIK of
    `320193` and one of `0000320193` are the same issuer, and storing the first would lose the
    zeros EDGAR writes.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise IngestionIdentityError("--symbol must not be empty")
    if not isinstance(exchange, str) or not exchange.strip():
        raise IngestionIdentityError(
            "--exchange must not be empty, for example NASDAQ"
        )
    return CompanyIdentity(
        symbol=symbol.strip().upper(),
        exchange=exchange.strip().upper(),
        # Raises the SEC client's own InvalidRequestError for anything that is not one to ten
        # digits, which says what it expected.
        cik=normalize_cik(cik),
    )


def check_stored_company(session: Session, identity: CompanyIdentity) -> None:
    """Refuse an identity that what is already stored cannot hold alongside itself."""
    stored_cik = session.scalar(
        select(Company).where(Company.sec_issuer_cik == identity.cik)
    )
    if stored_cik is not None and stored_cik.ticker.upper() != identity.symbol:
        raise IngestionIdentityError(
            f"CIK {identity.cik} is already stored as {stored_cik.ticker!r}, so "
            f"{identity.symbol!r} cannot be added. This schema holds one listing per issuer, "
            "so a second ticker for the same CIK would have to replace the first. Issuers "
            "with several listed share classes are not supported."
        )

    stored_ticker = session.scalar(
        select(Company).where(func.upper(Company.ticker) == identity.symbol)
    )
    if stored_ticker is not None and stored_ticker.sec_issuer_cik != identity.cik:
        raise IngestionIdentityError(
            f"{identity.symbol!r} is already stored under CIK "
            f"{stored_ticker.sec_issuer_cik}, not {identity.cik}. Refusing to re-point an "
            "existing ticker at a different issuer."
        )


def check_sources_agree(
    identity: CompanyIdentity,
    *,
    series: DailyPriceSeries,
    discovery: DiscoveryResult,
) -> None:
    """Refuse unless both providers describe the company the command named."""
    check_price_listing(identity, series)
    check_issuer_listing(identity, discovery)


def check_price_listing(identity: CompanyIdentity, series: DailyPriceSeries) -> None:
    """The price provider must have answered about the listing that was asked for."""
    if (
        series.symbol.upper() != identity.symbol
        or series.exchange.upper() != identity.exchange
    ):
        raise IngestionIdentityError(
            f"the price source returned {series.symbol} on {series.exchange}, but "
            f"{identity.symbol} on {identity.exchange} was requested. Nothing was written."
        )


def check_issuer_listing(
    identity: CompanyIdentity, discovery: DiscoveryResult
) -> None:
    """The SEC's own metadata must describe the issuer the command named.

    Separate from the price check, and run first, because an issuer this schema cannot hold
    should not cost a second provider request to find out.
    """
    tickers = {ticker.upper() for ticker in discovery.tickers}
    if identity.symbol not in tickers:
        raise IngestionIdentityError(
            f"CIK {identity.cik} does not list {identity.symbol} in SEC metadata. It reports "
            f"{discovery.issuer_name!r} with tickers {sorted(tickers) or ['none']}. "
            "Nothing was written."
        )

    # Checked against the feed rather than the stored row, so a first attempt at a multi-class
    # issuer is refused before anything exists to conflict with.
    if len(tickers) > 1:
        raise IngestionIdentityError(
            f"CIK {identity.cik} reports more than one listed ticker "
            f"({', '.join(sorted(tickers))}). This schema holds one listing per issuer, so "
            "these share classes cannot be stored without losing one of them. Nothing was "
            "written."
        )

    exchanges = {name.upper() for name in discovery.exchanges}
    # Compared case-insensitively: EDGAR writes "Nasdaq" where the command says "NASDAQ".
    if exchanges and identity.exchange not in exchanges:
        raise IngestionIdentityError(
            f"SEC metadata lists {identity.symbol} on {sorted(exchanges)}, not "
            f"{identity.exchange}. Nothing was written."
        )


def build_dataset(
    client: SecEdgarClient,
    *,
    identity: CompanyIdentity,
    bars: int,
    filings: int,
) -> CompanyDataset:
    """Fetch the prices and filings, and check the two agree on which company this is.

    The only part of this module that touches the network, and it takes its client as an
    argument so a test can substitute one without reaching for the real thing.
    """
    # The SEC first: an issuer this schema cannot represent is refused before the price
    # provider is asked anything.
    discovery = client.discover(identity.cik, filings)
    check_issuer_listing(identity, discovery)

    series = fetch_daily_bars(identity.symbol, identity.exchange, bars)
    check_price_listing(identity, series)

    records = client.fetch_filings(discovery)
    return CompanyDataset(
        identity=identity, series=series, discovery=discovery, records=records
    )


def run(
    identity: CompanyIdentity, *, bars: int, filings: int, dry_run: bool
) -> IngestionSummary:
    """Validate, fetch, then write -- in that order, and never any other.

    `bars` and `filings` keep the bounds the clients already enforce: 30 daily bars by
    default up to 500, and 3 Form 4 filings by default up to 10.
    """
    started_at = datetime.now(timezone.utc)

    # --- 1. what is already stored, before anything is fetched. -----------------------
    with SessionLocal() as session:
        check_stored_company(session, identity)

    # --- 2. everything over the network, before any write transaction. ----------------
    with SecEdgarClient() as client:
        dataset = build_dataset(client, identity=identity, bars=bars, filings=filings)

    # --- 3. one short transaction. ----------------------------------------------------
    parameters = {
        "ticker": identity.symbol,
        "exchange": identity.exchange,
        "cik": identity.cik,
        "bars": bars,
        "filings": filings,
        "dry_run": dry_run,
    }

    session = SessionLocal()
    try:
        if dry_run:
            # No transaction is opened and `ingest` issues no INSERT, so a dry run is
            # read-only in fact as well as in intent.
            return ingest(
                session,
                series=dataset.series,
                discovery=dataset.discovery,
                records=dataset.records,
                parameters=parameters,
                started_at=started_at,
                dry_run=True,
            )
        with session.begin():
            return ingest(
                session,
                series=dataset.series,
                discovery=dataset.discovery,
                records=dataset.records,
                parameters=parameters,
                started_at=started_at,
            )
    finally:
        session.close()


