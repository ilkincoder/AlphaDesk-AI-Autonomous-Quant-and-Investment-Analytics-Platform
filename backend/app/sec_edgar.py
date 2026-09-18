"""SEC EDGAR: finding Form 4 filings, and fetching the documents behind them.

Networking only. It imports the pure parser in `app.form4`; that module never imports
this one, so the parsing rules stay testable without a socket.

Everything here is read-only and bounded, for two reasons that are worth naming:

* **The SEC is a public service with a fair-access policy, not an API to be hammered.**
  Requests are serialised through one `RateLimiter` shared by discovery *and* document
  fetches, so the whole command stays under two requests a second. There are no retries:
  a 429 stops the run rather than quietly spending the allowance.
* **Filing documents are found by following data, not by trusting it.** Form 4's
  `primaryDocument` is an **XSL-rendered** path (`xslF345X06/wk-form4_….xml`); fetching it
  returns a rendered page, and the archive directory index returns HTML too. Both answer
  200. So a candidate is accepted only when it actually parses as an ownership document,
  and the fallback that looks for one is capped.

No attempt is ever made to work around an access block. If the SEC says no, this says no.
"""

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx

from app.form4 import Form4Document, parse_ownership_document

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_ROOT = "https://www.sec.gov/Archives/edgar/data"

ALLOWED_HOSTS = frozenset({"data.sec.gov", "www.sec.gov"})

# The root element of the document this client is looking for. A response is accepted
# only if it parses as XML *and* has this root -- the rendered page and the directory
# index are both XML-ish enough to pass a weaker check, and both answer HTTP 200.
OWNERSHIP_ROOT = "ownershipDocument"

# Only these two are Form 4. Anything else in the feed is not this client's business.
FORM4_TYPES = ("4", "4/A")

DEFAULT_LIMIT = 3
MAX_LIMIT = 10

REQUESTS_PER_SECOND = 2.0
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# How many archive documents the fallback will try before giving up. Bounded because the
# alternative is walking a directory whose contents this client does not control.
MAX_FALLBACK_DOCUMENTS = 3

# Redirects are followed by hand, one hop at a time, so the destination is validated
# *before* it is requested. Letting httpx follow them and checking afterwards would mean
# the request had already gone out.
MAX_REDIRECTS = 3

# Accession numbers look like 0002152188-26-000005. Validated before one reaches a URL.
_ACCESSION_PATTERN = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_CIK_PATTERN = re.compile(r"^\d{1,10}$")

# Values that mean "this is still the example, not a real contact". SEC's fair-access
# policy is explicit that a User-Agent naming a working contact is required, and that
# requests without one may be blocked -- so this is checked before anything is sent,
# rather than discovered as a mysterious 403 later.
_PLACEHOLDER_USER_AGENT_MARKERS = ("example.com", "example.org", "example.net")


class SecEdgarError(Exception):
    """Base class for every failure this client reports."""


class MissingUserAgentError(SecEdgarError):
    """`SEC_USER_AGENT` is unset, blank, or still the example value."""


class InvalidRequestError(SecEdgarError):
    """The arguments are not something this client will send -- a bad CIK, a bad limit."""


class UnsafeUrlError(SecEdgarError):
    """A URL was about to be requested that is not HTTPS on an SEC host."""


class AccessDeniedError(SecEdgarError):
    """SEC refused the request -- 401, 403, or its block page."""


class RateLimitError(SecEdgarError):
    """SEC rate-limited the request."""


class CompanyNotFoundError(SecEdgarError):
    """No company is registered under that CIK."""


class DocumentNotFoundError(SecEdgarError):
    """The filing exists, but no ownership document could be read from the archive."""


class ProviderUnavailableError(SecEdgarError):
    """A timeout, a refused connection, or a server-side failure."""


class ResponseTooLargeError(SecEdgarError):
    """A response exceeded the byte cap and was abandoned mid-download."""


class MalformedResponseError(SecEdgarError):
    """A response could not be understood, or was not the kind of thing it claimed."""


@dataclass(frozen=True)
class SecFilingRef:
    """One Form 4 as the submissions feed describes it -- before its document is read."""

    accession_number: str
    form_type: str
    filing_date: date
    acceptance_datetime: datetime | None
    report_date: date | None
    primary_document: str

    @property
    def is_amendment(self) -> bool:
        return self.form_type.upper().endswith("/A")


@dataclass(frozen=True)
class DiscoveryResult:
    """What the submissions feed offered, and how narrow that search was.

    `recent_filings_scanned` and `older_filing_files_not_searched` are reported so the
    scope is visible. Only the recent list is searched; a company's older filings live in
    separate chunks this milestone does not page through, and calling that a complete
    history would be false.
    """

    issuer_cik: str
    issuer_name: str
    tickers: tuple[str, ...]
    exchanges: tuple[str, ...]
    recent_filings_scanned: int
    older_filing_files_not_searched: int
    filings: tuple[SecFilingRef, ...]


@dataclass(frozen=True)
class FilingRecord:
    """A filing's document, plus the provenance the XML itself does not carry.

    `source_xml` is the document exactly as the SEC served it. It is carried here rather
    than dropped after parsing so ingestion can store it without a second fetch -- the
    document is the evidence a later reader would want to re-check a parse against, and
    fetching it twice would double this client's load on the SEC for no gain.
    """

    filing: SecFilingRef
    document: Form4Document
    source_xml: bytes
    source_xml_url: str
    retrieved_at: datetime


class RateLimiter:
    """Spaces request starts at least `1 / per_second` apart, across one run.

    One instance is shared by discovery and document fetching, so the limiter counts the
    whole command rather than resetting per phase. `clock` and `sleep` are injectable so
    tests can prove the pacing without actually waiting.
    """

    def __init__(
        self,
        per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_second <= 0:
            raise ValueError("per_second must be positive")
        self._interval = 1.0 / per_second
        self._clock = clock
        self._sleep = sleep
        self._last_started: float | None = None

    @property
    def interval(self) -> float:
        return self._interval

    def wait(self) -> None:
        """Block until the next request may start, then record that it did."""
        now = self._clock()
        if self._last_started is not None:
            remaining = self._interval - (now - self._last_started)
            if remaining > 0:
                self._sleep(remaining)
                # Counted as elapsed even if the injected sleep did not move the clock,
                # so a fake clock still observes the pacing.
                now += remaining
        self._last_started = now


class SecEdgarClient:
    """One EDGAR session: a User-Agent, a connection pool, and a shared rate limiter.

    Use it as a context manager so the connection pool is always closed.
    """

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
        limiter: RateLimiter | None = None,
        now: datetime | None = None,
    ) -> None:
        resolved = user_agent if user_agent is not None else _configured_user_agent()
        # Checked here, at construction, so a misconfigured environment fails before a
        # connection is opened rather than after a puzzling refusal.
        _validate_user_agent(resolved)
        self._user_agent: str = resolved

        self._now = now
        self._limiter = limiter if limiter is not None else RateLimiter(REQUESTS_PER_SECOND)
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_SECONDS,
                read=READ_TIMEOUT_SECONDS,
                write=CONNECT_TIMEOUT_SECONDS,
                pool=CONNECT_TIMEOUT_SECONDS,
            ),
            # Redirects are handled in `_get`, one hop at a time, so each destination is
            # validated before it is requested rather than after.
            follow_redirects=False,
        )

    def __enter__(self) -> "SecEdgarClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- discovery --------------------------------------------------------------------

    def discover(self, cik: str, limit: int = DEFAULT_LIMIT) -> DiscoveryResult:
        """Find recent Form 4 filings for `cik`, newest first.

        Raises `InvalidRequestError` for a CIK that is not digits, `CompanyNotFoundError`
        when no company is registered under it.
        """
        normalized_cik = normalize_cik(cik)
        _validate_limit(limit)

        url = SUBMISSIONS_URL.format(cik=normalized_cik)
        body, _ = self.fetch_bytes(
            url,
            accept="application/json",
            on_missing=CompanyNotFoundError,
            what=f"CIK {normalized_cik}",
        )
        payload = _decode_json(body, url)
        return _build_discovery(payload, normalized_cik, limit, FORM4_TYPES)

    def discover_forms(
        self, cik: str, form_types: Sequence[str]
    ) -> DiscoveryResult:
        """Every recent filing of the given form types, newest first, uncapped.

        Kept separate from `discover` rather than folded into it. That one is the Form 4
        entry point and its limit and ordering are Step 3's behaviour, which must not move.
        This returns the whole matching set so a caller can pick a 10-K, a 10-Q and several
        8-Ks from a single read, each under its own rule.

        Still `filings.recent` only: a form type absent from it is reported as a gap in the
        search, never as the company not having filed one.
        """
        normalized_cik = normalize_cik(cik)
        if not form_types:
            raise InvalidRequestError("at least one form type is required")

        url = SUBMISSIONS_URL.format(cik=normalized_cik)
        body, _ = self.fetch_bytes(
            url,
            accept="application/json",
            on_missing=CompanyNotFoundError,
            what=f"CIK {normalized_cik}",
        )
        payload = _decode_json(body, url)
        return _build_discovery(payload, normalized_cik, None, tuple(form_types))

    def fetch_document(self, url: str) -> tuple[str, str | None]:
        """Fetch an archive document as text, with its content type.

        Unlike the Form 4 path this makes no claim about what the document should be; the
        caller decides whether it got a 10-K, an earnings release, or something else. The
        URL still goes through the same host and scheme checks.
        """
        body, content_type = self.fetch_bytes(
            url,
            accept="text/html, application/xhtml+xml, text/plain, */*",
            on_missing=DocumentNotFoundError,
            what="archive document",
        )
        try:
            return body.decode("utf-8"), content_type
        except UnicodeDecodeError as exc:
            raise MalformedResponseError(
                f"the document at {url} is not valid UTF-8 and cannot be stored as text"
            ) from exc

    # --- documents --------------------------------------------------------------------

    def fetch_filings(
        self, discovery: DiscoveryResult, limit: int | None = None
    ) -> tuple[FilingRecord, ...]:
        """Fetch and parse a filing's ownership document for each discovered filing."""
        wanted = discovery.filings if limit is None else discovery.filings[:limit]
        return tuple(
            self._fetch_one(ref, discovery.issuer_cik) for ref in wanted
        )

    def _fetch_one(self, ref: SecFilingRef, issuer_cik: str) -> FilingRecord:
        retrieved_at = self._now or datetime.now(timezone.utc)

        source_url = archive_file_url(issuer_cik, ref.accession_number, ref.primary_document)
        body = self._try_document(source_url)
        if body is None:
            body, source_url = self._fallback_document(ref, issuer_cik)

        document = parse_ownership_document(body, expected_issuer_cik=issuer_cik)
        return FilingRecord(
            filing=ref,
            document=document,
            source_xml=body,
            source_xml_url=source_url,
            retrieved_at=retrieved_at,
        )

    def _try_document(self, url: str) -> bytes | None:
        """The bytes at `url`, or None when they are not an ownership document.

        Only "this is not the document" returns None. A timeout, a refusal, or a rate
        limit propagates: those are conditions the caller must hear about, not reasons to
        quietly try a different file.
        """
        try:
            body, _ = self.fetch_bytes(
                url,
                accept="application/xml, text/xml, */*",
                on_missing=DocumentNotFoundError,
                what="archive document",
            )
        except DocumentNotFoundError:
            return None

        if _ownership_root(body) != OWNERSHIP_ROOT:
            return None
        return body

    def _fallback_document(
        self, ref: SecFilingRef, issuer_cik: str
    ) -> tuple[bytes, str]:
        """Look for the ownership XML through the accession's directory listing.

        Bounded twice over: only `.xml` entries are considered, and at most
        `MAX_FALLBACK_DOCUMENTS` of them are tried. The directory index is a listing this
        client does not control, so it is treated as untrusted input throughout.
        """
        index_url = archive_index_url(issuer_cik, ref.accession_number)
        body, _ = self.fetch_bytes(
            index_url,
            accept="application/json",
            on_missing=DocumentNotFoundError,
            what=f"archive index for {ref.accession_number}",
        )
        payload = _decode_json(body, index_url)

        candidates = _xml_names_in_index(payload)
        for name in candidates[:MAX_FALLBACK_DOCUMENTS]:
            candidate_url = archive_file_url(issuer_cik, ref.accession_number, name)
            found = self._try_document(candidate_url)
            if found is not None:
                return found, candidate_url

        raise DocumentNotFoundError(
            f"no ownership XML in the archive directory for {ref.accession_number}; "
            f"looked at {min(len(candidates), MAX_FALLBACK_DOCUMENTS)} of "
            f"{len(candidates)} XML entries"
        )

    # --- transport --------------------------------------------------------------------

    def fetch_bytes(
        self,
        url: str,
        *,
        accept: str,
        on_missing: type[SecEdgarError],
        what: str,
    ) -> tuple[bytes, str | None]:
        """Fetch a URL, returning the body and its content type.

        Public so the Company Facts client can share this transport rather than growing a
        second one: the User-Agent, the rate limiter, the redirect validation, the timeouts,
        the size cap and the error taxonomy all live here and none of them should be
        duplicated.

        Only `www.sec.gov` and `data.sec.gov` over HTTPS, checked before every hop.
        """
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            # Checked before every request, including each redirect hop, so an off-host
            # destination is refused rather than followed and then regretted.
            _assert_safe_url(current)
            self._limiter.wait()

            try:
                with self._client.stream(
                    "GET",
                    current,
                    headers={
                        "User-Agent": self._user_agent,
                        "Accept": accept,
                        "Accept-Encoding": "gzip, deflate",
                    },
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise MalformedResponseError(
                                f"a redirect from {current} carried no Location header"
                            )
                        current = urljoin(current, location)
                        continue

                    _assert_safe_url(str(response.url))
                    self._raise_for_status(response, on_missing=on_missing, what=what)
                    return _read_bounded(response), response.headers.get("content-type")
            except httpx.TimeoutException:
                # `from None`: httpx exceptions carry the request URL, and chaining one
                # would print it in any traceback.
                raise ProviderUnavailableError(
                    "SEC did not respond in time (connect limit "
                    f"{CONNECT_TIMEOUT_SECONDS:g}s, read limit "
                    f"{READ_TIMEOUT_SECONDS:g}s). Nothing was retried."
                ) from None
            except httpx.HTTPError:
                raise ProviderUnavailableError(
                    "Could not reach sec.gov. Check network connectivity from the "
                    "backend container. Nothing was retried."
                ) from None

        raise MalformedResponseError(
            f"more than {MAX_REDIRECTS} redirects from {url}; refusing to keep following"
        )

    @staticmethod
    def _raise_for_status(
        response: httpx.Response, *, on_missing: type[SecEdgarError], what: str
    ) -> None:
        """Turn an unsuccessful status into the error that matches its cause."""
        if response.status_code == 404:
            raise on_missing(f"{what} was not found at {response.url}")
        if response.status_code in (401, 403):
            raise AccessDeniedError(
                f"SEC refused the request (HTTP {response.status_code}). Check that "
                "SEC_USER_AGENT names your application and a contact address SEC could "
                "actually reach."
            )
        if response.status_code == 429:
            raise RateLimitError(
                "SEC rate-limited the request (HTTP 429). Nothing was retried, so no "
                "further load was placed on SEC. Wait before trying again."
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"SEC reported a server error (HTTP {response.status_code}). "
                "Nothing was retried."
            )
        if response.status_code >= 400:
            raise MalformedResponseError(
                f"unexpected HTTP {response.status_code} from {response.url}"
            )


# --- CIK and URL validation -------------------------------------------------------------


def normalize_cik(cik: str | int) -> str:
    """A CIK as a ten-digit string, which is how EDGAR writes them.

    Kept as text rather than an integer so leading zeros survive, the same reasoning as
    `companies.sec_issuer_cik`.
    """
    if isinstance(cik, bool) or not isinstance(cik, (str, int)):
        raise InvalidRequestError(f"a CIK must be a string or an integer, not {type(cik).__name__}")

    text = str(cik).strip()
    if not _CIK_PATTERN.match(text):
        raise InvalidRequestError(
            f"{cik!r} is not a CIK: expected one to ten digits, for example 1045810"
        )
    return text.zfill(10)


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidRequestError("limit must be a whole number")
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidRequestError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")


def _assert_safe_url(url: str) -> None:
    """Only HTTPS, only on SEC hosts.

    Applied both before a request and to the URL actually reached, so an open redirect on
    sec.gov cannot send this client somewhere else.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeUrlError(f"refusing a non-HTTPS URL (scheme {parsed.scheme or 'none'!r})")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise UnsafeUrlError(
            f"refusing a URL outside SEC hosts: {parsed.hostname!r} is not one of "
            f"{', '.join(sorted(ALLOWED_HOSTS))}"
        )


def archive_file_url(issuer_cik: str, accession_number: str, name: str) -> str:
    """The archive URL for one document of one filing.

    Public because company-context ingestion builds these too, and a second URL builder
    would be a second place for the validation below to be forgotten.
    """
    return (
        f"{ARCHIVE_ROOT}/{int(issuer_cik)}/{_accession_path(accession_number)}"
        f"/{_safe_filename(name)}"
    )


def archive_index_url(issuer_cik: str, accession_number: str) -> str:
    """The archive directory index for one filing."""
    return (
        f"{ARCHIVE_ROOT}/{int(issuer_cik)}/{_accession_path(accession_number)}/index.json"
    )


def archive_index_html_url(issuer_cik: str, accession_number: str) -> str:
    """The human-readable filing index, which is where exhibit types live.

    `index.json` lists the same files but not usefully: its `type` field reports `text.gif`
    for HTML, images and XML alike, so nothing can be selected from it by document type.
    """
    return (
        f"{ARCHIVE_ROOT}/{int(issuer_cik)}/{_accession_path(accession_number)}"
        f"/{accession_number}-index.html"
    )


@dataclass(frozen=True)
class FilingIndexEntry:
    """One row of a filing's index table."""

    sequence: int | None
    description: str
    document_name: str
    document_type: str


class _FilingIndexParser(HTMLParser):
    """Collects the index table's rows.

    A real parser rather than a regular expression: the table's cells contain links and
    entities, and a pattern that happened to work on one filing's markup would be a poor
    thing to depend on for every filing.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None and data.strip():
            self._cell.append(data.strip())


def parse_filing_index(html: str) -> tuple[FilingIndexEntry, ...]:
    """Read a filing index page into entries: sequence, description, name, type.

    Returns an empty tuple when nothing parses, which the caller reports as a failure to
    read the index rather than as a filing with no documents.
    """
    parser = _FilingIndexParser()
    parser.feed(html)

    entries: list[FilingIndexEntry] = []
    for row in parser.rows:
        if len(row) < 4:
            continue
        sequence_text, description, document, document_type = row[0], row[1], row[2], row[3]
        if description.strip().lower() == "description":
            # The header row.
            continue
        parts = document.split()
        name = parts[0] if parts else ""
        if not _SAFE_FILENAME.match(name):
            continue
        entries.append(
            FilingIndexEntry(
                sequence=int(sequence_text) if sequence_text.isdigit() else None,
                description=description.strip(),
                document_name=name,
                document_type=document_type.strip(),
            )
        )
    return tuple(entries)


def _accession_path(accession_number: str) -> str:
    if not _ACCESSION_PATTERN.match(accession_number):
        raise MalformedResponseError(
            f"accession number {accession_number!r} is not in the expected "
            "NNNNNNNNNN-NN-NNNNNN form, so no archive URL will be built from it"
        )
    return accession_number.replace("-", "")


def _safe_filename(name: str) -> str:
    """Reduce a document reference to one plain path segment.

    This is what strips EDGAR's `xslF345Xnn/` render prefix, and it is also what stops a
    crafted value from climbing out of the accession directory.
    """
    segment = name.strip().rsplit("/", 1)[-1]
    if not _SAFE_FILENAME.match(segment):
        raise MalformedResponseError(
            f"{name!r} is not a usable document name in an archive path"
        )
    return segment


def _configured_user_agent() -> str | None:
    """From application settings, imported here so this module stays importable without
    a database URL -- settings construction needs one."""
    from app.config import settings

    return settings.sec_user_agent


def _validate_user_agent(value: str | None) -> None:
    if not value or not value.strip():
        raise MissingUserAgentError(
            "SEC_USER_AGENT is not set. SEC requires a User-Agent naming your "
            "application and a contact address, for example "
            "'AlphaDesk AI (contact: you@yourdomain.com)'. Set it in .env, then run: "
            "docker compose up -d backend"
        )
    lowered = value.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_USER_AGENT_MARKERS):
        raise MissingUserAgentError(
            "SEC_USER_AGENT is still the example value from .env.example. SEC's "
            "fair-access policy expects a contact address that reaches a person, and may "
            "block requests without one. Put a real address in .env, then run: "
            "docker compose up -d backend"
        )


# --- response handling ------------------------------------------------------------------


def _read_bounded(response: httpx.Response) -> bytes:
    """Read a body, abandoning it once it passes the cap.

    Streamed rather than read-then-checked, so the limit bounds the download instead of
    merely rejecting it afterwards.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ResponseTooLargeError(
                f"the response from {response.url} passed {MAX_RESPONSE_BYTES} bytes and "
                "was abandoned"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_json(body: bytes, url: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except ValueError:
        raise MalformedResponseError(
            f"{url} did not return JSON; SEC may be serving an HTML error page"
        ) from None
    if not isinstance(payload, dict):
        raise MalformedResponseError(
            f"{url} returned JSON that is a {type(payload).__name__}, not an object"
        )
    return payload


def _ownership_root(body: bytes) -> str | None:
    """The root element name, or None when the bytes are not XML at all.

    This is the test that actually distinguishes an ownership document from the rendered
    page and the directory listing, both of which also answer HTTP 200.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    tag = root.tag.rpartition("}")[2]
    return tag or None


def _xml_names_in_index(payload: Mapping[str, Any]) -> list[str]:
    """`.xml` filenames from an accession's `index.json`, as plain segments."""
    directory = payload.get("directory")
    if not isinstance(directory, Mapping):
        raise MalformedResponseError("the archive index has no 'directory' object")
    items = directory.get("item")
    if not isinstance(items, list):
        raise MalformedResponseError("the archive index has no 'item' list")

    names: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.lower().endswith(".xml"):
            continue
        segment = name.rsplit("/", 1)[-1]
        if _SAFE_FILENAME.match(segment):
            names.append(segment)
    return names


# --- submissions parsing ------------------------------------------------------------------


def _build_discovery(
    payload: Mapping[str, Any], cik: str, limit: int | None, form_types: tuple[str, ...]
) -> DiscoveryResult:
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise MalformedResponseError(
            f"the submissions response for CIK {cik} has no issuer name"
        )

    filings = payload.get("filings")
    if not isinstance(filings, Mapping):
        raise MalformedResponseError(f"the submissions response for CIK {cik} has no 'filings'")

    recent = filings.get("recent")
    if not isinstance(recent, Mapping) or not recent:
        raise MalformedResponseError(
            f"the submissions response for CIK {cik} has no 'recent' filings block"
        )

    older_files = filings.get("files")
    older_count = 0
    if isinstance(older_files, list):
        for chunk in older_files:
            if isinstance(chunk, Mapping):
                count = chunk.get("filingCount")
                older_count += count if isinstance(count, int) else 0

    rows = _column_rows(recent)
    selected: list[SecFilingRef] = []
    seen: set[str] = set()

    for row in rows:
        form_type = row.get("form")
        if form_type not in form_types:
            continue

        accession = row.get("accessionNumber")
        if not isinstance(accession, str) or not _ACCESSION_PATTERN.match(accession):
            raise MalformedResponseError(
                f"the submissions feed lists {form_type!r} with accession "
                f"{accession!r}, which is not in the expected form"
            )
        if accession in seen:
            continue
        seen.add(accession)
        selected.append(_to_ref(row, accession, form_type))

    # Newest first, with the accession number breaking ties so the order is the same on
    # every run rather than whatever the feed happened to return.
    selected.sort(key=lambda ref: (ref.filing_date, ref.accession_number), reverse=True)

    return DiscoveryResult(
        issuer_cik=cik,
        issuer_name=name.strip(),
        tickers=tuple(sorted(t for t in _string_list(payload.get("tickers")))),
        exchanges=tuple(sorted(t for t in _string_list(payload.get("exchanges")))),
        recent_filings_scanned=len(rows),
        older_filing_files_not_searched=older_count,
        filings=tuple(selected if limit is None else selected[:limit]),
    )


def _column_rows(recent: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The feed's parallel column arrays as a list of row dicts.

    SEC returns `recent` as `{"form": [...], "accessionNumber": [...], ...}`. Unequal
    column lengths would mean `zip` silently truncating, so that is refused outright.
    """
    keys = list(recent.keys())
    columns = [recent[key] for key in keys]
    if any(not isinstance(column, list) for column in columns):
        raise MalformedResponseError("the 'recent' block is not made of parallel lists")
    lengths = {len(column) for column in columns}
    if len(lengths) > 1:
        raise MalformedResponseError(
            f"the 'recent' block has columns of differing lengths {sorted(lengths)}; "
            "rows cannot be aligned"
        )
    return [dict(zip(keys, values)) for values in zip(*columns)]


def _to_ref(row: Mapping[str, Any], accession: str, form_type: str) -> SecFilingRef:
    filing_date = _parse_date(row.get("filingDate"), f"{accession}/filingDate")
    if filing_date is None:
        raise MalformedResponseError(f"{accession} has no filing date")

    primary_document = row.get("primaryDocument")
    if not isinstance(primary_document, str) or not primary_document.strip():
        raise MalformedResponseError(f"{accession} has no primary document")

    return SecFilingRef(
        accession_number=accession,
        form_type=form_type,
        filing_date=filing_date,
        acceptance_datetime=_parse_timestamp(
            row.get("acceptanceDateTime"), f"{accession}/acceptanceDateTime"
        ),
        report_date=_parse_date(row.get("reportDate"), f"{accession}/reportDate"),
        primary_document=primary_document,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _parse_date(value: Any, where: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise MalformedResponseError(f"{where} is not a calendar date ({value!r})") from None


def _parse_timestamp(value: Any, where: str) -> datetime | None:
    """An EDGAR timestamp, always returned timezone-aware.

    EDGAR writes acceptance times in UTC, generally with an explicit `Z` or offset, which
    is respected. A value with *no* offset is read as UTC and tagged as such -- the
    alternative is a naive datetime that cannot be compared with an aware one later, and
    silently mixes two different meanings of "when".
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise MalformedResponseError(f"{where} is not a timestamp ({value!r})") from None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed