"""Alpaca paper-account client: the account, its positions, the clock, and order submission.

Three reads and one write. The reads are the account and its positions (`fetch_snapshot`), the
market clock (`fetch_clock`) and the account's orders (`fetch_order_by_client_id`,
`fetch_open_orders`). The one write is `submit_order`, and there is no way to reach the live
trading host -- see `_resolve_base_url`.

Deliberately free of database imports, exactly as `app.twelvedata` is: every function here takes
credentials and returns dataclasses of `Decimal`s. Nothing writes to PostgreSQL, so the whole
client can be tested without a database and reused by anything that needs a broker.

Five decisions worth knowing before reading the code:

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
* **A submission that gets no answer is not a failure.** `submit_order` raises
  `SubmissionUncertainError` when a POST times out or the provider reports a fault on its own
  side, because the order may exist anyway. It is never retried here: retrying a POST that may
  already have been accepted is how one decision becomes two orders. The caller reconciles by
  `client_order_id`, which is why every function that submits takes one rather than generating
  it.
* **Nothing here decides what a valid order is.** Quantities, sides and instruments are the
  caller's, and this module refuses only what the broker's own response contradicts.

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
CLOCK_PATH = "/v2/clock"
ORDERS_PATH = "/v2/orders"
ORDER_BY_CLIENT_ID_PATH = "/v2/orders:by_client_order_id"

# Alpaca's own ceiling on a client-chosen order id. This application derives ids well inside it
# (`app.orders.client_order_id`), and the limit is asserted here rather than assumed.
MAX_CLIENT_ORDER_ID_LENGTH = 128

# How many orders one reconciliation read asks for. Alpaca caps this at 500; the account this is
# written for holds a handful, and a bounded request is the point.
OPEN_ORDERS_PAGE_SIZE = 100

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


class SubmissionUncertainError(AlpacaError):
    """A POST was sent and no usable answer came back.

    The single most important distinction in this client. A timeout, a dropped connection or a
    fault on Alpaca's own side means this application does **not** know whether the order exists
    -- the request may have been accepted and the reply lost. It is emphatically not a failure:
    treating it as one and re-sending is how one decision becomes two orders.

    Nothing in this module retries a POST for any reason. The caller reconciles by
    `client_order_id`, which is why `submit_order` requires one.
    """


class BrokerSnapshot:
    """One read of the account and its positions, as plain validated values.

    `started_at` is when the read *began*, not when it finished. That is the instant the
    figures describe, and it is the ordering key the sync uses to refuse a slow fetch that
    would otherwise overwrite a newer one.

    `trading_blocked`, `account_blocked` and `trade_suspended_by_user` are the account's own
    statements about whether it may trade at all. They are carried here rather than checked here:
    this client reports what the account says, and whether a restriction forbids a particular
    order is the execution layer's question. Alpaca defines all three, and a response that omits
    one is refused rather than read as "not blocked" -- an absent flag is not the account saying
    it is fine.
    """

    __slots__ = (
        "started_at",
        "account_id",
        "account_number",
        "currency",
        "cash",
        "equity",
        "status",
        "trading_blocked",
        "account_blocked",
        "trade_suspended_by_user",
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
        status: str = "ACTIVE",
        trading_blocked: bool = False,
        account_blocked: bool = False,
        trade_suspended_by_user: bool = False,
    ) -> None:
        self.started_at = started_at
        self.account_id = account_id
        self.account_number = account_number
        self.currency = currency
        self.cash = cash
        self.equity = equity
        self.positions = positions
        # Defaulted only for a snapshot a test builds by hand. `fetch_snapshot` always fills
        # these from the response, and refuses a response that does not carry them.
        self.status = status
        self.trading_blocked = trading_blocked
        self.account_blocked = account_blocked
        self.trade_suspended_by_user = trade_suspended_by_user

    @property
    def position_count(self) -> int:
        """Derived rather than stored: a count that could disagree with `positions` is
        worse than no count."""
        return len(self.positions)

    @property
    def restriction(self) -> str | None:
        """Why this account may not place an order, or None if nothing says it may not.

        One sentence rather than a set of flags, because every caller wants the same thing: a
        reason to show. The flags stay readable beside it for anyone who needs the specifics.
        """
        if self.account_blocked:
            return (
                "Alpaca reports this account as blocked, which prohibits account activity "
                "including trading."
            )
        if self.trading_blocked:
            return "Alpaca reports trading as blocked for this account."
        if self.trade_suspended_by_user:
            return "Trading is suspended on this account by its own settings in Alpaca."
        if self.status != "ACTIVE":
            return (
                f"Alpaca reports this account's status as {self.status}, not ACTIVE, so it is "
                "not in a state to place orders."
            )
        return None

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


class ClockReading:
    """The market clock, as plain validated values.

    `is_open` is the answer to "may a DAY market order be sent now", and it is Alpaca's rather
    than this application's: sessions, holidays and early closes are the exchange calendar's
    business, and a locally computed 09:30-16:00 would be wrong on every one of them.

    `next_close` is carried because "open" is not sufficient on its own. An order sent with
    seconds left in the session is a DAY order that will expire unfilled, so the caller checks
    how much of the session is left -- see `app.order_execution`.
    """

    __slots__ = ("timestamp", "is_open", "next_open", "next_close")

    def __init__(
        self,
        *,
        timestamp: datetime,
        is_open: bool,
        next_open: datetime,
        next_close: datetime,
    ) -> None:
        self.timestamp = timestamp
        self.is_open = is_open
        self.next_open = next_open
        self.next_close = next_close

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ClockReading(is_open={self.is_open}, timestamp={self.timestamp.isoformat()}, "
            f"next_close={self.next_close.isoformat()})"
        )


class BrokerOrder:
    """One order, as the broker reports it.

    **Every field is the broker's**, and nothing here is inferred. `filled_quantity` and
    `filled_avg_price` are what actually happened, which is a different fact from what was
    requested -- the whole reason an execution record keeps both.

    `status` is kept verbatim as well as mapped. Alpaca documents a long list of statuses, and a
    member of it this build does not recognise must still be readable rather than rounded to
    something it might not be.
    """

    __slots__ = (
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "quantity",
        "filled_quantity",
        "filled_avg_price",
        "status",
        "order_type",
        "time_in_force",
        "limit_price",
        "submitted_at",
    )

    def __init__(
        self,
        *,
        order_id: str,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: Decimal,
        filled_quantity: Decimal,
        filled_avg_price: Decimal | None,
        status: str,
        order_type: str,
        time_in_force: str,
        limit_price: Decimal | None,
        submitted_at: datetime | None,
    ) -> None:
        self.order_id = order_id
        self.client_order_id = client_order_id
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.filled_quantity = filled_quantity
        self.filled_avg_price = filled_avg_price
        self.status = status
        self.order_type = order_type
        self.time_in_force = time_in_force
        self.limit_price = limit_price
        self.submitted_at = submitted_at

    @property
    def remaining_quantity(self) -> Decimal:
        """How much of this order has not filled. Never negative: a broker reporting a fill
        larger than the order is a contradiction, and a negative remainder would propagate as
        one."""
        return max(self.quantity - self.filled_quantity, Decimal("0"))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"BrokerOrder(client_order_id={self.client_order_id!r}, symbol={self.symbol!r}, "
            f"side={self.side!r}, quantity={self.quantity}, filled={self.filled_quantity}, "
            f"status={self.status!r})"
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
    credentials = _credentials(api_key_id, secret_key, base_url)

    # Read before the first request, so it is the moment the figures describe rather than
    # the moment the slower of two calls happened to return.
    started_at = now if now is not None else datetime.now(timezone.utc)

    # A client per call inside a `with`, so the connection pool is always closed and
    # nothing is left holding a socket.
    with _client(transport, credentials) as client:
        account = _decode(
            _get(client, credentials.base_url + ACCOUNT_PATH),
            credentials.key_id,
            credentials.secret,
        )
        positions = _decode(
            _get(client, credentials.base_url + POSITIONS_PATH),
            credentials.key_id,
            credentials.secret,
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
        status=_require_text(account, "status", "account"),
        trading_blocked=_flag(account, "trading_blocked"),
        account_blocked=_flag(account, "account_blocked"),
        trade_suspended_by_user=_flag(account, "trade_suspended_by_user"),
        positions=_parse_positions(positions),
    )


def fetch_clock(
    *,
    api_key_id: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ClockReading:
    """Read the market clock: whether the session is open, and when it next closes.

    Alpaca's answer rather than a locally computed session, because holidays and early closes
    are the exchange calendar's business. Raises an `AlpacaError` subclass; nothing is written.
    """
    credentials = _credentials(api_key_id, secret_key, base_url)

    with _client(transport, credentials) as client:
        payload = _decode(
            _get(client, credentials.base_url + CLOCK_PATH),
            credentials.key_id,
            credentials.secret,
        )

    if not isinstance(payload, dict):
        raise MalformedResponseError(
            f"Alpaca's clock response is a {type(payload).__name__}, not an object"
        )

    is_open = payload.get("is_open")
    if not isinstance(is_open, bool):
        raise MalformedResponseError(
            "Alpaca's clock response has no boolean 'is_open', so whether the market is open "
            "cannot be read from it"
        )

    return ClockReading(
        timestamp=_timestamp(payload, "timestamp", "clock"),
        is_open=is_open,
        next_open=_timestamp(payload, "next_open", "clock"),
        next_close=_timestamp(payload, "next_close", "clock"),
    )


def submit_order(
    *,
    client_order_id: str,
    symbol: str,
    side: str,
    quantity: Decimal,
    order_type: str = "market",
    time_in_force: str = "day",
    api_key_id: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> BrokerOrder:
    """Send one order to the paper account, under an id the caller chose.

    **`client_order_id` is required, and this module never invents one.** It is the only thing
    that makes a submission whose answer was lost recoverable: the caller can reconstruct the
    same id and ask the broker what became of it. An id generated here would be lost with the
    response, and the order with it.

    **Nothing is retried.** `SubmissionUncertainError` means the outcome is unknown -- not that
    the order failed -- and re-sending is exactly the mistake this signature exists to prevent.

    The whole share quantity is a `Decimal` and is sent as a string, because Alpaca takes `qty`
    as a string and a float would have lost the value before it was serialised.
    """
    if not client_order_id or not client_order_id.strip():
        raise ValueError("a client_order_id is required to submit an order")

    if len(client_order_id) > MAX_CLIENT_ORDER_ID_LENGTH:
        raise ValueError(
            f"client_order_id is {len(client_order_id)} characters; Alpaca accepts at most "
            f"{MAX_CLIENT_ORDER_ID_LENGTH}"
        )

    credentials = _credentials(api_key_id, secret_key, base_url)

    body = {
        "symbol": symbol,
        "qty": _exact(quantity),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
    }

    with _client(transport, credentials) as client:
        payload = _submit(
            client,
            credentials.base_url + ORDERS_PATH,
            body,
            credentials.key_id,
            credentials.secret,
        )

    return _parse_order(payload)


def fetch_order_by_client_id(
    client_order_id: str,
    *,
    api_key_id: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> BrokerOrder | None:
    """The broker's own record of an order, found by the id this application assigned it.

    Returns **None when Alpaca has no such order**, which is a value rather than an error: it is
    the answer to "did my unanswered submission arrive", and the answer is sometimes no.

    A None is not proof that a timed-out submission failed, and this function does not treat it
    as one. Alpaca's own record can lag a submission by a moment, so the caller counts
    consecutive not-founds rather than concluding from the first -- see `app.order_execution`.
    """
    credentials = _credentials(api_key_id, secret_key, base_url)

    with _client(transport, credentials) as client:
        response = _get(
            client,
            credentials.base_url + ORDER_BY_CLIENT_ID_PATH,
            params={"client_order_id": client_order_id},
        )

        if response.status_code == 404:
            return None

        payload = _decode(response, credentials.key_id, credentials.secret)

    return _parse_order(payload)


def fetch_open_orders(
    *,
    api_key_id: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[BrokerOrder, ...]:
    """Every order the account still has working, including ones this application did not place.

    Read so that the cash and the shares an approved plan needs can be counted *net* of what is
    already committed. An order placed directly in Alpaca's own interface spends the same cash as
    one placed here, and a plan that ignored it would overdraw the account.

    An empty tuple is a valid answer meaning "nothing is working", not an error.
    """
    credentials = _credentials(api_key_id, secret_key, base_url)

    with _client(transport, credentials) as client:
        payload = _decode(
            _get(
                client,
                credentials.base_url + ORDERS_PATH,
                params={
                    "status": "open",
                    "limit": OPEN_ORDERS_PAGE_SIZE,
                    "direction": "asc",
                },
            ),
            credentials.key_id,
            credentials.secret,
        )

    if not isinstance(payload, list):
        raise MalformedResponseError(
            f"Alpaca's orders response is a {type(payload).__name__}, not a list of orders"
        )

    return tuple(_parse_order(item) for item in payload)



class _Credentials:
    """The three things every call needs, resolved and checked once.

    Resolving them in one place is what keeps `_require_credentials` and `_resolve_base_url`
    ahead of the first socket on *every* path rather than only on the path that had them first.
    """

    __slots__ = ("key_id", "secret", "base_url")

    def __init__(self, key_id: str, secret: str, base_url: str) -> None:
        self.key_id = key_id
        self.secret = secret
        self.base_url = base_url


def _credentials(
    api_key_id: str | None, secret_key: str | None, base_url: str | None = None
) -> _Credentials:
    """The credentials and base URL to use, validated before anything is opened."""
    resolved_key_id = api_key_id if api_key_id is not None else _configured_key_id()
    resolved_secret = secret_key if secret_key is not None else _configured_secret_key()

    # Checked here rather than inside the client, so a misconfigured environment fails
    # without opening a socket. The base URL is resolved first for the same reason: a live
    # host is refused before a credential is even looked at.
    resolved_base = _resolve_base_url(
        base_url if base_url is not None else _configured_base_url()
    )
    _require_credentials(resolved_key_id, resolved_secret)

    return _Credentials(key_id=resolved_key_id, secret=resolved_secret, base_url=resolved_base)


def _client(
    transport: httpx.BaseTransport | None, credentials: _Credentials
) -> httpx.Client:
    """One client per call, inside a `with`, so the pool is always closed and no socket leaks."""
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
            write=CONNECT_TIMEOUT_SECONDS,
            pool=CONNECT_TIMEOUT_SECONDS,
        ),
        headers={
            "APCA-API-KEY-ID": credentials.key_id,
            "APCA-API-SECRET-KEY": credentials.secret,
            "Accept": "application/json",
        },
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


def _get(
    client: httpx.Client, url: str, params: dict[str, Any] | None = None
) -> httpx.Response:
    """Make one GET, and turn any transport failure into a typed error."""
    try:
        return client.get(url, params=params)
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


def _submit(
    client: httpx.Client, url: str, body: dict[str, Any], key_id: str, secret: str
) -> Any:
    """Make one POST, and say precisely how much is known about what happened to it.

    The three-way split is the point of this function, and it is the difference between one
    order and two:

    * **An answer that says no** -- 400, 401, 403, 404, 422, 429 -- is a refusal. Alpaca either
      rejected the order or declined to process the request, and in every one of those cases no
      order was created. These raise the same typed errors a GET would.
    * **A fault on Alpaca's side, or no answer at all** is `SubmissionUncertainError`: the order
      may exist. It is *not* raised as a failure, because the caller must not treat it as one.
    * Anything else is a success, and its body is returned.

    Nothing is retried, here or anywhere above this.
    """
    try:
        response = client.post(url, json=body)
    except httpx.TimeoutException:
        raise SubmissionUncertainError(
            "Alpaca did not answer the order submission in time (connect limit "
            f"{CONNECT_TIMEOUT_SECONDS:g}s, read limit {READ_TIMEOUT_SECONDS:g}s). Whether the "
            "order reached the broker is not known, so nothing was retried and no second order "
            "was sent. It is reconciled by its client order id."
        ) from None
    except httpx.HTTPError:
        raise SubmissionUncertainError(
            "The order submission could not reach paper-api.alpaca.markets. Whether it arrived "
            "is not known, so nothing was retried and no second order was sent. It is "
            "reconciled by its client order id."
        ) from None

    if response.status_code >= 500:
        # Alpaca's own fault. The request may well have been processed before the fault, so
        # this is the uncertain case rather than a refusal -- and it is deliberately not run
        # through `_raise_provider_error`, which would call it `ProviderUnavailableError` and
        # read as "nothing happened".
        raise SubmissionUncertainError(
            f"Alpaca reported a problem on its own side (HTTP {response.status_code}) while "
            "accepting the order. Whether it was created is not known, so nothing was retried "
            "and no second order was sent. It is reconciled by its client order id."
        )

    return _decode(response, key_id, secret)


def _parse_order(raw: Any) -> BrokerOrder:
    """One order from the broker's JSON, with every number kept exact.

    `filled_avg_price` is the only nullable number here: an order that has not filled has no
    average price, and zero would be a claim about a price that does not exist.
    """
    if not isinstance(raw, dict):
        raise MalformedResponseError(
            f"an order in Alpaca's response is a {type(raw).__name__}, not an object"
        )

    symbol = _require_text(raw, "symbol", "order").upper()
    client_order_id = _require_text(raw, "client_order_id", "order")

    return BrokerOrder(
        order_id=_require_text(raw, "id", "order"),
        client_order_id=client_order_id,
        symbol=symbol,
        side=_require_text(raw, "side", "order"),
        quantity=_money(raw.get("qty"), symbol, "qty"),
        filled_quantity=_money(raw.get("filled_qty"), symbol, "filled_qty"),
        filled_avg_price=_optional_money(raw.get("filled_avg_price"), symbol, "filled_avg_price"),
        status=_require_text(raw, "status", "order"),
        order_type=_require_text(raw, "type", "order"),
        time_in_force=_require_text(raw, "time_in_force", "order"),
        limit_price=_optional_money(raw.get("limit_price"), symbol, "limit_price"),
        submitted_at=_optional_timestamp(raw, "submitted_at", symbol),
    )


def _exact(value: Decimal) -> str:
    """A decimal, as the string Alpaca takes for `qty`.

    Formatted with `f` rather than left to `str`, so a value that would print in scientific
    notation ("1E+2") is sent as digits. A whole share count is an integer by the time it gets
    here, and this is what guarantees it arrives as one.
    """
    return format(value, "f")


def _flag(payload: dict[str, Any], field: str) -> bool:
    """One of the account's boolean restriction flags.

    Alpaca always sends these, so an absent one is refused rather than read as "false": an
    account whose response omits `trading_blocked` has not said it may trade, and treating
    silence as permission is the wrong direction to be wrong in.
    """
    value = payload.get(field)
    if not isinstance(value, bool):
        raise MalformedResponseError(
            f"Alpaca's account response has no boolean {field!r}, so whether this account is "
            "permitted to trade cannot be read from it"
        )
    return value


def _timestamp(payload: dict[str, Any], field: str, subject: str) -> datetime:
    value = payload.get(field)
    parsed = _parse_datetime(value)
    if parsed is None:
        raise MalformedResponseError(
            f"Alpaca's {subject} response has no usable {field!r} timestamp"
        )
    return parsed


def _optional_timestamp(
    payload: dict[str, Any], field: str, subject: str
) -> datetime | None:
    """A timestamp that is genuinely allowed to be absent -- an order not yet submitted."""
    value = payload.get(field)
    if value is None:
        return None
    parsed = _parse_datetime(value)
    if parsed is None:
        raise DataValidationError(f"{subject}: Alpaca's {field} is not a timestamp ({value!r})")
    return parsed


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _optional_money(value: Any, subject: str, field: str) -> Decimal | None:
    """A money field that is genuinely absent when it does not apply -- `filled_avg_price` on
    an unfilled order, `limit_price` on a market order.

    Absent and zero are different answers, so an empty string or a null is None rather than
    `Decimal("0")`. A value that *is* present is parsed exactly as any other money field.
    """
    if value is None or value == "":
        return None
    return _money(value, subject, field)



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
    "BrokerOrder",
    "BrokerPosition",
    "BrokerSnapshot",
    "ClockReading",
    "DataValidationError",
    "LiveHostError",
    "MalformedResponseError",
    "MissingCredentialsError",
    "ProviderAuthError",
    "ProviderUnavailableError",
    "RateLimitError",
    "SubmissionUncertainError",
    "fetch_clock",
    "fetch_open_orders",
    "fetch_order_by_client_id",
    "fetch_snapshot",
    "redact",
    "submit_order",
]
