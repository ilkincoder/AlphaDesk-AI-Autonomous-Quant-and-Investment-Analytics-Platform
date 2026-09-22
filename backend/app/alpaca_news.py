"""Alpaca's news API: company news from Benzinga, read-only.

One GET, and no way to place a trade. **This is a different host from `app.alpaca`**, which
is why it is a different module: that client talks to the paper trading account and refuses
anything but `paper-api.alpaca.markets`; this one talks to the market data API and refuses
anything but `data.alpaca.markets`. Two hosts, two restrictions, two clients -- folding them
together would mean one module whose host rule is a parameter, which is exactly the shape
that lets a credential reach somewhere it should not.

It shares the paper account's credential pair, because Alpaca issues one key pair per account
and uses it for both. Nothing here writes it anywhere: it travels in a header, and `redact`
removes it from any provider message before that message becomes an exception.

**A successful request is not evidence of real-time access.** Alpaca documents that without
real-time entitlement the news window defaults to "at least 15 minutes ago" and ends fifteen
minutes before now, and a caller here cannot tell the two cases apart from a 200. So this
module reports the provider's own `created_at` and `updated_at` and never claims the data is
live; what to say about that is the caller's decision, and `app.news` says it once.

Deliberately free of database imports, like the other clients: dataclasses in, dataclasses
out, so it can be tested without PostgreSQL.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn

import httpx

PROVIDER = "alpaca_news"

# The market data host, and the only one this client will talk to.
DATA_BASE_URL = "https://data.alpaca.markets"
NEWS_PATH = "/v1beta1/news"

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 20.0

# Alpaca's own ceiling on one page. Asking for more is a 400, not a larger page.
MAX_LIMIT = 50
DEFAULT_LIMIT = 50

# How many pages one call will follow before stopping. A news feed is effectively unbounded
# going forward, and the point of this client is a *bounded* read; a caller that wants more
# asks again. Without a cap, a paginating loop would be an unbounded crawl of a provider.
MAX_PAGES = 4

_MAX_SYMBOLS = 100


class AlpacaNewsError(Exception):
    """Base class for every failure this client reports."""


class MissingCredentialsError(AlpacaNewsError):
    """No credential pair is configured. Raised before any connection is opened."""


class LiveHostError(AlpacaNewsError):
    """The configured base URL is not the Alpaca market data host."""


class InvalidRequestError(AlpacaNewsError):
    """The arguments are not something this client will send."""


class ProviderAuthError(AlpacaNewsError):
    """Alpaca refused the credentials, or the plan does not cover the news endpoint."""


class RateLimitError(AlpacaNewsError):
    """The account's request limit is exhausted."""


class ProviderUnavailableError(AlpacaNewsError):
    """A timeout, a refused connection, or a provider-side server error."""


class MalformedResponseError(AlpacaNewsError):
    """The response could not be understood, or was not the kind of thing it claimed."""


@dataclass(frozen=True)
class NewsItem:
    """One article as Alpaca reported it, before anything is decided about it.

    `published_at` is the provider's `created_at` -- when the story was published, not when
    we read it. `provider_updated_at` is its `updated_at`, which Alpaca supplies on every
    article today but which this client treats as optional, because a provider is free to
    stop sending it and a missing update time is not a reason to drop a story.
    """

    provider_article_id: str
    headline: str
    source: str
    url: str
    summary: str
    content: str
    symbols: tuple[str, ...]
    published_at: datetime
    provider_updated_at: datetime | None


def redact(text: str, *secrets: str | None) -> str:
    """Replace any of `secrets` appearing in `text` with a fixed marker."""
    for secret in secrets:
        if secret and secret.strip():
            text = text.replace(secret, "***")
    return text


def fetch_news(
    symbols: list[str],
    *,
    days: int = 7,
    limit: int = DEFAULT_LIMIT,
    include_content: bool = True,
    api_key_id: str | None = None,
    api_secret_key: str | None = None,
    base_url: str | None = None,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> list[NewsItem]:
    """Read up to `limit` recent articles mentioning any of `symbols`.

    The window runs from `days` before `now` to now, and is sent explicitly rather than left
    to the provider's default -- the default is the thing that changes with entitlement, and
    a caller that silently received a different window depending on the plan would not be
    able to say what it had asked for.

    Raises an `AlpacaNewsError` subclass. Nothing is retried, and nothing is written.
    """
    normalized = _validate_symbols(symbols)
    _validate_limit(limit)

    resolved_key_id = api_key_id if api_key_id is not None else _configured_key_id()
    resolved_secret = (
        api_secret_key if api_secret_key is not None else _configured_secret_key()
    )
    # Not a setting, unlike the trading client's: that one has a base URL because a person
    # could plausibly point it at the live host by mistake, and the restriction is what
    # stops them. Here there is nothing to point at -- this is one API on one host -- so
    # the constant is the whole story and `base_url` exists only so tests can override it.
    resolved_base = _resolve_base_url(DATA_BASE_URL if base_url is None else base_url)
    _require_credentials(resolved_key_id, resolved_secret)

    began = now if now is not None else datetime.now(timezone.utc)
    start = (began - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

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

    items: list[NewsItem] = []
    page_token: str | None = None

    with httpx.Client(transport=transport, timeout=timeout, headers=headers) as client:
        for _ in range(MAX_PAGES):
            remaining = limit - len(items)
            if remaining <= 0:
                break

            payload = _decode(
                _get(
                    client,
                    resolved_base + NEWS_PATH,
                    params={
                        "symbols": ",".join(normalized),
                        "start": start,
                        "limit": min(remaining, MAX_LIMIT),
                        "include_content": "true" if include_content else "false",
                        "sort": "desc",
                        **({"page_token": page_token} if page_token else {}),
                    },
                ),
                resolved_key_id,
                resolved_secret,
            )

            raw_news = payload.get("news")
            if not isinstance(raw_news, list):
                raise MalformedResponseError(
                    "Alpaca's news response has no 'news' list, so it contains no articles"
                )

            items.extend(_parse_item(raw) for raw in raw_news)

            token = payload.get("next_page_token")
            if not isinstance(token, str) or not token:
                break
            page_token = token

    return items[:limit]


def _validate_symbols(symbols: list[str]) -> list[str]:
    if not isinstance(symbols, list) or not symbols:
        raise InvalidRequestError(
            "at least one symbol is required; Alpaca's news endpoint is asked per symbol"
        )
    if len(symbols) > _MAX_SYMBOLS:
        raise InvalidRequestError(
            f"{len(symbols)} symbols were given where at most {_MAX_SYMBOLS} may be asked "
            "for in one request"
        )
    normalized = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol.strip():
            raise InvalidRequestError("every symbol must be a non-empty ticker")
        normalized.append(symbol.strip().upper())
    # Sorted and deduplicated so the request line -- and therefore the provider's answer --
    # does not depend on the order a query happened to return holdings in.
    return sorted(set(normalized))


def _validate_limit(limit: int) -> None:
    # `bool` is an `int` subclass, and a `True` reaching here would silently mean one.
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidRequestError("limit must be a whole number")
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidRequestError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")


def _configured_key_id() -> str | None:
    from app.config import settings

    return settings.alpaca_api_key_id


def _configured_secret_key() -> str | None:
    from app.config import settings

    return settings.alpaca_api_secret_key


def _resolve_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if normalized != DATA_BASE_URL:
        raise LiveHostError(
            f"base URL {base_url!r} is not Alpaca's market data host. The news client is "
            f"restricted to {DATA_BASE_URL}; the trading client is restricted to "
            "https://paper-api.alpaca.markets. Neither will talk to the other's host."
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
            f"No Alpaca credentials are configured ({', '.join(missing)}). The news client "
            "uses the same key pair as the paper account. Set them in .env, then run: "
            "docker compose up -d backend"
        )


def _get(client: httpx.Client, url: str, *, params: dict[str, Any]) -> httpx.Response:
    try:
        return client.get(url, params=params)
    except httpx.TimeoutException:
        raise ProviderUnavailableError(
            f"Alpaca's news API did not respond in time (connect limit "
            f"{CONNECT_TIMEOUT_SECONDS:g}s, read limit {READ_TIMEOUT_SECONDS:g}s). "
            "Nothing was retried."
        ) from None
    except httpx.HTTPError:
        raise ProviderUnavailableError(
            "Could not reach data.alpaca.markets. Check network connectivity from the "
            "backend container. Nothing was retried."
        ) from None


def _decode(response: httpx.Response, key_id: str, secret: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        _raise_provider_error(response.status_code, None, key_id, secret)

    if not isinstance(payload, dict):
        raise MalformedResponseError(
            f"Alpaca returned HTTP {response.status_code} with a JSON body that is a "
            f"{type(payload).__name__}, not an object"
        )

    if response.status_code >= 400:
        _raise_provider_error(
            response.status_code, payload.get("message"), key_id, secret
        )

    return payload


def _raise_provider_error(
    status: int, message: Any, key_id: str, secret: str
) -> NoReturn:
    detail = redact(str(message), key_id, secret).strip() if message is not None else ""
    suffix = f" Alpaca said: {detail[:300]}" if detail else ""

    if status in (401, 403):
        raise ProviderAuthError(
            f"Alpaca refused the credentials for the news API (HTTP {status}). The news "
            "endpoint uses the same key pair as the paper account, and not every plan "
            f"includes it.{suffix}"
        )
    if status == 429:
        raise RateLimitError(
            "Alpaca's news rate limit is exhausted (HTTP 429). Nothing was retried, so no "
            f"further requests were spent.{suffix}"
        )
    if status >= 500:
        raise ProviderUnavailableError(
            f"Alpaca reported a problem on its own side (HTTP {status}). "
            f"Nothing was retried.{suffix}"
        )
    raise MalformedResponseError(
        f"Alpaca rejected the news request (HTTP {status}) in a way this client does not "
        f"recognise.{suffix}"
    )


def _parse_item(raw: Any) -> NewsItem:
    if not isinstance(raw, dict):
        raise MalformedResponseError(
            f"an article in Alpaca's news response is a {type(raw).__name__}, not an object"
        )

    article_id = _identifier(raw.get("id"))

    published = _parse_instant(raw.get("created_at"), article_id, "created_at")
    # Optional, and genuinely so. Alpaca sends it on every article today, but "the provider
    # stopped sending a revision time" is not a reason to drop a story -- unlike the
    # publication time, which nothing here can substitute for.
    updated = _parse_optional_instant(raw.get("updated_at"), article_id, "updated_at")

    symbols = raw.get("symbols")
    if symbols is None:
        symbols = []
    if not isinstance(symbols, list):
        raise MalformedResponseError(
            f"article {article_id}: 'symbols' is a {type(symbols).__name__}, not a list"
        )

    return NewsItem(
        provider_article_id=article_id,
        headline=_text(raw.get("headline")),
        source=_text(raw.get("source")),
        url=_text(raw.get("url")),
        summary=_text(raw.get("summary")),
        content=_text(raw.get("content")),
        symbols=tuple(
            sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        ),
        published_at=published,
        provider_updated_at=updated,
    )


def _identifier(value: Any) -> str:
    """An article's id, as text, from either a number or a string.

    **Alpaca sends `id` as a JSON number** -- `61927717`, not `"61927717"` -- which is the
    opposite of its money fields, where a number is refused. The two are not inconsistent:
    a price is a *quantity*, and a JSON number has already been through a binary float by
    the time a client sees it, so the digits are no longer the provider's. An identifier is
    not a quantity. It is never arithmetic, it is compared and stored and nothing else, and
    an integer JSON represents exactly as text represents, so converting it loses nothing.

    A float is still refused: an id that has been through a float is an id whose last
    digits may already be wrong, and "which article is this" is not a question to guess at.
    """
    if isinstance(value, bool) or value is None:
        raise MalformedResponseError("an article in Alpaca's news response has no 'id'")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise MalformedResponseError(
        f"an article in Alpaca's news response has an 'id' of type "
        f"{type(value).__name__}, which is neither a whole number nor text"
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_optional_instant(
    value: Any, article_id: str, field: str
) -> datetime | None:
    """An instant that may be absent, but may not be unreadable.

    Absent is a fact about the provider's response and is kept as NULL. Present-but-garbled
    is a malformed response, refused by name -- reading it as "no update time" would turn a
    provider's bug into a claim about the article.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse_instant(value, article_id, field)


def _parse_instant(value: Any, article_id: str, field: str) -> datetime:
    """An ISO instant as an aware UTC datetime.

    A naive value is read as UTC rather than refused: Alpaca sends `...Z` today, and a
    provider that dropped the suffix would be stating the same moment. What is *not*
    tolerated is a missing or unparseable date, because a story with no publication time
    cannot be ordered or compared and must not be given ours.
    """
    if not isinstance(value, str) or not value.strip():
        raise MalformedResponseError(
            f"article {article_id} has no {field!r}, so it cannot be dated"
        )
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedResponseError(
            f"article {article_id}: {field} {text!r} is not a timestamp"
        ) from None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "DATA_BASE_URL",
    "MAX_LIMIT",
    "PROVIDER",
    "AlpacaNewsError",
    "InvalidRequestError",
    "LiveHostError",
    "MalformedResponseError",
    "MissingCredentialsError",
    "NewsItem",
    "ProviderAuthError",
    "ProviderUnavailableError",
    "RateLimitError",
    "fetch_news",
    "redact",
]
