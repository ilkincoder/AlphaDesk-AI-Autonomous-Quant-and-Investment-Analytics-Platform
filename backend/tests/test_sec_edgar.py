"""The SEC EDGAR client, against canned HTTP responses.

No network. `httpx.MockTransport` supplies whatever each test needs -- a submissions feed,
an archive document, a redirect, a block page, a body that never ends -- and records the
requests so the bounds can be asserted.

The rate limiter is real here, but its `sleep` is a no-op, so the pacing is measurable
without the suite actually waiting. `RateLimiter.wait` advances its own notion of time by
however long it decided to wait, which is what makes that possible.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import pathlib
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import httpx

from app.sec_edgar import (
    MAX_FALLBACK_DOCUMENTS,
    MAX_LIMIT,
    MAX_RESPONSE_BYTES,
    AccessDeniedError,
    CompanyNotFoundError,
    DocumentNotFoundError,
    InvalidRequestError,
    MalformedResponseError,
    MissingUserAgentError,
    ProviderUnavailableError,
    RateLimitError,
    RateLimiter,
    ResponseTooLargeError,
    SecEdgarClient,
    UnsafeUrlError,
    normalize_cik,
    parse_filing_index,
)
from app.form4 import IssuerMismatchError

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "sec"
REAL_XML = (FIXTURE_DIR / "real_nvda_form4_0002152188-26-000005.xml").read_bytes()

USER_AGENT = "AlphaDesk Test (contact: tester@alphadesk.test)"
PLACEHOLDER_AGENT = "AlphaDesk AI (contact: you@example.com)"

CIK = "0001045810"
ACCESSION = "0002152188-26-000005"
ACCESSIONS = (ACCESSION, "0001199039-26-000014", "0001197647-26-000009")


def xml_response(body: bytes = REAL_XML, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code, content=body, headers={"content-type": "text/xml"}
    )


def html_response(status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        text="<html><body>SEC archive index</body></html>",
        headers={"content-type": "text/html"},
    )


def submissions_body(rows, *, older_files=None, **overrides) -> dict:
    """A submissions feed shaped the way SEC returns one: parallel column arrays."""
    recent: dict[str, list] = {
        key: [] for key in
        ("accessionNumber", "form", "filingDate", "acceptanceDateTime",
         "primaryDocument", "reportDate")
    }
    for row in rows:
        recent["accessionNumber"].append(row["accession"])
        recent["form"].append(row["form"])
        recent["filingDate"].append(row.get("filing_date", "2026-01-01"))
        recent["acceptanceDateTime"].append(
            row.get("acceptance", "2026-01-01T12:00:00.000Z")
        )
        recent["primaryDocument"].append(
            row.get("primary", f"xslF345X06/wk-form4_{row['accession'][-6:]}.xml")
        )
        recent["reportDate"].append(row.get("report_date", "2025-12-31"))

    body = {
        "name": "NVIDIA CORP",
        "tickers": ["NVDA"],
        "exchanges": ["Nasdaq"],
        "filings": {"recent": recent},
    }
    body.update(overrides)
    if older_files is not None:
        body["filings"]["files"] = older_files
    return body


def form4_row(
    accession: str,
    filing_date: str = "2026-01-01",
    form: str = "4",
    primary: str | None = None,
    acceptance: str | None = None,
) -> dict:
    row = {"accession": accession, "form": form, "filing_date": filing_date}
    if primary is not None:
        row["primary"] = primary
    if acceptance is not None:
        row["acceptance"] = acceptance
    return row


class Harness:
    """Routes requests by URL fragment, and records every one that was made."""

    def __init__(self, routes):
        self.requests: list[httpx.Request] = []
        self._routes = list(routes)
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for fragment, responder in self._routes:
            if fragment in url:
                return responder(request) if callable(responder) else responder
        raise AssertionError(f"the test did not expect a request to {url}")

    def client(self, **kwargs) -> SecEdgarClient:
        kwargs.setdefault("user_agent", USER_AGENT)
        kwargs.setdefault("transport", self.transport)
        kwargs.setdefault("limiter", RateLimiter(2.0, sleep=lambda _seconds: None))
        return SecEdgarClient(**kwargs)

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


class DiscoveryTests(unittest.TestCase):
    def discover(self, body, limit=3, **kwargs):
        harness = Harness([("submissions", httpx.Response(200, json=body))])
        with harness.client(**kwargs) as client:
            return client.discover(CIK, limit), harness

    def test_selects_only_form_4_and_4a(self):
        body = submissions_body(
            [
                form4_row(ACCESSIONS[0]),
                {"accession": "0000000000-26-000001", "form": "10-K", "filing_date": "2026-01-02"},
                {"accession": "0000000000-26-000002", "form": "424B5", "filing_date": "2026-01-03"},
                form4_row(ACCESSIONS[1], filing_date="2026-02-01", form="4/A"),
            ]
        )

        result, _ = self.discover(body)

        self.assertEqual([ref.form_type for ref in result.filings], ["4/A", "4"])

    def test_deduplicates_by_accession_number(self):
        body = submissions_body([form4_row(ACCESSION), form4_row(ACCESSION)])

        result, _ = self.discover(body)

        self.assertEqual(len(result.filings), 1)

    def test_orders_newest_first_with_a_deterministic_tiebreak(self):
        body = submissions_body(
            [
                form4_row(ACCESSIONS[0], filing_date="2026-01-05"),
                form4_row(ACCESSIONS[1], filing_date="2026-03-01"),
                form4_row(ACCESSIONS[2], filing_date="2026-01-05"),
            ]
        )

        result, _ = self.discover(body)

        self.assertEqual(
            [ref.accession_number for ref in result.filings],
            [ACCESSIONS[1], ACCESSIONS[0], ACCESSIONS[2]],
        )

    def test_the_limit_bounds_what_is_returned(self):
        body = submissions_body([form4_row(a) for a in ACCESSIONS])

        result, _ = self.discover(body, limit=2)

        self.assertEqual(len(result.filings), 2)

    def test_an_empty_result_is_valid(self):
        result, _ = self.discover(submissions_body([form4_row(ACCESSION, form="10-K")]))

        self.assertEqual(result.filings, ())
        self.assertEqual(result.issuer_name, "NVIDIA CORP")

    def test_the_scope_counts_are_reported(self):
        body = submissions_body(
            [form4_row(a) for a in ACCESSIONS],
            older_files=[{"name": "CIK0001045810-submissions-001.json", "filingCount": 1478}],
        )

        result, _ = self.discover(body)

        self.assertEqual(result.recent_filings_scanned, 3)
        self.assertEqual(result.older_filing_files_not_searched, 1478)

    def test_tickers_and_exchanges_are_reported(self):
        result, _ = self.discover(submissions_body([form4_row(ACCESSION)]))

        self.assertEqual(result.tickers, ("NVDA",))
        self.assertEqual(result.exchanges, ("Nasdaq",))

    def test_the_acceptance_timestamp_is_timezone_aware_utc(self):
        body = submissions_body(
            [form4_row(ACCESSION, acceptance="2026-09-11T21:04:47.000Z")]
        )

        result, _ = self.discover(body)
        accepted = result.filings[0].acceptance_datetime

        self.assertEqual(accepted, datetime(2026, 9, 11, 21, 4, 47, tzinfo=timezone.utc))
        self.assertIsNotNone(accepted.tzinfo)

    def test_an_offset_free_timestamp_is_read_as_utc(self):
        """Documented interpretation: EDGAR writes UTC, so a bare value is tagged as UTC."""
        body = submissions_body([form4_row(ACCESSION, acceptance="2026-09-11T21:04:47")])

        result, _ = self.discover(body)
        accepted = result.filings[0].acceptance_datetime

        self.assertEqual(accepted.tzinfo, timezone.utc)
        self.assertEqual(accepted, datetime(2026, 9, 11, 21, 4, 47, tzinfo=timezone.utc))

    def test_a_missing_acceptance_timestamp_stays_none(self):
        body = submissions_body([form4_row(ACCESSION)])
        body["filings"]["recent"]["acceptanceDateTime"] = [None]

        result, _ = self.discover(body)

        self.assertIsNone(result.filings[0].acceptance_datetime)

    def test_the_user_agent_is_sent_on_the_submissions_request(self):
        _, harness = self.discover(submissions_body([form4_row(ACCESSION)]))

        self.assertEqual(harness.requests[0].headers["User-Agent"], USER_AGENT)

    def test_columns_of_unequal_length_are_refused(self):
        """`zip` would silently truncate, which would lose filings without saying so."""
        body = submissions_body([form4_row(ACCESSION)])
        body["filings"]["recent"]["form"].append("4")

        with self.assertRaises(MalformedResponseError):
            self.discover(body)

    def test_a_malformed_accession_number_is_refused(self):
        body = submissions_body([form4_row("not-an-accession")])

        with self.assertRaises(MalformedResponseError):
            self.discover(body)


class CikTests(unittest.TestCase):
    def test_a_bare_cik_is_padded_to_ten_digits(self):
        self.assertEqual(normalize_cik("1045810"), CIK)
        self.assertEqual(normalize_cik(1045810), CIK)

    def test_an_already_padded_cik_is_unchanged(self):
        self.assertEqual(normalize_cik(CIK), CIK)

    def test_a_non_numeric_cik_is_refused(self):
        for value in ("NVDA", "10-45810", "", "  ", "0001045810000"):
            with self.subTest(value=value):
                with self.assertRaises(InvalidRequestError):
                    normalize_cik(value)

    def test_an_out_of_range_limit_is_refused(self):
        for value in (0, MAX_LIMIT + 1, -1):
            with self.subTest(value=value):
                harness = Harness([])
                with harness.client() as client:
                    with self.assertRaises(InvalidRequestError):
                        client.discover(CIK, value)
                self.assertEqual(harness.requests, [], "no request may be made")


class DocumentResolutionTests(unittest.TestCase):
    def fetch(self, primary="xslF345X06/wk-form4_1789160684.xml", routes=None, **kwargs):
        body = submissions_body([form4_row(ACCESSION, primary=primary)])
        all_routes = [("submissions", httpx.Response(200, json=body))]
        all_routes.extend(routes or [])
        harness = Harness(all_routes)
        with harness.client(**kwargs) as client:
            discovery = client.discover(CIK, 1)
            return client.fetch_filings(discovery), harness

    def test_the_xsl_render_prefix_is_stripped_from_the_document_url(self):
        """Fetching the rendered path returns a page, not ownership XML."""
        records, harness = self.fetch(
            routes=[("/000215218826000005/wk-form4_1789160684.xml", xml_response())]
        )

        self.assertEqual(len(records), 1)
        self.assertIn("/000215218826000005/wk-form4_1789160684.xml", harness.urls[-1])
        self.assertNotIn("xslF345X06", harness.urls[-1])

    def test_a_rendered_page_is_not_accepted_as_the_document(self):
        """The rendered page answers HTTP 200 with an XML-ish content type.

        Only the root element distinguishes it, which is why content type is not trusted.
        """
        with self.assertRaises(DocumentNotFoundError):
            self.fetch(
                routes=[
                    ("/wk-form4_1789160684.xml", html_response()),
                    ("index.json", httpx.Response(200, json={"directory": {"item": []}})),
                ]
            )

    def test_the_fallback_finds_the_document_through_the_index(self):
        """The primary candidate is a rendered page; the real document is a sibling."""
        records, harness = self.fetch(
            routes=[
                ("/wk-form4_1789160684.xml", html_response()),
                ("index.json", httpx.Response(200, json={
                    "directory": {"item": [
                        {"name": "0002152188-26-000005-index.html"},
                        {"name": "0002152188-26-000005.txt"},
                        {"name": "actual-form4.xml"},
                    ]}
                })),
                ("actual-form4.xml", xml_response()),
            ]
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].document.issuer_cik, CIK)
        self.assertTrue(records[0].source_xml_url.endswith("/actual-form4.xml"))
        # The render path was tried first and rejected; the .txt was never tried at all.
        self.assertFalse([url for url in harness.urls if url.endswith(".txt")])

    def test_the_fallback_tries_at_most_the_cap(self):
        """A directory listing this client does not control must not be walked blindly."""
        many = [{"name": f"doc{index}.xml"} for index in range(20)]
        harness = Harness([
            ("submissions", httpx.Response(200, json=submissions_body([form4_row(ACCESSION)]))),
            ("/wk-form4_1789160684.xml", html_response()),
            ("index.json", httpx.Response(200, json={"directory": {"item": many}})),
            (".xml", html_response()),
        ])

        with harness.client() as client:
            discovery = client.discover(CIK, 1)
            with self.assertRaises(DocumentNotFoundError):
                client.fetch_filings(discovery)

        attempted = [url for url in harness.urls if "/doc" in url]
        self.assertEqual(len(attempted), MAX_FALLBACK_DOCUMENTS)
        self.assertEqual(len(many), 20, "the listing offered far more than were tried")

    def test_the_fallback_ignores_non_xml_entries(self):
        with self.assertRaises(DocumentNotFoundError):
            self.fetch(
                routes=[
                    ("/wk-form4_1789160684.xml", html_response()),
                    ("index.json", httpx.Response(200, json={
                        "directory": {"item": [
                            {"name": "0002152188-26-000005.txt"},
                            {"name": "0002152188-26-000005-index.html"},
                        ]}
                    })),
                ]
            )

    def test_nothing_found_raises_document_not_found(self):
        harness = Harness([
            ("submissions", httpx.Response(200, json=submissions_body([form4_row(ACCESSION)]))),
            (".xml", html_response()),
            ("index.json", httpx.Response(404)),
        ])
        with harness.client() as client:
            discovery = client.discover(CIK, 1)
            with self.assertRaises(DocumentNotFoundError):
                client.fetch_filings(discovery)

    def test_an_issuer_mismatch_is_refused(self):
        other = REAL_XML.replace(b"<issuerCik>0001045810</issuerCik>",
                                 b"<issuerCik>0000320193</issuerCik>")
        harness = Harness([
            ("submissions", httpx.Response(200, json=submissions_body([form4_row(ACCESSION)]))),
            (".xml", xml_response(other)),
        ])
        with harness.client() as client:
            discovery = client.discover(CIK, 1)
            with self.assertRaises(IssuerMismatchError):
                client.fetch_filings(discovery)

    def test_the_source_url_of_a_fallback_document_is_the_one_actually_read(self):
        records, _ = self.fetch(
            routes=[
                ("/wk-form4_1789160684.xml", html_response()),
                ("index.json", httpx.Response(200, json={
                    "directory": {"item": [{"name": "actual-form4.xml"}]}
                })),
                ("actual-form4.xml", xml_response()),
            ]
        )

        self.assertTrue(records[0].source_xml_url.endswith("/actual-form4.xml"))


class UserAgentTests(unittest.TestCase):
    def test_a_blank_user_agent_fails_before_any_request(self):
        harness = Harness([])

        with self.assertRaises(MissingUserAgentError):
            SecEdgarClient(user_agent="", transport=harness.transport)

        self.assertEqual(harness.requests, [])

    def test_an_unconfigured_application_fails_before_any_request(self):
        """`user_agent=None` means "read the settings", so the settings are what is empty."""
        harness = Harness([])

        with mock.patch("app.sec_edgar._configured_user_agent", return_value=None):
            with self.assertRaises(MissingUserAgentError):
                SecEdgarClient(transport=harness.transport)

        self.assertEqual(harness.requests, [])

    def test_a_placeholder_user_agent_fails_before_any_request(self):
        harness = Harness([])

        with self.assertRaises(MissingUserAgentError) as caught:
            SecEdgarClient(user_agent=PLACEHOLDER_AGENT, transport=harness.transport)

        self.assertIn("example value", str(caught.exception))
        self.assertEqual(harness.requests, [])

    def test_the_error_says_what_to_do(self):
        with self.assertRaises(MissingUserAgentError) as caught:
            SecEdgarClient(user_agent="", transport=Harness([]).transport)

        self.assertIn("SEC_USER_AGENT", str(caught.exception))

    def test_a_real_user_agent_is_accepted(self):
        client = SecEdgarClient(user_agent=USER_AGENT, transport=Harness([]).transport)
        client.close()


class UrlSafetyTests(unittest.TestCase):
    def test_a_redirect_off_sec_is_refused_without_being_followed(self):
        """The destination is validated *before* the request, not after.

        Following first and checking afterwards would mean the request had already gone
        out -- which is the thing the check exists to prevent.
        """
        harness = Harness([
            ("submissions", httpx.Response(200, json=submissions_body([form4_row(ACCESSION)]))),
            ("/wk-form4", httpx.Response(302, headers={"location": "https://evil.example/x.xml"})),
        ])
        with harness.client() as client:
            discovery = client.discover(CIK, 1)
            with self.assertRaises(UnsafeUrlError):
                client.fetch_filings(discovery)

        self.assertEqual(
            [url for url in harness.urls if "evil.example" in url],
            [],
            "the off-host destination must never be requested",
        )

    def test_a_non_https_url_is_refused(self):
        from app.sec_edgar import _assert_safe_url

        with self.assertRaises(UnsafeUrlError):
            _assert_safe_url("http://www.sec.gov/Archives/x.xml")

    def test_a_non_sec_host_is_refused(self):
        from app.sec_edgar import _assert_safe_url

        with self.assertRaises(UnsafeUrlError):
            _assert_safe_url("https://evil.example/x.xml")


class HttpFailureTests(unittest.TestCase):
    def discover_with(self, responder):
        harness = Harness([("submissions", responder)])
        with harness.client() as client:
            return client.discover(CIK, 1)

    def test_a_403_is_reported_as_access_denied(self):
        with self.assertRaises(AccessDeniedError) as caught:
            self.discover_with(httpx.Response(403, text="blocked"))

        self.assertIn("SEC_USER_AGENT", str(caught.exception))

    def test_a_401_is_reported_as_access_denied(self):
        with self.assertRaises(AccessDeniedError):
            self.discover_with(httpx.Response(401, text="unauthorised"))

    def test_a_429_is_reported_as_a_rate_limit(self):
        with self.assertRaises(RateLimitError) as caught:
            self.discover_with(httpx.Response(429, text="slow down"))

        self.assertIn("Nothing was retried", str(caught.exception))

    def test_a_404_on_submissions_is_a_missing_company(self):
        with self.assertRaises(CompanyNotFoundError):
            self.discover_with(httpx.Response(404, text="no such CIK"))

    def test_a_server_error_is_reported_as_unavailable(self):
        with self.assertRaises(ProviderUnavailableError):
            self.discover_with(httpx.Response(503, text="down"))

    def test_a_timeout_is_reported_as_unavailable(self):
        def boom(request):
            raise httpx.ReadTimeout("too slow")

        with self.assertRaises(ProviderUnavailableError) as caught:
            self.discover_with(boom)

        self.assertIn("did not respond in time", str(caught.exception))

    def test_a_connection_failure_is_reported_as_unavailable(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        with self.assertRaises(ProviderUnavailableError):
            self.discover_with(boom)

    def test_html_where_json_was_expected_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            self.discover_with(html_response())

    def test_an_oversized_body_is_abandoned(self):
        huge = b"x" * (MAX_RESPONSE_BYTES + 1)

        with self.assertRaises(ResponseTooLargeError):
            self.discover_with(httpx.Response(200, content=huge))

    def test_no_retries_are_attempted(self):
        """One 429 must mean one request, or a rate limit becomes a credit drain."""
        harness = Harness([("submissions", httpx.Response(429))])

        with harness.client() as client:
            with self.assertRaises(RateLimitError):
                client.discover(CIK, 1)

        self.assertEqual(len(harness.requests), 1)

    def test_transport_errors_are_not_chained_onto_the_reported_error(self):
        def boom(request):
            raise httpx.ConnectError("refused: GET https://data.sec.gov/x?token=abc")

        harness = Harness([("submissions", boom)])
        with harness.client() as client:
            with self.assertRaises(ProviderUnavailableError) as caught:
                client.discover(CIK, 1)

        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("token=abc", str(caught.exception))


class FakeClock:
    """A clock that moves only when the limiter sleeps.

    A real clock plus a no-op sleep does not work: the limiter counts the wait it decided
    on as elapsed, wall time does not, and successive waits compound. That is right for a
    real sleep and wrong to assert against a fake one, so the clock advances by exactly
    what was slept.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def pacing_limiter(per_second: float = 2.0):
    clock = FakeClock()
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)

    return RateLimiter(per_second, clock=clock, sleep=sleep), waits, clock


class RateLimiterTests(unittest.TestCase):
    def test_requests_are_spaced_by_the_interval(self):
        limiter, waits, _ = pacing_limiter(2.0)

        limiter.wait()
        limiter.wait()
        limiter.wait()

        # Two gaps between three requests, each exactly half a second.
        self.assertEqual(len(waits), 2)
        for waited in waits:
            self.assertAlmostEqual(waited, 0.5)

    def test_no_wait_on_the_first_request(self):
        limiter, waits, _ = pacing_limiter()

        limiter.wait()

        self.assertEqual(waits, [])

    def test_no_wait_once_enough_time_has_already_passed(self):
        limiter, waits, clock = pacing_limiter(2.0)

        limiter.wait()
        clock.advance(5.0)
        limiter.wait()

        self.assertEqual(waits, [], "five seconds is well past a half-second interval")

    def test_a_partial_interval_is_topped_up_not_restarted(self):
        """Waiting the full interval after a partial one would halve the real rate."""
        limiter, waits, clock = pacing_limiter(2.0)

        limiter.wait()
        clock.advance(0.2)
        limiter.wait()

        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 0.3)

    def test_the_interval_follows_the_rate(self):
        self.assertAlmostEqual(RateLimiter(2.0).interval, 0.5)
        self.assertAlmostEqual(RateLimiter(4.0).interval, 0.25)

    def test_a_non_positive_rate_is_refused(self):
        with self.assertRaises(ValueError):
            RateLimiter(0)

    def test_the_limiter_covers_discovery_and_documents(self):
        """One limiter for the whole run, not one per phase."""
        limiter, waits, _ = pacing_limiter(2.0)

        harness = Harness([
            ("submissions", httpx.Response(200, json=submissions_body([form4_row(ACCESSION)]))),
            (".xml", xml_response()),
        ])
        with harness.client(limiter=limiter) as client:
            discovery = client.discover(CIK, 1)
            client.fetch_filings(discovery)

        # First request waits for nothing; the second waits on the first's behalf.
        self.assertEqual(len(harness.requests), 2)
        self.assertEqual(len(waits), 1)


class FilingIndexTests(unittest.TestCase):
    """Reading a filing's index table, which is the only place exhibit types appear."""

    INDEX = """
    <html><body><table>
      <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>
      <tr><td>1</td><td>8-K</td><td><a href="x">nvda-20260826.htm</a> iXBRL</td><td>8-K</td></tr>
      <tr><td>2</td><td>EX-99.1</td><td><a href="y">q2fy27pr.htm</a></td><td>EX-99.1</td></tr>
      <tr><td>7</td><td>NVDA LOGO</td><td><a href="z">nvdalogoa19.jpg</a></td><td>GRAPHIC</td></tr>
      <tr><td>&nbsp;</td><td>Complete submission text file</td><td>0001045810-26-000073.txt</td><td>&nbsp;</td></tr>
    </table></body></html>
    """

    def test_entries_carry_type_and_description(self):
        entries = parse_filing_index(self.INDEX)

        self.assertEqual(len(entries), 4)
        by_name = {entry.document_name: entry for entry in entries}
        self.assertEqual(by_name["q2fy27pr.htm"].document_type, "EX-99.1")
        self.assertEqual(by_name["nvda-20260826.htm"].document_type, "8-K")
        self.assertEqual(by_name["nvdalogoa19.jpg"].document_type, "GRAPHIC")

    def test_the_link_markup_does_not_leak_into_the_filename(self):
        entries = parse_filing_index(self.INDEX)

        self.assertIn("nvda-20260826.htm", {e.document_name for e in entries})
        self.assertNotIn("iXBRL", {e.document_name for e in entries})

    def test_the_header_row_is_not_an_entry(self):
        entries = parse_filing_index(self.INDEX)

        self.assertNotIn("Description", {e.description for e in entries})

    def test_sequence_is_read_when_it_is_a_number(self):
        entries = parse_filing_index(self.INDEX)
        by_name = {entry.document_name: entry for entry in entries}

        self.assertEqual(by_name["q2fy27pr.htm"].sequence, 2)
        self.assertIsNone(by_name["0001045810-26-000073.txt"].sequence)

    def test_an_unreadable_index_yields_nothing_rather_than_guessing(self):
        self.assertEqual(parse_filing_index("<html><body>no table</body></html>"), ())

    def test_a_name_that_is_not_a_safe_filename_is_skipped(self):
        entries = parse_filing_index(
            "<table><tr><td>1</td><td>X</td><td>../../etc/passwd</td><td>EX-99.1</td></tr></table>"
        )

        self.assertEqual(entries, ())


class ClientLifecycleTests(unittest.TestCase):
    def test_the_client_closes_its_connection_pool(self):
        client = SecEdgarClient(
            user_agent=USER_AGENT,
            transport=Harness([]).transport,
            limiter=RateLimiter(2.0, sleep=lambda _: None),
        )

        with client as entered:
            self.assertIs(entered, client)

        self.assertTrue(client._client.is_closed)

    def test_a_single_client_serves_discovery_and_documents(self):
        harness = Harness([
            ("submissions", httpx.Response(200, json=submissions_body([form4_row(ACCESSION)]))),
            (".xml", xml_response()),
        ])
        with harness.client() as client:
            discovery = client.discover(CIK, 1)
            records = client.fetch_filings(discovery)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].filing.filing_date, date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()