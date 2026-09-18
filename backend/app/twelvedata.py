"""Twelve Data daily-price client.

Fetches one US stock's daily OHLCV bars and returns them as validated plain values.

Deliberately free of database imports: `fetch_daily_bars` takes a ticker, an exchange
and a bar count, and returns dataclasses of `Decimal`s. It writes nothing and stores
nothing, so it can be tested without PostgreSQL and reused by the Module 1 tools that
come later. Persisting bars is Step 4's job, not this module's.

Three decisions are worth knowing before reading the code:

* **The API key travels in the `Authorization` header, never in the URL.** A key in a
  query string ends up in httpx's own exception messages, in logs, and in anything that
  records a request line. Sending it as a header removes that whole class of leak rather
  than trying to scrub it afterwards. `redact` still exists, because a provider is free
  to echo a key back in an error message.
* **Prices never touch a float.** Every price is built with `Decimal` from the string
  Twelve Data sent. `Decimal(219.2)` would take a binary float first and inherit its
  error; `Decimal("219.2")` is exactly 219.2.
* **Every digit the provider sent is kept, including ones the database cannot hold.**
  Twelve Data occasionally returns a price with more decimal places than expected --
  `225.0099945` and several like it appeared in a 120-session NVDA window. These are
  provider-returned precision artifacts; this module does not speculate about what
  produces them, and passing `dp` to ask the provider to round them away makes things
  worse rather than better (it re-derives values that were clean, turning `213.38000`
  into `213.380005`).

  So a price is never rounded, and a bar is never refused for being *too precise*. The
  mismatch that creates is real and unresolved: `daily_prices.open/high/low/close` are
  `numeric(18,6)`, so a price needing a seventh decimal place cannot be inserted as the
  schema stands. Step 4 must choose -- round on ingest under a documented policy, or
  widen the column in a migration. Nothing here papers over it, and nothing here
  pre-empts that decision.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

PROVIDER = "twelve_data"
INTERVAL = "1day"
ADJUST_MODE = "splits"

# What goes in daily_prices.adjustment_basis. That column is CHECK-constrained to
# ('raw', 'adjusted') and this step adds no migration, so the client reports the
# vocabulary the database can hold and keeps the provider's exact mode alongside it in
# `provider_adjust_mode`. Nothing is labelled more precisely than the provider warrants.
ADJUSTMENT_BASIS = "adjusted"

API_BASE_URL = "https://api.twelvedata.com"
TIME_SERIES_PATH = "/time_series"

DEFAULT_BARS = 30
MAX_BARS = 500

# Asked for on top of the requested count. One slot covers the current session, which the
# date policy drops; the second covers a provider that reports a date ahead of the
# exchange's own calendar. Losing a bar that was actually asked for would be worse.
_COMPLETION_ALLOWANCE = 2

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0

_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,19}$")


class TwelveDataError(Exception):
    """Base class for every failure this client reports.

    One subclass per cause, so a caller can catch `RateLimitError` and back off without
    also catching a bad symbol, or catch this and handle everything at once.
    """


class MissingApiKeyError(TwelveDataError):
    """No key is configured. Raised before any connection is opened."""


class InvalidRequestError(TwelveDataError):
    """The arguments are not something this client will send."""


class ProviderAuthError(TwelveDataError):
    """Twelve Data refused the key, or the plan does not cover the request."""


class SymbolNotFoundError(TwelveDataError):
    """Twelve Data does not recognise the ticker on that exchange."""


class RateLimitError(TwelveDataError):
    """The account's request or credit limit is exhausted."""


class ProviderUnavailableError(TwelveDataError):
    """A timeout, a refused connection, or a provider-side server error."""


class MalformedResponseError(TwelveDataError):
    """The response could not be understood, or was about something else."""


class DataValidationError(TwelveDataError):
    """The response parsed, but the values in it are not usable.

    Separate from `MalformedResponseError`: one means "this is not a response I can
    read", the other means "I read it and the numbers in it are wrong".
    """


@dataclass(frozen=True)
class DailyBar:
    """One exchange trading session.

    `trading_date` is the session's own calendar date in the exchange's timezone -- the
    day the market was open -- not a UTC instant. Converting it to a timestamp would
    invent an hour the provider never stated.
    """

    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    # A count of shares, so an int. daily_prices.volume is a bigint for the same reason.
    volume: int


@dataclass(frozen=True)
class DailyPriceSeries:
    """Validated daily bars for one instrument, oldest first."""

    symbol: str
    exchange: str
    currency: str
    exchange_timezone: str
    provider: str
    interval: str
    adjustment_basis: str
    provider_adjust_mode: str
    retrieved_at: datetime
    requested_bars: int
    bars: tuple[DailyBar, ...]

    @property
    def returned_bars(self) -> int:
        """How many bars actually came back, which may be fewer than were asked for.

        Derived rather than stored: a separate count could disagree with `bars`, and a
        count that contradicts the data it counts is worse than no count.
        """
        return len(self.bars)


def redact(text: str, *secrets: str | None) -> str:
    """Replace any of `secrets` appearing in `text` with a fixed marker.

    Applied to every provider-supplied message before it reaches an exception, because
    Twelve Data is free to quote the key back in an error and this client's whole point
    is that a key never leaves the process in a log line or a traceback.
    """
    for secret in secrets:
        if secret and secret.strip():
            text = text.replace(secret, "***")
    return text


def fetch_daily_bars(
    symbol: str,
    exchange: str,
    bars: int = DEFAULT_BARS,
    *,
    api_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> DailyPriceSeries:
    """Fetch `bars` completed daily bars for `symbol` on `exchange`.

    `api_key` defaults to the configured `TWELVE_DATA_API_KEY`. `transport` and `now`
    exist so tests can supply canned HTTP responses and a fixed clock without patching
    anything; production callers leave both alone.

    Returns bars sorted oldest first, with the current exchange-local session and any
    later date excluded. See `_drop_incomplete_sessions`.

    Raises a `TwelveDataError` subclass -- see the classes above.
    """
    normalized_symbol = _validate_symbol(symbol)
    normalized_exchange = _validate_exchange(exchange)
    _validate_bar_count(bars)

    resolved_key = api_key if api_key is not None else _configured_api_key()
    if not resolved_key or not resolved_key.strip():
        # Before the request, so a misconfigured environment fails without spending a
        # credit or opening a socket.
        raise MissingApiKeyError(
            "No Twelve Data API key is configured. Set TWELVE_DATA_API_KEY in .env, "
            "then run: docker compose up -d backend"
        )

    retrieved_at = now if now is not None else datetime.now(timezone.utc)

    payload = _request(
        resolved_key, normalized_symbol, normalized_exchange, bars, transport
    )

    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise MalformedResponseError(
            "Twelve Data's response has no 'meta' object, so it cannot be checked "
            "against what was requested"
        )
    _verify_meta(meta, normalized_symbol, normalized_exchange)

    raw_values = payload.get("values")
    if not isinstance(raw_values, list):
        raise MalformedResponseError(
            "Twelve Data's response has no 'values' list, so it contains no bars"
        )

    parsed = [_parse_bar(raw) for raw in raw_values]
    completed = _drop_incomplete_sessions(
        _collapse_duplicate_dates(parsed), meta["exchange_timezone"], retrieved_at
    )
    completed.sort(key=lambda item: item.trading_date)

    # A couple of spare bars were requested to cover the current session being dropped,
    # so more can come back than were asked for. Keeping the most recent `bars` makes
    # `returned_bars <= requested_bars` an invariant rather than a coincidence -- asking
    # for thirty and being handed thirty-one is a surprise, however it arose.
    completed = completed[-bars:]

    return DailyPriceSeries(
        symbol=normalized_symbol,
        exchange=meta["exchange"].strip().upper(),
        currency=meta["currency"].strip(),
        exchange_timezone=meta["exchange_timezone"].strip(),
        provider=PROVIDER,
        interval=INTERVAL,
        adjustment_basis=ADJUSTMENT_BASIS,
        provider_adjust_mode=ADJUST_MODE,
        retrieved_at=retrieved_at,
        requested_bars=bars,
        bars=tuple(completed),
    )


def _configured_api_key() -> str | None:
    """The key from application settings, imported here rather than at module level.

    Importing `app.config` builds the settings singleton, which requires `DATABASE_URL`.
    Keeping that import inside the function means this module -- a data layer with no
    database in it -- can be imported and unit-tested without a database URL at all.
    """
    from app.config import settings

    return settings.twelve_data_api_key


def _validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise InvalidRequestError("symbol must be a non-empty ticker, for example NVDA")

    normalized = symbol.strip().upper()
    if not _SYMBOL_PATTERN.match(normalized):
        raise InvalidRequestError(
            f"{symbol!r} does not look like a US ticker: letters and digits, optionally "
            "with '.' or '-', starting with a letter"
        )
    return normalized


def _validate_exchange(exchange: str) -> str:
    if not isinstance(exchange, str) or not exchange.strip():
        raise InvalidRequestError(
            "exchange must be a non-empty name, for example NASDAQ"
        )
    return exchange.strip().upper()


def _validate_bar_count(bars: int) -> None:
    # `bool` is an `int` subclass, and `--bars` reaching here as `True` would silently
    # mean one bar.
    if isinstance(bars, bool) or not isinstance(bars, int):
        raise InvalidRequestError("bars must be a whole number")
    if not 1 <= bars <= MAX_BARS:
        raise InvalidRequestError(f"bars must be between 1 and {MAX_BARS}, got {bars}")


def _request(
    api_key: str,
    symbol: str,
    exchange: str,
    bars: int,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    """Make the one HTTP call, and turn any transport failure into a typed error."""
    params = {
        "symbol": symbol,
        "exchange": exchange,
        "interval": INTERVAL,
        "adjust": ADJUST_MODE,
        # `desc` is what makes `outputsize` mean "the most recent N". The bars are sorted
        # back into oldest-first order afterwards, because `order=asc` with no date
        # bounds is ambiguous about which N you get.
        "order": "desc",
        "outputsize": bars + _COMPLETION_ALLOWANCE,
        "format": "JSON",
    }

    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=CONNECT_TIMEOUT_SECONDS,
        pool=CONNECT_TIMEOUT_SECONDS,
    )

    try:
        # A client per call inside a `with`, so the connection pool is always closed and
        # nothing is left holding a socket.
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.get(
                f"{API_BASE_URL}{TIME_SERIES_PATH}",
                params=params,
                headers={
                    "Authorization": f"apikey {api_key}",
                    "Accept": "application/json",
                },
            )
    except httpx.TimeoutException:
        # `from None`: httpx's exceptions carry the request URL, and a chained traceback
        # would print it. Nothing here re-raises anything that could hold a secret.
        raise ProviderUnavailableError(
            f"Twelve Data did not respond in time (connect limit "
            f"{CONNECT_TIMEOUT_SECONDS:g}s, read limit {READ_TIMEOUT_SECONDS:g}s). "
            "Nothing was retried."
        ) from None
    except httpx.HTTPError:
        raise ProviderUnavailableError(
            "Could not reach api.twelvedata.com. Check network connectivity from the "
            "backend container. Nothing was retried."
        ) from None

    return _decode(response, api_key)


def _decode(response: httpx.Response, api_key: str) -> dict[str, Any]:
    """Turn a response into a success payload, or raise the matching error.

    Twelve Data reports failures in the body, and can do so under an HTTP 200, so the
    payload is inspected before the status code is trusted.
    """
    try:
        payload = response.json()
    except ValueError:
        # A non-JSON body: an HTML error page, a truncated response, a proxy in the way.
        _raise_provider_error(response.status_code, None, api_key)

    if not isinstance(payload, dict):
        raise MalformedResponseError(
            f"Twelve Data returned HTTP {response.status_code} with a JSON body that is "
            f"a {type(payload).__name__}, not an object"
        )

    code = payload.get("code")
    if payload.get("status") == "error" or (code is not None and "values" not in payload):
        _raise_provider_error(
            code if isinstance(code, int) else response.status_code,
            payload.get("message"),
            api_key,
        )

    if response.status_code >= 400:
        _raise_provider_error(response.status_code, payload.get("message"), api_key)

    return payload


def _raise_provider_error(code: int, message: Any, api_key: str) -> NoReturn:
    detail = redact(str(message), api_key).strip() if message is not None else ""
    suffix = f" Twelve Data said: {detail[:300]}" if detail else ""

    # 400 with "not found" in the message is how an unknown ticker sometimes arrives
    # instead of a 404. Checked on the message because the status alone is ambiguous.
    if code == 404 or (code == 400 and "not found" in detail.lower()):
        raise SymbolNotFoundError(
            "Twelve Data does not recognise that ticker on the exchange given. Check the "
            f"symbol and that it lists there.{suffix}"
        )

    if code in (401, 403):
        raise ProviderAuthError(
            f"Twelve Data refused the API key (HTTP {code}). Check TWELVE_DATA_API_KEY "
            f"in .env, and that your plan covers this endpoint.{suffix}"
        )

    if code == 429:
        raise RateLimitError(
            "Twelve Data's rate limit or credit allowance is exhausted (HTTP 429). "
            "Nothing was retried, so no further credits were spent. Wait for the limit "
            f"to reset, or check your plan's quota.{suffix}"
        )

    if code >= 500:
        raise ProviderUnavailableError(
            f"Twelve Data reported a problem on its own side (HTTP {code}). "
            f"Nothing was retried.{suffix}"
        )

    raise MalformedResponseError(
        f"Twelve Data rejected the request (HTTP {code}) in a way this client does not "
        f"recognise.{suffix}"
    )


def _verify_meta(meta: dict[str, Any], symbol: str, exchange: str) -> None:
    """Check the response describes the instrument that was asked for."""
    returned_symbol = meta.get("symbol")
    if not isinstance(returned_symbol, str) or not returned_symbol.strip():
        raise MalformedResponseError("Twelve Data's response has no 'symbol' in its meta")
    if returned_symbol.strip().upper() != symbol:
        raise MalformedResponseError(
            f"asked Twelve Data for {symbol} but the response describes "
            f"{returned_symbol!r}; refusing to treat it as {symbol}"
        )

    returned_interval = meta.get("interval")
    if returned_interval != INTERVAL:
        raise MalformedResponseError(
            f"asked Twelve Data for {INTERVAL} bars but the response says "
            f"{returned_interval!r}"
        )

    returned_exchange = meta.get("exchange")
    if not isinstance(returned_exchange, str) or not returned_exchange.strip():
        raise MalformedResponseError(
            "Twelve Data's response has no 'exchange' in its meta"
        )
    if returned_exchange.strip().upper() != exchange:
        raise MalformedResponseError(
            f"asked for {symbol} on {exchange} but the response is for "
            f"{returned_exchange!r}"
        )

    for field in ("currency", "exchange_timezone"):
        value = meta.get(field)
        if not isinstance(value, str) or not value.strip():
            raise MalformedResponseError(
                f"Twelve Data's response has no {field!r} in its meta, which the "
                "exchange-local date policy needs"
            )


def _parse_bar(raw: Any) -> DailyBar:
    if not isinstance(raw, dict):
        raise MalformedResponseError(
            f"a bar in Twelve Data's response is a {type(raw).__name__}, not an object"
        )

    trading_date = _parse_session_date(raw.get("datetime"))

    bar = DailyBar(
        trading_date=trading_date,
        open=_parse_price(raw.get("open"), "open", trading_date),
        high=_parse_price(raw.get("high"), "high", trading_date),
        low=_parse_price(raw.get("low"), "low", trading_date),
        close=_parse_price(raw.get("close"), "close", trading_date),
        volume=_parse_volume(raw.get("volume"), trading_date),
    )
    _verify_ohlc(bar)
    return bar


def _verify_ohlc(bar: DailyBar) -> None:
    """Reject a bar whose four prices cannot describe a single session.

    These are the same invariants `daily_prices` enforces with CHECK constraints,
    applied here as well so a bad bar is refused at the edge. Catching it now means the
    failure arrives naming the symbol and the date, rather than in Step 4 as a
    constraint violation inside a half-written transaction.
    """
    if bar.high < bar.low:
        raise DataValidationError(
            f"{bar.trading_date}: high {bar.high} is below low {bar.low}, which no "
            "session can produce"
        )
    if not bar.low <= bar.open <= bar.high:
        raise DataValidationError(
            f"{bar.trading_date}: open {bar.open} is outside the low-high range "
            f"{bar.low}-{bar.high}"
        )
    if not bar.low <= bar.close <= bar.high:
        raise DataValidationError(
            f"{bar.trading_date}: close {bar.close} is outside the low-high range "
            f"{bar.low}-{bar.high}"
        )


def _parse_session_date(value: Any) -> date:
    if not isinstance(value, str) or not value.strip():
        raise MalformedResponseError("a bar in Twelve Data's response has no 'datetime'")

    text = value.strip()
    # Daily bars are dated "YYYY-MM-DD". A time component is tolerated and dropped rather
    # than parsed into an instant: the session date is the fact, the time is not.
    date_part = text.split(" ", 1)[0]
    try:
        return date.fromisoformat(date_part)
    except ValueError:
        raise MalformedResponseError(
            f"bar datetime {text!r} is not a calendar date"
        ) from None


def _parse_price(value: Any, field: str, trading_date: date) -> Decimal:
    if value is None or value == "":
        raise DataValidationError(f"{trading_date}: {field} is missing")

    if not isinstance(value, str):
        # Twelve Data sends prices as strings. A JSON number would already have been
        # through a binary float, which is the precision loss this module exists to avoid.
        raise DataValidationError(
            f"{trading_date}: {field} arrived as {type(value).__name__}, not the string "
            "this client requires; a number here would have lost precision already"
        )

    try:
        price = Decimal(value.strip())
    except InvalidOperation:
        raise DataValidationError(
            f"{trading_date}: {field} is not a number ({value!r})"
        ) from None

    if not price.is_finite():
        raise DataValidationError(f"{trading_date}: {field} is not finite ({value!r})")
    if price <= 0:
        raise DataValidationError(
            f"{trading_date}: {field} must be positive, got {value!r}"
        )

    # No decimal-place limit is enforced. A price with more places than
    # numeric(18,6) can hold is returned exactly as the provider gave it -- refusing it
    # would discard a real bar, and rounding it here would hide a decision that belongs
    # to the storage step. See the module docstring.
    return price


def _parse_volume(value: Any, trading_date: date) -> int:
    if value is None or value == "":
        # Not defaulted to 0. daily_prices.volume is NOT NULL, and a session that
        # reported no volume is not the same fact as a session that traded nothing.
        raise DataValidationError(
            f"{trading_date}: volume is missing. It must not be stored as zero, and "
            "daily_prices.volume cannot be null, so this bar cannot be used."
        )

    if not isinstance(value, str):
        raise DataValidationError(
            f"{trading_date}: volume arrived as {type(value).__name__}, not a string"
        )

    try:
        volume = Decimal(value.strip())
    except InvalidOperation:
        raise DataValidationError(
            f"{trading_date}: volume is not a number ({value!r})"
        ) from None

    if not volume.is_finite():
        raise DataValidationError(
            f"{trading_date}: volume is not finite ({value!r})"
        )
    if volume < 0:
        raise DataValidationError(f"{trading_date}: volume is negative ({value!r})")
    if volume != volume.to_integral_value():
        raise DataValidationError(
            f"{trading_date}: volume {value!r} is not a whole number of shares"
        )
    return int(volume)


def _collapse_duplicate_dates(bars: Iterable[DailyBar]) -> list[DailyBar]:
    """Collapse repeated dates, refusing to choose between two that disagree.

    Two identical rows are the same fact stated twice, so keeping one loses nothing. Two
    rows for one session that differ are a contradiction this client cannot resolve, and
    picking either would put a number on screen that nothing supports.
    """
    by_date: dict[date, DailyBar] = {}
    for bar in bars:
        existing = by_date.get(bar.trading_date)
        if existing is None:
            by_date[bar.trading_date] = bar
        elif existing != bar:
            raise DataValidationError(
                f"Twelve Data returned two different bars for {bar.trading_date}: "
                f"{existing} and {bar}. Neither can be trusted, so neither is returned."
            )
    return list(by_date.values())


def _drop_incomplete_sessions(
    bars: Iterable[DailyBar], exchange_timezone: str, retrieved_at: datetime
) -> list[DailyBar]:
    """Keep only sessions that have finished, in the exchange's own calendar.

    A bar dated today is a session still in progress -- on a live run NVDA's current-day
    row carried about 1% of the previous day's volume -- so letting it through would feed
    a partial session into later calculations as though it were a whole one. Future dates
    are dropped for the same reason: they cannot be historical facts yet.

    The cost of the rule, stated plainly: a session that has genuinely closed is *also*
    excluded until the exchange's calendar day rolls over. That is the conservative
    direction to be wrong in, and it is deliberate.
    """
    local_today = _exchange_local_date(exchange_timezone, retrieved_at)
    return [bar for bar in bars if bar.trading_date < local_today]


def _exchange_local_date(exchange_timezone: str, instant: datetime) -> date:
    try:
        zone = ZoneInfo(exchange_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise MalformedResponseError(
            f"Twelve Data reported an exchange timezone of {exchange_timezone!r}, which "
            "is not a zone this system knows, so the current session cannot be identified"
        ) from None
    return instant.astimezone(zone).date()