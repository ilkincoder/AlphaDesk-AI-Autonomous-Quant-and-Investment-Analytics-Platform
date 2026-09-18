"""The NVDA command, which is now a fixed identity over the shared ingestion code.

Everything about *how* a company is ingested -- identity validation, precision, adjustment
metadata, completed bars, duplicate detection, conflicts -- lives in `app.company_ingestion`
and is tested in `test_company_ingestion.py`. What is checked here is only that this wrapper
supplies NVIDIA's identity and behaves as a command: the arguments, the output, the exit code.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import contextlib
import io
import json
import unittest
from unittest import mock

from app.ingest_nvda import EXIT_FAILED, EXIT_OK, NVDA, main
from app.ingestion import IngestionConflictError

SUMMARY = {
    "company": {"cik": "0001045810", "name": "NVIDIA CORP", "ticker": "NVDA", "id": 1},
    "counts": {"daily_prices": {"inserted": 1, "unchanged": 0, "total": 1}},
    "dry_run": False,
}


def invoke(argv, *, error=None):
    """Run the command with the shared ingestion replaced."""
    stdout, stderr = io.StringIO(), io.StringIO()

    with mock.patch("app.ingest_nvda.run") as patched:
        if error is not None:
            patched.side_effect = error
        else:
            patched.return_value = mock.Mock(as_dict=lambda: SUMMARY)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)

    return code, stdout.getvalue(), stderr.getvalue(), patched


class IdentityTests(unittest.TestCase):
    def test_the_command_ingests_nvidia(self):
        _, _, _, patched = invoke([])

        identity = patched.call_args.args[0]
        self.assertEqual(identity.symbol, "NVDA")
        self.assertEqual(identity.exchange, "NASDAQ")
        self.assertEqual(identity.cik, "0001045810")

    def test_the_fixed_identity_carries_a_ten_digit_cik(self):
        """A padded CIK is what preserves the leading zero EDGAR writes."""
        self.assertEqual(len(NVDA.cik), 10)
        self.assertTrue(NVDA.cik.isdigit())

    def test_the_identity_is_not_taken_from_the_command_line(self):
        """The company is fixed, so there is no way to point this at another one."""
        with self.assertRaises(SystemExit):
            invoke(["--symbol", "AAPL"])


class ArgumentTests(unittest.TestCase):
    def test_the_documented_defaults_are_used(self):
        _, _, _, patched = invoke([])

        self.assertEqual(patched.call_args.kwargs["bars"], 30)
        self.assertEqual(patched.call_args.kwargs["filings"], 3)
        self.assertFalse(patched.call_args.kwargs["dry_run"])

    def test_arguments_reach_the_shared_run(self):
        _, _, _, patched = invoke(["--bars", "7", "--filings", "2", "--dry-run"])

        self.assertEqual(patched.call_args.kwargs["bars"], 7)
        self.assertEqual(patched.call_args.kwargs["filings"], 2)
        self.assertTrue(patched.call_args.kwargs["dry_run"])


class OutputTests(unittest.TestCase):
    def test_success_prints_json_and_exits_zero(self):
        code, out, err, _ = invoke([])

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["company"]["ticker"], "NVDA")

    def test_a_conflict_exits_nonzero_with_one_sanitized_line(self):
        code, out, err, _ = invoke([], error=IngestionConflictError("already exists"))

        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(out, "")
        self.assertEqual(err.count("\n"), 1)
        self.assertTrue(err.startswith("error: "))
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
