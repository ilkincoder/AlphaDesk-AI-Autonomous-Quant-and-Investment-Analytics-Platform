"""The Federal Reserve's and the Bureau of Labor Statistics' own news feeds.

Three URLs, listed below as an allowlist rather than assembled from a pattern. Nothing here
discovers feeds, follows links to find more, or fetches anything from a host that is not
`federalreserve.gov` or `bls.gov`. That is the whole access policy, and it is enforced in
`_require_official_url` rather than assumed: a fetch that could be pointed anywhere is a
crawler, and this is not one.

**Two syndication formats, one shape out.** The Fed publishes RSS 2.0 and the BLS publishes
Atom. Both are parsed with the standard library's `xml.etree.ElementTree` -- the parser
`app/form4.py` already uses -- and normalized to the same `FeedEntry`, so nothing downstream
has to know which is which.

**The releases themselves are retrieved, sparingly.** A feed entry's summary is often one
sentence, which is not enough to index and not much to read. When a summary is shorter than
`MIN_SUMMARY_CHARS`, the linked release is fetched from the agency's own site and its text
extracted. Bounded by `max_releases` per call, and only ever from the two official domains:
these are public documents on public sites, and the polite thing is to read the page the
agency published rather than to guess at its contents.

Every failure raises a typed error, so one unreachable agency is the caller's to isolate
rather than something that stops a run.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from app.htmltext import to_text

# The three feeds, and everything this module is allowed to fetch. Each is the agency's own
# documented feed URL, verified against the site before it was written here.
FED_MONETARY_POLICY = "https://www.federalreserve.gov/feeds/press_monetary.xml"
BLS_CPI = "https://www.bls.gov/feed/cpi.rss"
BLS_EMPLOYMENT_SITUATION = "https://www.bls.gov/feed/empsit.rss"

# Domains a release page may be fetched from. The feed's own links point here; this is what
# makes that a fact rather than a hope.
ALLOWED_RELEASE_HOSTS = frozenset(
    {"federalreserve.gov", "www.federalreserve.gov", "bls.gov", "www.bls.gov"}
)

# Identifies the application, as the SEC client's User-Agent does. Neither agency requires
# one, but a request that says who is making it is the polite default for a public service.
USER_AGENT = "AlphaDesk AI (news ingestion; contact: you@example.com)"

CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0

# A release page larger than this is abandoned mid-download. Public releases are a few
# hundred kilobytes at most; anything larger is not the document this client is after.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# Below this, a feed's own summary is not worth indexing on its own, so the linked release
# is fetched instead. The Fed's `description` is its headline repeated verbatim, which is
# the clearest case: 40 characters of nothing new.
MIN_SUMMARY_CHARS = 400

_ATOM = "{http://www.w3.org/2005/Atom}"

# An Atom link element is written either as a self-closing tag with an href, or with the URL
# as text -- both appear in the wild, and the BLS uses the latter.
_WHITESPACE = re.compile(r"\s+")


class OfficialFeedError(Exception):
    """Base class for every failure this module reports."""


class UnknownFeedError(OfficialFeedError):
    """The feed asked for is not one of the three this module knows."""


class UnsafeUrlError(OfficialFeedError):
    """A URL was about to be fetched that is not one of the official feeds or releases."""


class ProviderUnavailableError(OfficialFeedError):
    """A timeout, a refused connection, or a server-side failure."""


class MalformedResponseError(OfficialFeedError):
    """The response could not be parsed as a feed."""


class ResponseTooLargeError(OfficialFeedError):
    """A response exceeded the byte cap and was abandoned mid-download."""


@dataclass(frozen=True)
class Feed:
    """One official feed, and what a reader should call it."""

    key: str
    url: str
    title: str
    # The name stored on every article from this feed, and the label a reader sees.
    source: str


FEEDS: tuple[Feed, ...] = (
    Feed(
        key="fed_monetary",
        url=FED_MONETARY_POLICY,
        title="Federal Reserve — Monetary Policy",
        source="Federal Reserve",
    ),
    Feed(key="bls_cpi", url=BLS_CPI, title="BLS — Consumer Price Index", source="BLS"),
    Feed(
        key="bls_employment_situation",
        url=BLS_EMPLOYMENT_SITUATION,
        title="BLS — Employment Situation",
        source="BLS",
    ),
)

FEEDS_BY_KEY = {feed.key: feed for feed in FEEDS}


@dataclass(frozen=True)
class FeedEntry:
    """One feed entry, normalized across RSS and Atom.

    `entry_id` is the feed's own identifier -- the Fed's `guid`, the BLS's `id`. It is
    carried rather than derived from the URL because it is what the publisher says its
    identity is, and because a feed that reuses a URL for a corrected release should update
    that entry rather than create a second one.

    `summary` is what the feed itself supplied and may be nearly empty; `text` is the
    release page's text when one was fetched. Either can be the article's body -- the
    caller decides which is worth keeping, and neither is invented here.
    """

    entry_id: str
    title: str
    url: str
    summary: str
    published_at: datetime
    provider_updated_at: datetime | None
    text: str | None = None


def fetch_feed(
    feed: Feed,
    *,
    limit: int = 10,
    transport: httpx.BaseTransport | None = None,
) -> list[FeedEntry]:
    """Read up to `limit` entries from one official feed, newest first as published.

    Raises `UnknownFeedError` for a feed that is not in `FEEDS`, and the transport errors
    above for anything that goes wrong on the way.
    """
    if feed.key not in FEEDS_BY_KEY:
        raise UnknownFeedError(
            f"{feed.key!r} is not one of the feeds this module reads: "
            f"{', '.join(sorted(FEEDS_BY_KEY))}"
        )
    if limit < 1:
        return []

    _require_official_url(feed.url)
    body = _get(feed.url, transport)
    entries = _parse(body, feed)
    entries.sort(key=lambda entry: entry.published_at, reverse=True)
    return entries[:limit]


def fetch_release_text(
    url: str, *, transport: httpx.BaseTransport | None = None
) -> str:
    """The readable text of one official release page.

    Refuses any host but the two agencies'. Raises `UnsafeUrlError` rather than returning
    nothing, so a caller never mistakes "I would not fetch that" for "there was no text".
    """
    _require_official_url(url)
    return to_text(_get(url, transport)).strip()


def enrich_with_release_text(
    entries: list[FeedEntry],
    *,
    max_releases: int = 0,
    transport: httpx.BaseTransport | None = None,
) -> list[FeedEntry]:
    """Fetch the linked release for entries whose own summary is too thin to index.

    `max_releases` bounds how many pages one call will read. Exhausting it is not an error:
    the entries left alone keep the summary the feed gave them, which is a smaller article
    and still a true one.
    """
    if max_releases <= 0:
        return entries

    enriched: list[FeedEntry] = []
    fetched = 0
    for entry in entries:
        if fetched >= max_releases or len(entry.summary) >= MIN_SUMMARY_CHARS:
            enriched.append(entry)
            continue
        if not entry.url:
            enriched.append(entry)
            continue
        try:
            text = fetch_release_text(entry.url, transport=transport)
        except OfficialFeedError:
            # A release page that could not be read is not a reason to lose the entry: the
            # feed's own summary is still what the publisher said. The article is shorter,
            # and `enrichment` on the summary records nothing that is untrue.
            enriched.append(entry)
            continue
        fetched += 1
        enriched.append(
            FeedEntry(
                entry_id=entry.entry_id,
                title=entry.title,
                url=entry.url,
                summary=entry.summary,
                published_at=entry.published_at,
                provider_updated_at=entry.provider_updated_at,
                text=text or None,
            )
        )
    return enriched


# --- parsing ------------------------------------------------------------------------------


def _parse(body: str, feed: Feed) -> list[FeedEntry]:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise MalformedResponseError(
            f"{feed.key}: the feed is not well-formed XML ({exc})"
        ) from None

    if root.tag == f"{_ATOM}feed":
        nodes = root.findall(f"{_ATOM}entry")
        return [_atom_entry(node, feed) for node in nodes]

    # RSS 2.0. `channel/item` is the only shape the Fed's feed uses, and matching nothing
    # yields an empty list rather than a guess at some other structure.
    channel = root.find("channel")
    if channel is None:
        raise MalformedResponseError(
            f"{feed.key}: the response is neither an Atom feed nor an RSS channel"
        )
    return [_rss_item(node, feed) for node in channel.findall("item")]


def _rss_item(node: ElementTree.Element, feed: Feed) -> FeedEntry:
    title = _clean(_text_of(node, "title"))
    link = _clean(_text_of(node, "link"))
    # The Fed's `guid` duplicates its link; using it as the id keeps the identity the
    # publisher states while the URL stays where a reader goes.
    entry_id = _clean(_text_of(node, "guid")) or link or title
    if not entry_id:
        raise MalformedResponseError(f"{feed.key}: an item has no guid, link or title")

    published = _parse_rfc822(_text_of(node, "pubDate"), feed, entry_id)
    return FeedEntry(
        entry_id=entry_id,
        title=title,
        url=link,
        summary=_clean(_text_of(node, "description")),
        published_at=published,
        provider_updated_at=None,
    )


def _atom_entry(node: ElementTree.Element, feed: Feed) -> FeedEntry:
    title = _clean(_text_of(node, f"{_ATOM}title"))
    link = _atom_link(node)
    entry_id = _clean(_text_of(node, f"{_ATOM}id")) or link or title
    if not entry_id:
        raise MalformedResponseError(f"{feed.key}: an entry has no id, link or title")

    published = _parse_iso(_text_of(node, f"{_ATOM}published"), feed, entry_id)
    updated = _text_of(node, f"{_ATOM}updated")
    return FeedEntry(
        entry_id=entry_id,
        title=title,
        url=link,
        summary=_clean(_text_of(node, f"{_ATOM}content")),
        published_at=published,
        # Only when it differs: an Atom feed repeats `updated` for a new entry, and storing
        # that as an "update" would say a release was revised when it was only published.
        provider_updated_at=_parse_optional_iso(updated, feed, entry_id, published),
    )


def _atom_link(node: ElementTree.Element) -> str:
    link = node.find(f"{_ATOM}link")
    if link is None:
        return ""
    href = link.get("href")
    if href:
        return _clean(href)
    return _clean(link.text or "")


def _text_of(node: ElementTree.Element, tag: str) -> str:
    found = node.find(tag)
    return "" if found is None or found.text is None else found.text


def _clean(value: str) -> str:
    """Collapse whitespace. The text is *not* stripped of markup here -- that happens once,
    in `app.htmltext.to_text`, when the summary becomes an article's body."""
    return _WHITESPACE.sub(" ", value or "").strip()


def _parse_rfc822(value: str, feed: Feed, entry_id: str) -> datetime:
    """RFC 822, which is what RSS 2.0 says `pubDate` is."""
    if not value.strip():
        raise MalformedResponseError(f"{feed.key}: entry {entry_id} has no pubDate")
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        raise MalformedResponseError(
            f"{feed.key}: entry {entry_id} has an unreadable pubDate ({value!r})"
        ) from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_iso(value: str, feed: Feed, entry_id: str) -> datetime:
    if not value.strip():
        raise MalformedResponseError(
            f"{feed.key}: entry {entry_id} has no published timestamp"
        )
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MalformedResponseError(
            f"{feed.key}: entry {entry_id} has an unreadable timestamp ({text!r})"
        ) from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_iso(
    value: str, feed: Feed, entry_id: str, published: datetime
) -> datetime | None:
    if not value.strip():
        return None
    updated = _parse_iso(value, feed, entry_id)
    return None if updated == published else updated


# --- transport ----------------------------------------------------------------------------


def _require_official_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeUrlError(f"{url!r} is not an https URL, so it will not be fetched")
    if parsed.hostname not in ALLOWED_RELEASE_HOSTS:
        raise UnsafeUrlError(
            f"{url!r} is not on an official domain. This client reads only "
            f"{', '.join(sorted(ALLOWED_RELEASE_HOSTS))} -- it does not follow links "
            "elsewhere, and it does not crawl."
        )


def _get(url: str, transport: httpx.BaseTransport | None) -> str:
    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=CONNECT_TIMEOUT_SECONDS,
        pool=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml, */*"},
            follow_redirects=False,
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise ProviderUnavailableError(
                        f"{url} returned HTTP {response.status_code}. Nothing was retried."
                    )
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ResponseTooLargeError(
                            f"{url} exceeded {MAX_RESPONSE_BYTES} bytes and was abandoned. "
                            "An official release is far smaller than that."
                        )
                    chunks.append(chunk)
                return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    except httpx.TimeoutException:
        raise ProviderUnavailableError(
            f"{url} did not respond in time (connect limit {CONNECT_TIMEOUT_SECONDS:g}s, "
            f"read limit {READ_TIMEOUT_SECONDS:g}s). Nothing was retried."
        ) from None
    except httpx.HTTPError:
        raise ProviderUnavailableError(
            f"Could not reach {url}. Check network connectivity from the backend "
            "container. Nothing was retried."
        ) from None


def feed_document(feed: Feed) -> dict[str, Any]:
    """One feed as plain values, for a command or a test to print."""
    return {"key": feed.key, "title": feed.title, "source": feed.source, "url": feed.url}


__all__ = [
    "ALLOWED_RELEASE_HOSTS",
    "BLS_CPI",
    "BLS_EMPLOYMENT_SITUATION",
    "FED_MONETARY_POLICY",
    "FEEDS",
    "FEEDS_BY_KEY",
    "MIN_SUMMARY_CHARS",
    "Feed",
    "FeedEntry",
    "MalformedResponseError",
    "OfficialFeedError",
    "ProviderUnavailableError",
    "ResponseTooLargeError",
    "UnknownFeedError",
    "UnsafeUrlError",
    "enrich_with_release_text",
    "fetch_feed",
    "fetch_release_text",
]
