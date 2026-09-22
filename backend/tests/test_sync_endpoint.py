"""How POST /portfolio/sync's outcomes become HTTP.

Every case here stubs `broker_sync.sync` itself, so nothing in this file reads the real
portfolio or reaches Alpaca. What the sync *does* is `test_broker_sync`'s subject; what is
checked here is the status code and the message a client gets when it cannot do it.

The mapping is the point. A client has to be able to tell apart "you are already syncing"
(409, retry in a moment), "these credentials belong to a different account" (409, a person
must decide), "no credentials are configured" (503, fix the configuration) and "there is no
portfolio to sync into" (404, run the seed) -- because the right response to each is
different.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

from fastapi import HTTPException

from app import broker_sync
from app.alpaca import (
    DataValidationError,
    MalformedResponseError,
    MissingCredentialsError,
    ProviderAuthError,
    ProviderUnavailableError,
    RateLimitError,
)
from app.broker_sync import (
    AccountMismatchError,
    CurrencyMismatchError,
    PortfolioNotStoredError,
    SyncInProgressError,
    SyncOutcome,
)
from app.main import post_portfolio_sync

SYNCED_AT = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)

OUTCOME = SyncOutcome(
    applied=True,
    portfolio_id=1,
    broker="alpaca_paper",
    broker_account_id="8f3a2b10-4c5d-4e6f-8a9b-0c1d2e3f4a5b",
    last_synced_at=SYNCED_AT,
    currency="USD",
    cash_balance=Decimal("2500.25"),
    equity=Decimal("12345.67"),
    position_count=3,
)


def call(outcome=OUTCOME, error: Exception | None = None):
    """Run the handler with `broker_sync.sync` replaced. The session is never used."""
    patched = (
        mock.patch.object(broker_sync, "sync", side_effect=error)
        if error is not None
        else mock.patch.object(broker_sync, "sync", return_value=outcome)
    )
    with patched:
        return post_portfolio_sync(session=mock.MagicMock())


class SyncEndpointTest(unittest.TestCase):
    def status_of(self, error: Exception) -> int:
        with self.assertRaises(HTTPException) as caught:
            call(error=error)
        return caught.exception.status_code

    def test_a_successful_sync_returns_what_the_portfolio_now_holds(self):
        response = call()

        self.assertTrue(response.applied)
        self.assertEqual(response.portfolio_id, 1)
        self.assertEqual(response.broker, "alpaca_paper")
        self.assertEqual(response.broker_account_id, OUTCOME.broker_account_id)
        self.assertEqual(response.last_synced_at, SYNCED_AT)
        self.assertEqual(response.cash_balance, Decimal("2500.25"))
        self.assertEqual(response.equity, Decimal("12345.67"))
        self.assertEqual(response.position_count, 3)

    def test_a_refused_stale_snapshot_is_a_success_with_applied_false(self):
        """Older data was refused, which is the endpoint working. Reporting a failure
        would send a client looking for a fault that is not there."""
        stale = SyncOutcome(**{**vars(OUTCOME), "applied": False})

        response = call(outcome=stale)

        self.assertFalse(response.applied)
        self.assertEqual(response.position_count, 3)

    def test_an_overlapping_sync_is_409(self):
        status = self.status_of(SyncInProgressError("A portfolio sync is already running."))

        self.assertEqual(status, 409)

    def test_a_different_account_is_409_and_never_503(self):
        """Nothing is broken and nothing is down: a person has to decide which account
        this portfolio belongs to, and 503 would read as "try again later"."""
        status = self.status_of(AccountMismatchError("bound to another account"))

        self.assertEqual(status, 409)

    def test_a_missing_portfolio_is_404_with_the_seed_command(self):
        with self.assertRaises(HTTPException) as caught:
            call(error=PortfolioNotStoredError("No portfolio named 'Alpaca Paper'"))

        self.assertEqual(caught.exception.status_code, 404)

    def test_missing_credentials_are_503_and_name_the_variables(self):
        with self.assertRaises(HTTPException) as caught:
            call(
                error=MissingCredentialsError(
                    "No Alpaca paper credentials are configured (ALPACA_API_KEY_ID)."
                )
            )

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("ALPACA_API_KEY_ID", caught.exception.detail)

    def test_every_provider_failure_is_503_with_the_clients_own_message(self):
        for error in (
            ProviderUnavailableError("Alpaca did not respond in time."),
            ProviderAuthError("Alpaca refused the credentials (HTTP 401)."),
            RateLimitError("Alpaca's rate limit is exhausted (HTTP 429)."),
            DataValidationError("AAPL: Alpaca reports a quantity of -3."),
            MalformedResponseError("Alpaca's positions response is not a list."),
            CurrencyMismatchError("The Alpaca account reports its figures in EUR."),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(HTTPException) as caught:
                    call(error=error)

                self.assertEqual(caught.exception.status_code, 503)
                self.assertEqual(caught.exception.detail, str(error))

    def test_the_response_carries_no_credentials(self):
        """What the endpoint returns is the portfolio's state and the account it came
        from. Nothing in the client's configuration is reachable through it."""
        response = call()

        self.assertEqual(
            set(response.model_dump()),
            {
                "applied",
                "portfolio_id",
                "broker",
                "broker_account_id",
                "last_synced_at",
                "currency",
                "cash_balance",
                "equity",
                "position_count",
            },
        )


if __name__ == "__main__":
    unittest.main()
