"""Turning what three sources said into one kind of article.

Pure: dataclasses in, dataclasses out. No database, no network, no settings. The clients
fetch and the ingestion writes; everything in between is decided here, which is what makes
the decisions testable without either.

**Four decisions worth knowing before reading the code.**

* **Publication time is the publisher's, and is never replaced by ours.** `published_at`
  comes from the provider and is what the article is ordered by. `ingested_at` is when this
  database learned about it, and the two are separate facts that happen to be equal only for
  an article that has just broken. Stamping an article with the time we read it would make a
  year-old release look new, and it is the one mistake here that cannot be detected later.
* **A provider that supplies no id is identified by its canonical URL.** That is the
  documented fallback, and it lives in one column (`provider_article_id`) rather than in a
  second key, so there is exactly one rule about what makes two articles the same one.
* **The summary is the body when there is nothing better.** A feed's own `description` is
  sometimes one sentence and sometimes the headline repeated; a release page's text is
  preferred when it was fetched. Whichever it is, it is the publisher's words, cleaned of
  markup and nothing else.
* **Everything fetched is untrusted data.** It is stored as text and read as text. Nothing
  here executes it, resolves anything it references, or sends it to a model -- this
  milestone has no model in the path at all.

`content_sha256` is what decides whether an article has changed, and therefore whether it
needs re-embedding. It covers the title and the body and nothing else: a provider changing
its own `updated_at` on unchanged text must not cause a re-embed.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.alpaca_news import PROVIDER as ALPACA_NEWS_PROVIDER
from app.alpaca_news import NewsItem
from app.htmltext import to_text
from app.official_feeds import Feed, FeedEntry

# What kind of thing an article is. Two values, because they answer different questions:
# a company article is about something a portfolio can hold, a macro article is about the
# economy and belongs to no symbol at all.
CATEGORY_COMPANY = "company"
CATEGORY_MACRO = "macro"

CATEGORIES = (CATEGORY_COMPANY, CATEGORY_MACRO)

# How much of an article's body an API response shows as its excerpt.
EXCERPT_CHARS = 280

# A URL is stored as the publisher wrote it, with only the fragments removed -- a `#section`
# anchor is a position in a page, not a different page, and two links that differ only there
# are the same article.
_FRAGMENT = re.compile(r"#.*$")
_WHITESPACE = re.compile(r"\s+")

# Schemes a stored link may use. A link is handed to a browser, so anything that is not a
# plain web address has no business being one.
_SAFE_SCHEMES = ("http://", "https://")


@dataclass(frozen=True)
class NewsArticle:
    """One article, normalized. The only shape the rest of the system sees.

    `symbols` is empty for a macro release, which is not an absence of information: it says
    the article is not about any particular company, and the macro category says the same
    thing in a second way so a query can filter on whichever it has.
    """

    provider: str
    provider_article_id: str
    source: str
    canonical_url: str
    title: str
    text: str
    symbols: tuple[str, ...]
    category: str
    published_at: datetime
    provider_updated_at: datetime | None
    content_sha256: str

    @property
    def is_macro(self) -> bool:
        return self.category == CATEGORY_MACRO


def from_alpaca(item: NewsItem, *, provider: str = ALPACA_NEWS_PROVIDER) -> NewsArticle:
    """Normalize one Alpaca article.

    The body is the article's `content` when the provider sent one, and its `summary`
    otherwise. Alpaca supplies both today; a future response with only a summary is a
    shorter article, not a broken one.
    """
    title = _collapse(item.headline)
    body = _body(item.content) or _body(item.summary)
    symbols = tuple(
        sorted({symbol.strip().upper() for symbol in item.symbols if symbol.strip()})
    )

    article = NewsArticle(
        provider=provider,
        provider_article_id=item.provider_article_id,
        source=_collapse(item.source) or provider,
        canonical_url=_canonical_url(item.url),
        title=title,
        text=body,
        symbols=symbols,
        # Has symbols means it is about a company. A Benzinga story tagged with no ticker
        # is still company news by provenance, but this milestone stores what it can
        # justify, and the provider's own tags are the evidence for that.
        category=CATEGORY_COMPANY if symbols else CATEGORY_MACRO,
        published_at=item.published_at,
        provider_updated_at=item.provider_updated_at,
        content_sha256="",
    )
    return _with_hash(article)


def from_feed_entry(
    entry: FeedEntry,
    feed: Feed,
    *,
    provider: str | None = None,
) -> NewsArticle:
    """Normalize one Fed or BLS entry.

    A macro article by construction: these feeds carry policy statements and statistical
    releases, which are about the economy rather than about a listed company. Nothing here
    tries to work out which tickers a release might move -- that would be an inference, and
    the brief for this milestone asks for the release, not a reading of it.
    """
    text = _body(entry.text or "") or _body(entry.summary)

    article = NewsArticle(
        provider=provider or f"official_{feed.key}",
        # The publisher's own identifier where it gave one, and the release's URL where it
        # did not -- the same fallback rule that applies to every other provider.
        provider_article_id=_collapse(entry.entry_id)
        or _canonical_url(entry.url)
        or _collapse(entry.title),
        source=feed.source,
        canonical_url=_canonical_url(entry.url),
        title=_collapse(entry.title),
        text=text,
        symbols=(),
        category=CATEGORY_MACRO,
        published_at=entry.published_at,
        provider_updated_at=entry.provider_updated_at,
        content_sha256="",
    )
    return _with_hash(article)


def _with_hash(article: NewsArticle) -> NewsArticle:
    """A copy carrying the hash of what the article actually says.

    Title and body only. `provider_updated_at` is deliberately excluded: it is the
    provider's claim about its own record, and a provider that touches it without changing
    a word has not changed the article.
    """
    digest = hashlib.sha256(
        f"{article.title}\n\n{article.text}".encode("utf-8")
    ).hexdigest()
    return NewsArticle(
        provider=article.provider,
        provider_article_id=article.provider_article_id,
        source=article.source,
        canonical_url=article.canonical_url,
        title=article.title,
        text=article.text,
        symbols=article.symbols,
        category=article.category,
        published_at=article.published_at,
        provider_updated_at=article.provider_updated_at,
        content_sha256=digest,
    )


def _body(value: str) -> str:
    """Markup stripped, whitespace collapsed. Empty when there was nothing to read."""
    if not value or not value.strip():
        return ""
    return _collapse(to_text(value))


def _collapse(value: str) -> str:
    return _WHITESPACE.sub(" ", value or "").strip()


def _canonical_url(value: str) -> str:
    """A link that is safe to store and hand to a browser, or an empty string.

    Not a normalizer: the publisher's own spelling is kept. What is removed is the fragment,
    which names a position inside a page rather than a page, and what is refused is any
    scheme that is not http or https -- a stored link becomes an `href`, and `javascript:`
    in one is the oldest way there is to turn a news feed into an attack.
    """
    collapsed = _collapse(value)
    if not collapsed:
        return ""
    stripped = _FRAGMENT.sub("", collapsed)
    if not stripped.lower().startswith(_SAFE_SCHEMES):
        return ""
    return stripped


def excerpt(text: str, *, limit: int = EXCERPT_CHARS) -> str:
    """The first `limit` characters of a body, ending on a word boundary.

    For a listing, which shows many articles at once and needs a line or two of each. The
    cut is marked with an ellipsis so an excerpt is never mistaken for the whole text.
    """
    collapsed = _collapse(text)
    if len(collapsed) <= limit:
        return collapsed
    head = collapsed[:limit]
    space = head.rfind(" ")
    if space > limit // 2:
        head = head[:space]
    return f"{head.rstrip()}…"


def as_document(article: NewsArticle) -> dict[str, Any]:
    """The article as plain JSON-safe values, for a command to print."""
    return {
        "provider": article.provider,
        "provider_article_id": article.provider_article_id,
        "source": article.source,
        "canonical_url": article.canonical_url,
        "title": article.title,
        "symbols": list(article.symbols),
        "category": article.category,
        "published_at": article.published_at.isoformat(),
        "provider_updated_at": (
            None if article.provider_updated_at is None else article.provider_updated_at.isoformat()
        ),
        "content_sha256": article.content_sha256,
        "text_chars": len(article.text),
    }


__all__ = [
    "CATEGORIES",
    "CATEGORY_COMPANY",
    "CATEGORY_MACRO",
    "EXCERPT_CHARS",
    "NewsArticle",
    "as_document",
    "excerpt",
    "from_alpaca",
    "from_feed_entry",
]
