"""Run one Module 1 tool by name and print its structured result.

    docker compose exec backend python -m app.tools list
    docker compose exec backend python -m app.tools market_insider_analysis \\
        --symbol NVDA --start 2026-08-06 --end 2026-09-17

One command, one tool per subcommand, and every argument is handed to the tool's own Pydantic
request model -- the CLI does not parse, coerce or validate anything itself. Add `--help` to
any subcommand to see its arguments, or run `list` to see the tools and what they return.

**Exit status says what kind of answer came back**, which is why it is not just 0 or 1:

* `0` -- the tool ran. That includes `unavailable`: "we hold nothing that answers this" is an
  answer about the data, and it is the answer for a large share of honest questions here.
* `1` -- `failed`. The tool could not run: the database, the search index or the embedding
  model was unreachable. Nothing is claimed about the data.
* `2` -- the arguments were rejected before anything was read, so nothing ran at all.

Reading a tool's structured result is not the same as reading its exit code. `0` with
`"status": "partial"` means the call worked and something was withheld; the `reason` field says
what.
"""

import argparse
import json
import sys
import textwrap
from collections.abc import Sequence
from datetime import date
from typing import Any, get_args

from pydantic import ValidationError

from app.db import SessionLocal
from app.search_filings import DEFAULT_TOP_K, MAX_TOP_K
from app.tools import TOOLS, invoke, tool_names
from app.tools.filing_search import TOOL_NAME as FILING_SEARCH
from app.tools.financial_facts import (
    DEFAULT_CANDIDATE_LIMIT,
    MAX_CANDIDATE_LIMIT,
    TOOL_NAME as FINANCIAL_FACTS,
    MetricName,
)
from app.tools.market_insider import TOOL_NAME as MARKET_INSIDER
from app.tools.portfolio import TOOL_NAME as PORTFOLIO_CONTEXT
from app.tools.results import ToolStatus

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

_METRIC_NAMES: tuple[str, ...] = get_args(MetricName)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.command == "list":
        _print_tools()
        return EXIT_OK

    try:
        with SessionLocal() as session:
            result = invoke(args.command, _arguments(args), session)
    except ValidationError as exc:
        _print_validation_error(args.command, exc)
        return EXIT_USAGE

    json.dump(result.as_json(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_FAILED if result.status is ToolStatus.FAILED else EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools",
        description=(
            "Invoke one Module 1 analysis tool and print its structured result. Read-only: "
            "nothing is written, fetched or indexed."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("list", help="show the tools and what each returns")

    market = subcommands.add_parser(
        MARKET_INSIDER, help="price move and insider transactions over a window"
    )
    market.add_argument("--symbol", required=True, help="ticker, for example NVDA")
    market.add_argument(
        "--start",
        dest="start_date",
        type=_iso_date,
        required=True,
        help="window start, YYYY-MM-DD; required, and never widened to fit the stored data",
    )
    market.add_argument(
        "--end",
        dest="end_date",
        type=_iso_date,
        required=True,
        help="window end, YYYY-MM-DD; this is what the information cutoff is derived from",
    )

    facts = subcommands.add_parser(
        FINANCIAL_FACTS, help="one reported financial figure for one period"
    )
    facts.add_argument("--symbol", required=True, help="ticker, for example NVDA")
    facts.add_argument(
        "--metric", required=True, choices=_METRIC_NAMES, help="which concept to read"
    )
    facts.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        required=True,
        help="analysis cut-off date, YYYY-MM-DD; required",
    )
    facts.add_argument(
        "--period-start",
        dest="period_start",
        type=_iso_date,
        default=argparse.SUPPRESS,
        help="period start, YYYY-MM-DD; omit it and no start is required",
    )
    facts.add_argument(
        "--period-end",
        dest="period_end",
        type=_iso_date,
        default=argparse.SUPPRESS,
        help="period end, YYYY-MM-DD",
    )
    facts.add_argument(
        "--accession",
        dest="accession_number",
        default=argparse.SUPPRESS,
        help="the filing that reported it, for example 0001045810-26-000075",
    )
    facts.add_argument("--unit", default=argparse.SUPPRESS, help="unit, for example USD")
    facts.add_argument(
        "--candidate-limit",
        dest="candidate_limit",
        type=int,
        default=argparse.SUPPRESS,
        help=(
            f"how many observations to list (default: {DEFAULT_CANDIDATE_LIMIT}, "
            f"maximum: {MAX_CANDIDATE_LIMIT})"
        ),
    )

    search = subcommands.add_parser(
        FILING_SEARCH, help="passages from the indexed filings relevant to a question"
    )
    search.add_argument("--symbol", required=True, help="ticker, for example NVDA")
    search.add_argument(
        "--question", required=True, help="the question to search the filing text for"
    )
    search.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        required=True,
        help="analysis cut-off date, YYYY-MM-DD; required, because it decides what counts "
        "as publicly available",
    )
    search.add_argument(
        "--top-k",
        dest="top_k",
        type=int,
        default=argparse.SUPPRESS,
        help=f"how many passages to return (default: {DEFAULT_TOP_K}, maximum: {MAX_TOP_K})",
    )

    context = subcommands.add_parser(
        PORTFOLIO_CONTEXT, help="whether a symbol is held, and the portfolio's value"
    )
    context.add_argument("--symbol", required=True, help="ticker, for example NVDA")

    return parser


def _arguments(args: argparse.Namespace) -> dict[str, Any]:
    """The given arguments, keyed exactly as the tool's request model declares them.

    Every optional flag uses `argparse.SUPPRESS`, so an argument that was not given is absent
    from the namespace entirely and the request model's own default applies. The CLI therefore
    holds no defaults of its own to fall out of step with the model's.
    """
    return {key: value for key, value in vars(args).items() if key != "command"}


def _print_tools() -> None:
    """Every tool, in declaration order, with what it is for and what it returns."""
    assert len(TOOLS) == len(tool_names())
    for name, spec in TOOLS.items():
        print(name)
        print(_wrap(f"when to use: {spec.description}", indent=2))
        print(_wrap(f"returns: {spec.payload}", indent=2))
        print(
            _wrap(f"arguments: {', '.join(spec.request_model.model_fields)}", indent=2)
        )
        print()


def _wrap(text: str, *, indent: int = 0, width: int = 78) -> str:
    """Wrap on spaces so a long description stays readable in a terminal."""
    padding = " " * indent
    return textwrap.fill(
        text,
        width=width,
        initial_indent=padding,
        subsequent_indent=padding,
        break_long_words=False,
    )


def _print_validation_error(command: str, exc: ValidationError) -> None:
    """Say which argument was wrong, in the caller's own vocabulary.

    Nothing has been read at this point: the request model is validated before the tool is
    called, so a rejected request cannot half-run.
    """
    print(f"error: the arguments for {command} were rejected", file=sys.stderr)
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "arguments"
        print(f"  {location}: {error['msg']}", file=sys.stderr)
    print("Nothing was read. Run with --help to see the accepted arguments.", file=sys.stderr)


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date in YYYY-MM-DD form"
        ) from None


if __name__ == "__main__":
    raise SystemExit(main())
