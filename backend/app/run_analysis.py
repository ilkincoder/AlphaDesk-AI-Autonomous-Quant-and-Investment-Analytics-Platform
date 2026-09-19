"""Ask one question about one company, and print what came back.

    docker compose exec backend python -m app.run_analysis \\
        --question "Compare the price movement with reported insider activity, explain
                    relevant filing risks, and relate this to my demo holdings." \\
        --symbol NVDA --start-date 2026-08-06 --end-date 2026-09-17 --as-of 2026-09-17

A thin wrapper over `app.agent.run_analysis`. It parses arguments, prints the result and sets
an exit code, and it holds no logic that a future `/analysis/chat` endpoint would need.

The company, the market window and the information cutoff are all optional: leave them out and
the Supervisor settles them from the question and the reference date. Pass them and they are
authoritative -- a question that disagrees with them produces a clarification request rather
than a guess. Note that the start and end dates must be given together; the market tool
requires both, and a half-specified window is a question nobody can answer.

**Exit codes separate "answered" from "could not answer".**

* `0` -- the run produced an answer, a clarification request, or an explanation that the
  request is out of scope. All three are answers, and all three are things a person should
  read rather than retry.
* `1` -- the run could not answer: the provider failed, the key is missing, the budget ran
  out, or an answer citing evidence that does not exist was withheld.
* `2` -- the arguments were rejected before anything was attempted.

`--json` prints the whole structured result, including the evidence map and the per-tool log.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from app.agent.run import (
    STATUS_COMPANY_NOT_STORED,
    STATUS_UNSUPPORTED_CAPABILITY,
    RunResult,
    run_analysis,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# Statuses that are answers even though no analysis happened. Spelled out rather than implied
# by "not failed", so a new status has to be classified deliberately. A company this system
# holds nothing for belongs here: the run answered the question it was asked, and the answer
# is that there is nothing stored to answer it from.
_ANSWERS = frozenset(
    {
        "completed",
        "clarification_needed",
        STATUS_UNSUPPORTED_CAPABILITY,
        STATUS_COMPANY_NOT_STORED,
    }
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.start_date and not args.end_date or args.end_date and not args.start_date:
        print(
            "error: --start-date and --end-date must be given together; the market analysis "
            "tool requires both, and a half-specified window cannot be resolved.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        result = run_analysis(
            question=args.question,
            reference_date=args.reference_date,
            symbol=args.symbol,
            start_date=args.start_date,
            end_date=args.end_date,
            as_of=args.as_of,
        )
    except KeyError as exc:  # pragma: no cover - a bug in this application, not the question
        print(f"error: the run could not be assembled: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if args.json:
        json.dump(result.as_json(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_human(result)

    return EXIT_OK if result.status in _ANSWERS else EXIT_FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.run_analysis",
        description=(
            "Ask a question about one company's stored market, insider, filing and "
            "portfolio data, and print the analysis. Read-only: nothing is written."
        ),
    )
    parser.add_argument(
        "--question", required=True, help="the question to answer"
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="ticker, for example NVDA. Optional: the question is read for one if absent. "
        "Authoritative when given.",
    )
    parser.add_argument(
        "--start-date",
        dest="start_date",
        type=_iso_date,
        default=None,
        help="market window start, YYYY-MM-DD. Give with --end-date or not at all.",
    )
    parser.add_argument(
        "--end-date",
        dest="end_date",
        type=_iso_date,
        default=None,
        help="market window end, YYYY-MM-DD",
    )
    parser.add_argument(
        "--as-of",
        dest="as_of",
        type=_iso_date,
        default=None,
        help=(
            "information cut-off date, YYYY-MM-DD: what counts as publicly available. "
            "Defaults to the end of the market window, or to the reference date when there "
            "is no window."
        ),
    )
    parser.add_argument(
        "--reference-date",
        dest="reference_date",
        type=_iso_date,
        default=None,
        help=(
            "the date relative periods are resolved against, YYYY-MM-DD. Defaults to today. "
            "Set it to ask a question as it would have been asked on that date."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the whole structured result instead of the readable summary",
    )
    return parser


def _print_human(result: RunResult) -> None:
    """The readable form: the answer first, then what it rests on."""
    out = sys.stdout
    print(f"{result.run_id}  {result.status}", file=out)
    if result.model:
        print(f"model: {result.model}", file=out)
    print(file=out)
    print(f"Question:  {result.question}", file=out)
    print(f"Reference: {result.reference_date.isoformat()}", file=out)

    if result.resolved:
        resolved = result.resolved
        print(f"Company:   {resolved['symbol']}", file=out)
        window = (
            f"{resolved['start_date']} to {resolved['end_date']}"
            if resolved.get("start_date")
            else "none (this question needs no market window)"
        )
        print(f"Window:    {window}", file=out)
        print(f"Cut-off:   {resolved['as_of']}", file=out)
    else:
        # No request was settled, which is the right outcome for a clarification, a refusal
        # or a conversational reply. Saying "the run stopped" would describe those as
        # failures, and they are not.
        print(
            "Company:   none -- this request was not analysed "
            f"({result.status})",
            file=out,
        )

    if result.destination:
        print(f"Routed to: {result.destination}", file=out)
        if result.route_reason:
            print(f"           {result.route_reason}", file=out)

    print(file=out)
    print("--- Answer ---", file=out)
    print(result.answer if result.answer else "(no answer text was produced)", file=out)

    if result.citations:
        print(file=out)
        print("--- Cited evidence ---", file=out)
        for citation in result.citations:
            filing = citation.get("filing") or {}
            location = ", ".join(
                str(filing[key])
                for key in ("accession_number", "form_type", "section")
                if filing.get(key)
            )
            print(f"[{citation['reference']}] {citation['label']}", file=out)
            if location:
                print(f"      {location}", file=out)
            if filing.get("source_url"):
                print(f"      {filing['source_url']}", file=out)
            if filing.get("similarity") is not None:
                print(
                    f"      similarity {filing['similarity']} (not a probability)",
                    file=out,
                )

    if result.limitations:
        print(file=out)
        print("--- Limitations carried from the tools ---", file=out)
        for item in result.limitations:
            print(f"- {item}", file=out)

    if result.next_steps:
        print(file=out)
        print("--- Possible next steps ---", file=out)
        for step in result.next_steps:
            print(f"- {step}", file=out)

    if result.tool_executions:
        print(file=out)
        print("--- Tools that ran ---", file=out)
        width = max(len(item["tool"]) for item in result.tool_executions)
        for item in result.tool_executions:
            notes = []
            if item.get("reason"):
                notes.append(str(item["reason"]))
            if item.get("reused_previous_result"):
                notes.append("reused an earlier identical call")
            if item.get("rejected"):
                notes.append(f"refused: {item['rejected']}")
            suffix = f"  ({'; '.join(notes)})" if notes else ""
            print(f"{item['tool'].ljust(width)}  {item['status']}{suffix}", file=out)
            if item.get("evidence_refs"):
                print(f"{' ' * width}  -> {', '.join(item['evidence_refs'])}", file=out)

    if result.warnings:
        print(file=out)
        print("--- Warnings ---", file=out)
        for warning in result.warnings:
            print(f"! {warning}", file=out)

    usage = result.usage or {}
    limits = usage.get("limits", {})
    print(file=out)
    print("--- Usage ---", file=out)
    print(
        f"model requests {usage.get('model_requests')}/{limits.get('max_model_requests')}"
        f"  |  tool calls {usage.get('tool_calls')}/{limits.get('max_tool_calls')}"
        f"  |  retries {usage.get('transient_retries')}"
    )
    print(
        f"tokens: {usage.get('prompt_tokens')} in, {usage.get('completion_tokens')} out, "
        f"{usage.get('total_tokens')} total  |  elapsed {usage.get('elapsed_seconds')}s"
    )
    if usage.get("stopped_by"):
        print(f"stopped by: {usage['stopped_by']} -- {usage.get('stop_detail')}")


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date in YYYY-MM-DD form"
        ) from None


if __name__ == "__main__":
    raise SystemExit(main())
