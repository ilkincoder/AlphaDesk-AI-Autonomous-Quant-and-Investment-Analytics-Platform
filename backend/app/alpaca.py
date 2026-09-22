"""Alpaca paper-account client: the account and its open positions, read-only.

Two GETs and nothing else. There is no order endpoint in this module, no POST, and no way
to reach the live trading host -- see `_resolve_base_url`. The sync endpoint is the only
caller, and it stores the result in PostgreSQL.

Deliberately free of database imports, exactly as `app.twelvedata` is: `fetch_snapshot`
takes credentials and returns dataclasses of `Decimal`s. It writes nothing, so it can be
tested without PostgreSQL and reused by anything that needs a broker read.

Three decisions worth knowing before reading the code:

* **Paper only, enforced rather than assumed.** The configured base URL is checked against
  `PAPER_BASE_URL` before any request is made. A live URL is an error, not a warning: the
  difference between the two hosts is the difference between a portfolio figure and a real
  order, and a typo in `.env` should not be able to cross it.
* **Credentials travel in headers, never in a URL.** A key in a query string ends up in
  httpx's own exception messages and in anything that records a request line. `redact`
  still exists, because a provider is free to echo a credential back in an error message.
* **No number ever touches a float.** Alpaca returns every money field as a JSON *string*
  ("29.51"), and each one is parsed with `Decimal(...)` from that text. A value arriving as
  a JSON number is refused rather than accepted, because by then it has already been
  through a binary float and its precision is gone.

What this module does *not* do is decide what a valid portfolio looks like. It refuses
what the schema cannot store -- a short position, a non-positive entry price, a symbol
twice in one response -- and leaves everything else to the caller.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

import httpx

BROKER = "alpaca_paper"

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ACCOUNT_PATH = "/v2/account"
POSITIONS_PATH = "/v2/positions"

# Same ceilings the Twelve Data client uses, for the same reason: one provider request has
# a bound, so an unreachable endpoint yields an error rather than a sync that never ends.
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 15.0

# Alpaca account ids are uuids. Not enforced as a strict pattern -- a broker is free to
# change its identifier format, and refusing a valid account over the shape of its id would
# be this client inventing a rule -- but an empty one is not an identity.
_MAX_SYMBOL_LENGTH = 20


class AlpacaError(Exception):
    """Base class for every failure this client reports.

    One subclass per cause, so the endpoint can map a missing key to a configuration
    message and a rejected key to an authentication one without inspecting strings.
    """


class MissingCredentialsError(AlpacaError):
    """No key id or secret is configured. Raised before any connection is opened."""


class LiveHostError(AlpacaError):
    """The configured base URL is not the paper trading host."""


class ProviderAuthError(AlpacaError):
    """Alpaca refused the credentials."""


class RateLimitError(AlpacaError):
    """The account's request limit is exhausted."""


class ProviderUnavailableError(AlpacaError):
    """A timeout, a refused connection, or a provider-side server error."""


class MalformedResponseError(AlpacaError):
    """The response could not be understood, or was about something else."""


class DataValidationError(AlpacaError):
    """The response parsed, but the values in it are not usable.

    Separate from `MalformedResponseError`: one means "this is not a response I can read",
    the other means "I read it and the numbers in it are wrong".
    """


class BrokerSnapshot:
    """One read of the account and its positions, as plain validated values.

    `started_at` is when the read *began*, not when it finished. That is the instant the
    figures describe, and it is the ordering key the sync uses to refuse a slow fetch that
    would otherwise overwrite a newer one.
    """

    __slots__ = (
        "started_at",
        "account_id",
        "account_number",
        "currency",
        "cash",
        "equity",
        "positions",
    )

    def __init__(
        self,
        *,
        started_at: datetime,
        account_id: str,
        account_number: str,
        currency: str,
        cash: Decimal,
        equity: Decimal,
        positions: tuple["BrokerPosition", ...],
    ) -> None:
        self.started_at = started_at
        self.account_id = account_id
        self.account_number = account_number
        self.currency = currency
        self.cash = cash
        self.equity = equity
        self.positions = positions

    @property
    def position_count(self) -> int:
        """Derived rather than stored: a count that could disagree with `positions` is
        worse than no count."""
        return len(self.positions)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        # Deliberately not the dataclass repr: this carries an account number, and a repr
        # is exactly the kind of thing that reaches a log line. No credentials are held
        # here at all, which is why none are redacted.
        return (
            f"BrokerSnapshot(account_id={self.account_id!r}, currency={self.currency!r}, "
            f"cash={self.cash}, equity={self.equity}, positions={self.position_count})"
        )


class BrokerPosition:
    """One open position, as the broker reported it."""

    __slots__ = (
        "symbol",
        "quantity",
        "average_entry_price",
        "current_price",
        "market_value",
    )

    def __init__(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        average_entry_price: Decimal,
        current_price: Decimal,
        market_value: Decimal,
    ) -> None:
        self.symbol = symbol
        self.quantity = quantity
        self.average_entry_price = average_entry_price
        self.current_price = current_price
        self.market_value = market_value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"BrokerPosition(symbol={self.symbol!r}, quantity={self.quantity}, "
            f"avg_entry={self.average_entry_price}, current={self.current_price}, "
            f"market_value={self.market_value})"
        )


def redact(text: str, *secrets: str | None) -> str:
    """Replace any of `secrets` appearing in `text` with a fixed marker.

    Applied to every provider-supplied message before it reaches an exception, because
    Alpaca is free to quote a credential back in an error and this client's whole point is
    that neither ever leaves the process in a log line or a traceback.
    """
    for secret in secrets:
        if secret and secret.strip():
            text = text.replace(secret, "***")
    return text


def fetch_snapshot(
    *,
    api_key_id: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> BrokerSnapshot:
    """Read the paper account and its open positions.

    The arguments default to the configured settings. `transport` and `now` exist so tests
    can supply canned HTTP responses and a fixed clock without patching anything;
    production callers leave both alone.

    An empty positions list is a valid answer meaning "no open positions" -- it is not an
    error and not a missing response.

    Raises an `AlpacaError` subclass. Nothing is retried, and nothing is written.
    """
    resolved_key_id = api_key_id if api_key_id is not None else _configured_key_id()
    resolved_secret = (
        secret_key if secret_key is not None else _configured_secret_key()
    )
    resolved_base = _resolve_base_url(base_url if base_url is not None else _configured_base_url())

    # Both checks happen before the first request, so a misconfigured environment fails
    # without opening a socket.
    _require_credentials(resolved_key_id, resolved_secret)

    # Read before the first request, so it is the moment the figures describe rather than
    # the moment the slower of two calls happened to return.
    started_at = now if now is not None else datetime.now(timezone.utc)

    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=CONNECT_TIMEOUT_SECONDS,
        pool=CONNECT_TIMEOUT_SECONDS,
    )
    headers = {
        "APCA-API-KEY-ID": resolved_key_id,
        "APCA-API-SECRET-KEY": resolved_secret,
        "Accept": "application/json",
    }

    # A client per call inside a `with`, so the connection pool is always closed and
    # nothing is left holding a socket.
    with httpx.Client(transport=transport, timeout=timeout, headers=headers) as client:
        account = _decode(
            _get(client, resolved_base + ACCOUNT_PATH),
            resolved_key_id,
            resolved_secret,
        )
        positions = _decode(
            _get(client, resolved_base + POSITIONS_PATH),
            resolved_key_id,
            resolved_secret,
        )

    if not isinstance(positions, list):
        raise MalformedResponseError(
            "Alpaca's positions response is a "
            f"{type(positions).__name__}, not a list of positions"
        )

    return BrokerSnapshot(
        started_at=started_at,
        account_id=_require_text(account, "id", "account"),
        account_number=_require_text(account, "account_number", "account"),
        currency=_require_text(account, "currency", "account"),
        cash=_money(account.get("cash"), "account", "cash"),
        equity=_money(account.get("equity"), "account", "equity"),
        positions=_parse_positions(positions),
    )


def _configured_key_id() -> str | None:
    """The key id from application settings, imported here rather than at module level.

    Importing `app.config` builds the settings singleton, which requires `DATABASE_URL`.
    Keeping that import inside the function means this module -- a data layer with no
    database in it -- can be imported and unit-tested without a database URL at all.
    """
    from app.config import settings

    return settings.alpaca_api_key_id


def _configured_secret_key() -> str | None:
    from app.config import settings

    return settings.alpaca_api_secret_key


def _configured_base_url() -> str:
    from app.config import settings

    return settings.alpaca_base_url


def _resolve_base_url(base_url: str) -> str:
    """Return the paper host, or refuse.

    Trailing slashes are tolerated because they are a formatting accident. Anything else
    is not: the live host is a different account with real money behind it, and a sync
    pointed at it would write live positions into this portfolio.
    """
    normalized = (base_url or "").strip().rstrip("/")
    if normalized != PAPER_BASE_URL:
        raise LiveHostError(
            f"Alpaca base URL {base_url!r} is not the paper trading host. This "
            f"integration is restricted to {PAPER_BASE_URL}; set ALPACA_API_BASE_URL to "
            "that, or leave it unset."
        )
    return normalized


def _require_credentials(key_id: str | None, secret: str | None) -> None:
    missing = [
        name
        for name, value in (
            ("ALPACA_API_KEY_ID", key_id),
            ("ALPACA_API_SECRET_KEY", secret),
        )
        if not value or not value.strip()
    ]
    if missing:
        raise MissingCredentialsError(
            f"No Alpaca paper credentials are configured ({', '.join(missing)}). Set "
            "them in .env, then run: docker compose up -d backend"
        )


def _get(client: httpx.Client, url: str) -> httpx.Response:
    """Make one GET, and turn any transport failure into a typed error."""
    try:
        return client.get(url)
    except httpx.TimeoutException:
        # `from None`: httpx's exceptions carry the request URL, and a chained traceback
        # would print it. Nothing here re-raises anything that could hold a credential.
        raise ProviderUnavailableError(
            f"Alpaca did not respond in time (connect limit "
            f"{CONNECT_TIMEOUT_SECONDS:g}s, read limit {READ_TIMEOUT_SECONDS:g}s). "
            "Nothing was retried."
        ) from None
    except httpx.HTTPError:
        raise ProviderUnavailableError(
            "Could not reach paper-api.alpaca.markets. Check network connectivity from "
            "the backend container. Nothing was retried."
        ) from None


def _decode(response: httpx.Response, key_id: str, secret: str) -> Any:
    """Turn a response into a success payload, or raise the matching error."""
    try:
        payload = response.json()
    except ValueError:
        # A non-JSON body: an HTML error page, a truncated response, a proxy in the way.
        _raise_provider_error(response.status_code, None, key_id, secret)

    if response.status_code >= 400:
        message = payload.get("message") if isinstance(payload, dict) else None
        _raise_provider_error(response.status_code, message, key_id, secret)

    return payload


def _raise_provider_error(
    status: int, message: Any, key_id: str, secret: str
) -> NoReturn:
    # Both credentials are scrubbed, not only the one the provider is likelier to echo.
    detail = (
        redact(str(message), key_id, secret).strip() if message is not None else ""
    )
    suffix = f" Alpaca said: {detail[:300]}" if detail else ""

    if status in (401, 403):
        raise ProviderAuthError(
            f"Alpaca refused the credentials (HTTP {status}). Check "
            f"ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env, and that they belong "
            f"to a paper account.{suffix}"
        )

    if status == 429:
        raise RateLimitError(
            "Alpaca's rate limit is exhausted (HTTP 429). Nothing was retried, so no "
            f"further requests were spent. Wait for the limit to reset.{suffix}"
        )

    if status >= 500:
        raise ProviderUnavailableError(
            f"Alpaca reported a problem on its own side (HTTP {status}). "
            f"Nothing was retried.{suffix}"
        )

    raise MalformedResponseError(
        f"Alpaca rejected the request (HTTP {status}) in a way this client does not "
        f"recognise.{suffix}"
    )


def _parse_positions(raw_positions: list[Any]) -> tuple[BrokerPosition, ...]:
    """Validate every position, refusing to choose between two that disagree.

    A symbol appearing twice is a contradiction this client cannot resolve: picking either
    row would put a quantity on screen that nothing supports, and summing them would invent
    a position the broker does not report.
    """
    by_symbol: dict[str, BrokerPosition] = {}
    for raw in raw_positions:
        position = _parse_position(raw)
        if position.symbol in by_symbol:
            raise DataValidationError(
                f"Alpaca returned two positions for {position.symbol}; neither can be "
                "trusted, so neither is used"
            )
        by_symbol[position.symbol] = position
    return tuple(by_symbol[symbol] for symbol in sorted(by_symbol))


def _parse_position(raw: Any) -> BrokerPosition:
    if not isinstance(raw, dict):
        raise MalformedResponseError(
            f"a position in Alpaca's response is a {type(raw).__name__}, not an object"
        )

    symbol = _require_text(raw, "symbol", "position").upper()
    if len(symbol) > _MAX_SYMBOL_LENGTH:
        raise DataValidationError(
            f"Alpaca reported the symbol {symbol!r}, which is longer than the "
            f"{_MAX_SYMBOL_LENGTH} characters this schema can store"
        )

    quantity = _money(raw.get("qty"), symbol, "qty")
    average_entry_price = _money(raw.get("avg_entry_price"), symbol, "avg_entry_price")
    current_price = _money(raw.get("current_price"), symbol, "current_price")
    market_value = _money(raw.get("market_value"), symbol, "market_value")

    # Long-only, and refused rather than dropped. `holdings.quantity > 0` cannot store a
    # short, and a sync that silently omitted one would report a portfolio that looks
    # complete while missing a position.
    if quantity <= 0:
        raise DataValidationError(
            f"{symbol}: Alpaca reports a quantity of {quantity}. This portfolio stores "
            "long positions only, so a short or closed position cannot be stored."
        )

    # `holdings.average_buy_price > 0` is the constraint this mirrors.
    if average_entry_price <= 0:
        raise DataValidationError(
            f"{symbol}: Alpaca reports an average entry price of {average_entry_price}, "
            "which the holdings table cannot store as a purchase price"
        )

    if current_price < 0:
        raise DataValidationError(
            f"{symbol}: Alpaca reports a current price of {current_price}"
        )

    if market_value < 0:
        raise DataValidationError(
            f"{symbol}: Alpaca reports a market value of {market_value}"
        )

    return BrokerPosition(
        symbol=symbol,
        quantity=quantity,
        average_entry_price=average_entry_price,
        current_price=current_price,
        market_value=market_value,
    )


def _require_text(payload: dict[str, Any], field: str, subject: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MalformedResponseError(
            f"Alpaca's {subject} response has no usable {field!r}, which this sync "
            "needs to identify what it is looking at"
        )
    return value.strip()


def _money(value: Any, subject: str, field: str) -> Decimal:
    """Parse one money field, from the string Alpaca sends.

    A JSON number is refused rather than converted. Alpaca sends these as strings
    precisely so a client can keep the exact value, and a number here means the value was
    already through a binary float before this function saw it -- which is the precision
    loss the NUMERIC columns exist to avoid.
    """
    if value is None or value == "":
        raise DataValidationError(f"{subject}: Alpaca's {field} is missing")

    if not isinstance(value, str):
        raise DataValidationError(
            f"{subject}: Alpaca's {field} arrived as {type(value).__name__}, not the "
            "string this client requires; a number here would have lost precision already"
        )

    try:
        amount = Decimal(value.strip())
    except InvalidOperation:
        raise DataValidationError(
            f"{subject}: Alpaca's {field} is not a number ({value!r})"
        ) from None

    if not amount.is_finite():
        raise DataValidationError(
            f"{subject}: Alpaca's {field} is not finite ({value!r})"
        )
    return amount


__all__ = [
    "BROKER",
    "PAPER_BASE_URL",
    "AlpacaError",
    "BrokerPosition",
    "BrokerSnapshot",
    "DataValidationError",
    "LiveHostError",
    "MalformedResponseError",
    "MissingCredentialsError",
    "ProviderAuthError",
    "ProviderUnavailableError",
    "RateLimitError",
    "fetch_snapshot",
    "redact",
]
