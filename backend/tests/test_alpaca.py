"""The Alpaca paper client: what it reads, what it refuses, and what it never leaks.

Every case here is a canned HTTP response handed to `httpx.MockTransport`. Nothing in this
file opens a socket, and no credential in it is real.

The two properties worth stating outright, because they are the reason the client exists in
this shape:

* **it is paper-only** -- a live host is refused before a request is made, not warned about;
* **no number touches a float** -- every money field arrives as a JSON string and is parsed
  as `Decimal` from that text, and a field that arrives as a JSON *number* is refused
  rather than converted, because by then its precision is already gone.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

import httpx

from app import alpaca
from app.alpaca import (
    PAPER_BASE_URL,
    DataValidationError,
    LiveHostError,
    MalformedResponseError,
    MissingCredentialsError,
    ProviderAuthError,
    ProviderUnavailableError,
    RateLimitError,
    fetch_snapshot,
)

KEY_ID = "PKTESTKEYIDNOTREAL"
SECRET = "test-secret-not-real"

STARTED_AT = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)

# Byte-for-byte the field list Alpaca documents for /v2/account, trimmed to the ones this
# client reads. Every money value is a string, which is the whole point.
ACCOUNT = {
    "id": "8f3a2b10-4c5d-4e6f-8a9b-0c1d2e3f4a5b",
    "account_number": "PA3TESTACCOUNT",
    "currency": "USD",
    "cash": "5000.55",
    "equity": "12345.67",
    "buying_power": "24691.34",
    "status": "ACTIVE",
}

AAPL = {
    "symbol": "AAPL",
    "qty": "5",
    "avg_entry_price": "180.10",
    "current_price": "201.25",
    "market_value": "1006.25",
    "side": "long",
}


def transport_returning(*responses: object) -> httpx.MockTransport:
    """A transport that answers successive GETs from `responses`, in order.

    Each entry is either a dict (JSON-encoded with HTTP 200) or a full `httpx.Response`.
    """
    queued = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queued:
            raise AssertionError(f"unexpected request to {request.url}")
        nxt = queued.pop(0)
        if isinstance(nxt, httpx.Response):
            return nxt
        return httpx.Response(200, json=nxt)

    return httpx.MockTransport(handler)


def snapshot(**overrides) -> object:
    """One well-formed account + positions read, with any part replaceable."""
    account = overrides.pop("account", ACCOUNT)
    positions = overrides.pop("positions", [AAPL])
    return fetch_snapshot(
        api_key_id=KEY_ID,
        secret_key=SECRET,
        transport=transport_returning(account, positions),
        now=STARTED_AT,
        **overrides,
    )


class ReadingTests(unittest.TestCase):
    def test_every_figure_is_exact_and_carries_no_float_error(self):
        result = snapshot()

        self.assertEqual(result.started_at, STARTED_AT)
        self.assertEqual(result.account_id, ACCOUNT["id"])
        self.assertEqual(result.account_number, "PA3TESTACCOUNT")
        self.assertEqual(result.currency, "USD")
        # 5000.55 is not representable as a binary float. Decimal("5000.55") is exactly
        # that, and this assertion is what would fail if the value had been through one.
        self.assertEqual(result.cash, Decimal("5000.55"))
        self.assertEqual(result.equity, Decimal("12345.67"))

        (position,) = result.positions
        self.assertEqual(position.symbol, "AAPL")
        self.assertEqual(position.quantity, Decimal("5"))
        self.assertEqual(position.average_entry_price, Decimal("180.10"))
        self.assertEqual(position.current_price, Decimal("201.25"))
        self.assertEqual(position.market_value, Decimal("1006.25"))
        self.assertEqual(result.position_count, 1)

    def test_a_fractional_quantity_keeps_every_digit(self):
        result = snapshot(
            positions=[{**AAPL, "qty": "731.298556124", "market_value": "147136.60"}]
        )

        self.assertEqual(result.positions[0].quantity, Decimal("731.298556124"))

    def test_no_open_positions_is_a_valid_answer_of_zero(self):
        """Not an error, and not a missing response: an account holding nothing has to be
        distinguishable from a read that failed, because only one of them may rewrite the
        portfolio."""
        result = snapshot(positions=[])

        self.assertEqual(result.positions, ())
        self.assertEqual(result.position_count, 0)
        self.assertEqual(result.cash, Decimal("5000.55"))

    def test_buying_power_is_not_read_and_cannot_become_cash(self):
        result = snapshot(account={**ACCOUNT, "buying_power": "999999.99"})

        self.assertEqual(result.cash, Decimal("5000.55"))
        self.assertFalse(hasattr(result, "buying_power"))

    def test_credentials_travel_as_headers_and_never_in_the_url(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=ACCOUNT if len(seen) == 1 else [])

        fetch_snapshot(
            api_key_id=KEY_ID,
            secret_key=SECRET,
            transport=httpx.MockTransport(handler),
            now=STARTED_AT,
        )

        self.assertEqual(len(seen), 2)
        for request in seen:
            self.assertEqual(request.headers["APCA-API-KEY-ID"], KEY_ID)
            self.assertEqual(request.headers["APCA-API-SECRET-KEY"], SECRET)
            self.assertNotIn(KEY_ID, str(request.url))
            self.assertNotIn(SECRET, str(request.url))

    def test_both_reads_go_to_the_paper_host(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=ACCOUNT if len(seen) == 1 else [])

        fetch_snapshot(
            api_key_id=KEY_ID,
            secret_key=SECRET,
            transport=httpx.MockTransport(handler),
            now=STARTED_AT,
        )

        self.assertEqual(
            seen,
            [f"{PAPER_BASE_URL}/v2/account", f"{PAPER_BASE_URL}/v2/positions"],
        )


class PaperOnlyTests(unittest.TestCase):
    def test_the_live_host_is_refused(self):
        with self.assertRaises(LiveHostError) as caught:
            fetch_snapshot(
                api_key_id=KEY_ID,
                secret_key=SECRET,
                base_url="https://api.alpaca.markets",
            )

        self.assertIn("paper", str(caught.exception))
        self.assertIn("https://api.alpaca.markets", str(caught.exception))

    def test_a_lookalike_host_is_refused_too(self):
        for host in (
            "https://paper-api.alpaca.markets.evil.example",
            "http://paper-api.alpaca.markets",
            "",
        ):
            with self.subTest(host=host):
                with self.assertRaises(LiveHostError):
                    fetch_snapshot(
                        api_key_id=KEY_ID, secret_key=SECRET, base_url=host
                    )

    def test_the_paper_host_is_accepted_with_a_trailing_slash(self):
        result = snapshot(base_url=f"{PAPER_BASE_URL}/")

        self.assertEqual(result.currency, "USD")

    def test_a_refused_host_never_reaches_the_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a request was made to a host that should be refused")

        with self.assertRaises(LiveHostError):
            fetch_snapshot(
                api_key_id=KEY_ID,
                secret_key=SECRET,
                base_url="https://api.alpaca.markets",
                transport=httpx.MockTransport(handler),
            )


def refuse_to_be_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError("a request was made without credentials")


class CredentialTests(unittest.TestCase):
    def test_a_blank_half_of_the_pair_fails_before_any_request(self):
        for key_id, secret in (("", SECRET), (KEY_ID, ""), ("  ", "  ")):
            with self.subTest(key_id=key_id, secret=secret):
                with self.assertRaises(MissingCredentialsError):
                    fetch_snapshot(
                        api_key_id=key_id,
                        secret_key=secret,
                        transport=httpx.MockTransport(refuse_to_be_called),
                    )

    def test_nothing_configured_fails_before_any_request(self):
        """Settings are patched rather than left to the environment.

        A machine with real credentials in `.env` would otherwise reach the network here
        and the test would pass or fail according to what the developer happened to have
        configured, which is the opposite of what it is asserting.
        """
        with (
            mock.patch.object(alpaca, "_configured_key_id", return_value=None),
            mock.patch.object(alpaca, "_configured_secret_key", return_value=None),
        ):
            with self.assertRaises(MissingCredentialsError) as caught:
                fetch_snapshot(
                    transport=httpx.MockTransport(refuse_to_be_called),
                )

        message = str(caught.exception)
        self.assertIn("ALPACA_API_KEY_ID", message)
        self.assertIn("ALPACA_API_SECRET_KEY", message)

    def test_the_message_names_which_credential_is_missing(self):
        with self.assertRaises(MissingCredentialsError) as caught:
            fetch_snapshot(api_key_id=KEY_ID, secret_key="")

        message = str(caught.exception)
        self.assertIn("ALPACA_API_SECRET_KEY", message)
        self.assertNotIn("ALPACA_API_KEY_ID", message)


class ProviderErrorTests(unittest.TestCase):
    def _failing(self, response: httpx.Response) -> Exception:
        with self.assertRaises(Exception) as caught:  # noqa: B017 - asserted below
            fetch_snapshot(
                api_key_id=KEY_ID,
                secret_key=SECRET,
                transport=transport_returning(response),
                now=STARTED_AT,
            )
        return caught.exception

    def test_a_refused_key_is_an_auth_error(self):
        error = self._failing(
            httpx.Response(401, json={"message": "unauthorized."})
        )

        self.assertIsInstance(error, ProviderAuthError)
        self.assertIn("ALPACA_API_KEY_ID", str(error))

    def test_a_rate_limit_is_its_own_error(self):
        error = self._failing(
            httpx.Response(429, json={"message": "too many requests"})
        )

        self.assertIsInstance(error, RateLimitError)

    def test_a_provider_side_failure_is_an_outage(self):
        error = self._failing(httpx.Response(503, text="<html>nope</html>"))

        self.assertIsInstance(error, ProviderUnavailableError)

    def test_a_timeout_is_an_outage_that_says_nothing_was_retried(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        with self.assertRaises(ProviderUnavailableError) as caught:
            fetch_snapshot(
                api_key_id=KEY_ID,
                secret_key=SECRET,
                transport=httpx.MockTransport(handler),
                now=STARTED_AT,
            )

        self.assertIn("Nothing was retried", str(caught.exception))

    def test_a_credential_echoed_back_in_an_error_is_scrubbed(self):
        """Alpaca is free to quote the key it refused. The message that reaches a log line
        or a client must not carry it."""
        error = self._failing(
            httpx.Response(
                401,
                json={"message": f"key {KEY_ID} / {SECRET} is not valid"},
            )
        )

        message = str(error)
        self.assertNotIn(KEY_ID, message)
        self.assertNotIn(SECRET, message)
        self.assertIn("***", message)

    def test_an_unrecognised_status_is_reported_as_malformed_rather_than_swallowed(self):
        error = self._failing(httpx.Response(418, json={"message": "teapot"}))

        self.assertIsInstance(error, MalformedResponseError)
        self.assertIn("418", str(error))


class PositionValidationTests(unittest.TestCase):
    def test_a_short_position_is_refused_rather_than_dropped(self):
        with self.assertRaises(DataValidationError) as caught:
            snapshot(positions=[{**AAPL, "qty": "-3"}])

        self.assertIn("AAPL", str(caught.exception))
        self.assertIn("long positions only", str(caught.exception))

    def test_a_closed_position_of_zero_is_refused_too(self):
        with self.assertRaises(DataValidationError):
            snapshot(positions=[{**AAPL, "qty": "0"}])

    def test_a_non_positive_entry_price_is_refused(self):
        with self.assertRaises(DataValidationError) as caught:
            snapshot(positions=[{**AAPL, "avg_entry_price": "0"}])

        self.assertIn("average entry price", str(caught.exception))

    def test_a_negative_market_value_is_refused(self):
        with self.assertRaises(DataValidationError):
            snapshot(positions=[{**AAPL, "market_value": "-1.00"}])

    def test_two_positions_for_one_symbol_are_refused(self):
        with self.assertRaises(DataValidationError) as caught:
            snapshot(positions=[AAPL, {**AAPL, "qty": "9"}])

        self.assertIn("two positions for AAPL", str(caught.exception))

    def test_a_json_number_where_a_string_was_promised_is_refused(self):
        """The value has already been through a binary float by the time it arrives, so
        accepting it would silently store a number the broker never sent."""
        with self.assertRaises(DataValidationError) as caught:
            snapshot(positions=[{**AAPL, "market_value": 1006.25}])

        self.assertIn("not the string", str(caught.exception))

    def test_a_missing_field_is_refused_by_name(self):
        with self.assertRaises(DataValidationError) as caught:
            snapshot(positions=[{**AAPL, "current_price": None}])

        self.assertIn("current_price", str(caught.exception))

    def test_a_symbol_too_long_for_the_column_is_refused(self):
        with self.assertRaises(DataValidationError) as caught:
            snapshot(positions=[{**AAPL, "symbol": "A" * 21}])

        self.assertIn("symbol", str(caught.exception))

    def test_positions_come_back_sorted_by_symbol(self):
        result = snapshot(
            positions=[
                {**AAPL, "symbol": "NVDA"},
                {**AAPL, "symbol": "AAPL"},
                {**AAPL, "symbol": "MSFT"},
            ]
        )

        self.assertEqual([item.symbol for item in result.positions], ["AAPL", "MSFT", "NVDA"])


class MalformedPayloadTests(unittest.TestCase):
    def test_an_account_without_an_id_is_refused(self):
        with self.assertRaises(MalformedResponseError) as caught:
            snapshot(account={**ACCOUNT, "id": None})

        self.assertIn("id", str(caught.exception))

    def test_a_positions_body_that_is_not_a_list_is_refused(self):
        with self.assertRaises(MalformedResponseError) as caught:
            snapshot(positions={"symbol": "AAPL"})

        self.assertIn("not a list", str(caught.exception))

    def test_a_position_that_is_not_an_object_is_refused(self):
        with self.assertRaises(MalformedResponseError):
            snapshot(positions=["AAPL"])

    def test_a_non_json_body_is_an_error_rather_than_a_crash(self):
        with self.assertRaises(MalformedResponseError):
            fetch_snapshot(
                api_key_id=KEY_ID,
                secret_key=SECRET,
                transport=transport_returning(
                    httpx.Response(200, text="<html>gateway</html>")
                ),
                now=STARTED_AT,
            )


if __name__ == "__main__":
    unittest.main()
