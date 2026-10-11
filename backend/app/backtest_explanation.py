"""Explaining a stored backtest in prose, from the figures the run already holds.

**Nothing here calculates anything.** The fact sheet is assembled from the run's own stored
columns and result document, and the model is asked to *read* it. A number that is not in that
sheet -- a return the engine did not compute, a drawdown it did not measure -- cannot be
produced from it, and `_invented_figures` refuses a reply that contains one anyway rather than
storing it.

**Unavailable is a state, not a crash.** No key, a provider that will not answer, a reply that
cannot be read, a figure that is not the run's: every one of them returns an `Explanation` with
a status and a reason, and the run's results are untouched. The explanation is a convenience
laid over a result that is already complete, so its failure is never the page's failure. This
is the same direction `app.filing_index` and `app.analysis` fail in.

**The explanation is stored with the model and the prompt version that produced it.** Text a
model wrote is not evidence, and a reader is owed the means to tell where it came from and
whether a later prompt would have said something else. `app.models.RebalanceProposal.rationale`
carries the same three facts for the same reason.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.agent import llm
from app.agent.prompts import BACKTEST_EXPLANATION_PROMPT
from app.config import settings

logger = logging.getLogger(__name__)

# Bumped whenever the prompt or the fact sheet changes shape, and stored on every explanation.
# A stored explanation is only meaningful beside the request that produced it.
PROMPT_VERSION = "1"

# The statuses an explanation can carry. `unavailable` is an answer, not an error: the page
# shows the results either way and offers the explanation again.
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"

# Why an explanation is not there. Stable words rather than sentences, so a page can decide what
# to offer -- a missing key is not worth a retry button, a provider that timed out is.
REASON_NOT_CONFIGURED = "model_not_configured"
REASON_PROVIDER = "provider_unavailable"
REASON_UNREADABLE = "reply_unreadable"
REASON_INVENTED_FIGURE = "figure_not_in_the_run"

# How much prose one explanation may cost. Long enough for four short paragraphs, short enough
# that the reply is a summary of the figures rather than a story about them.
MAX_EXPLANATION_TOKENS = 700

# Figures a reply may mention: a percentage or a currency amount, with or without the sign.
# Only these are checked -- see `_invented_figures`.
_PERCENT = re.compile(r"(?<![\d.])(-?\d+(?:\.\d+)?)\s*%")
_CURRENCY = re.compile(r"\$\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Explanation:
    """One explanation, or the reason there is not one."""

    status: str
    text: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    reason: str | None = None
    detail: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status == STATUS_OK


def _unavailable(reason: str, detail: str) -> Explanation:
    return Explanation(status=STATUS_UNAVAILABLE, reason=reason, detail=detail)


def explain(run: Mapping[str, object], *, client: object | None = None) -> Explanation:
    """Generate an explanation of one stored run, or say why it could not be generated.

    `client` is injected rather than built, for the reason `app.agent.run` gives: a test that
    forgot to pass one would otherwise make a real call and spend real money. In production it
    is `None` and the configured client is built here -- inside the request, never at import,
    so the application starts and serves its existing features without a key.
    """
    sheet = facts(run)

    if client is None:
        try:
            client = llm.build_client(settings)
        except llm.ProviderAuthError as exc:
            return _unavailable(REASON_NOT_CONFIGURED, str(exc))

    messages = [
        {"role": "system", "content": BACKTEST_EXPLANATION_PROMPT},
        {"role": "user", "content": sheet},
    ]

    try:
        response = client.complete(messages=messages, max_tokens=MAX_EXPLANATION_TOKENS)
    except llm.ProviderAuthError as exc:
        return _unavailable(REASON_NOT_CONFIGURED, str(exc))
    except llm.ProviderUnavailableError as exc:
        return _unavailable(REASON_PROVIDER, str(exc))
    except llm.ModelError as exc:
        return _unavailable(REASON_UNREADABLE, str(exc))

    text = (response.content or "").strip()
    if not text:
        return _unavailable(REASON_UNREADABLE, "the model returned no explanation")

    invented = _invented_figures(text, run)
    if invented:
        # Not stored, and not shown: an explanation carrying a figure the run never produced is
        # worse than no explanation, because it reads like a measurement.
        logger.warning(
            "discarded an explanation of run %s: it stated %s, which the run does not contain",
            run.get("id"),
            ", ".join(sorted(invented)),
        )
        return _unavailable(
            REASON_INVENTED_FIGURE,
            "the explanation stated figures this run does not contain, so it was discarded "
            "rather than stored",
        )

    return Explanation(
        status=STATUS_OK,
        text=text,
        model=str(getattr(client, "model_name", "") or "") or None,
        prompt_version=PROMPT_VERSION,
    )


# --- the fact sheet ---------------------------------------------------------------------

def facts(run: Mapping[str, object]) -> str:
    """Everything the explanation is allowed to be about, as plain lines.

    Deterministic: the same stored run produces the same sheet, and every figure in it was
    calculated by the engine and recorded on the row. Assembled here rather than left to the
    prompt so that what the model is given can be read, tested and refused.
    """
    result = _mapping(run.get("result"))
    performance = _mapping(result.get("performance"))
    benchmark = _mapping(result.get("benchmark"))
    coverage = _mapping(result.get("coverage"))
    parameters = _mapping(run.get("parameters"))

    lines = [
        f"Instrument: {run.get('symbol')} on {run.get('exchange')}"
        + (f" ({run.get('instrument_name')})" if run.get("instrument_name") else ""),
        f"Window: {run.get('start_date')} to {run.get('end_date')}, "
        f"{coverage.get('sessions')} session(s) traded inside it, after "
        f"{coverage.get('warmup_sessions')} session(s) of warm-up the test did not cover",
        f"Strategy: {run.get('strategy_id')} version {run.get('strategy_version')}, run with "
        + ", ".join(f"{name}={value}" for name, value in parameters.items()),
        f"Initial capital: {run.get('initial_capital')} {run.get('currency')}",
        f"Costs: commission {run.get('commission_per_share')} USD per share each side, "
        f"slippage {run.get('slippage_percent')}% of the execution price",
        _strategy_line(result, performance, coverage),
        _benchmark_line(result, benchmark),
        _position_line(result),
        f"Prices: {run.get('provider')}, {run.get('adjustment_basis')} "
        f"(provider mode: {run.get('provider_adjust_mode')}), "
        f"{run.get('data_sessions')} bar(s) retrieved covering "
        f"{run.get('data_first_date')} to {run.get('data_last_date')}",
    ]

    assumptions = _strings(result.get("assumptions"))
    limitations = _strings(result.get("limitations"))
    if assumptions:
        lines.append("What the run assumed: " + " ".join(assumptions))
    if limitations:
        lines.append("What the run says it does not show: " + " ".join(limitations))

    return "\n".join(lines)


def _strategy_line(result: Mapping, performance: Mapping, coverage: Mapping) -> str:
    trades = result.get("trades") or []
    sessions = coverage.get("sessions") or 0
    completed = sum(1 for trade in trades if trade.get("side") == "sell")
    return (
        f"Strategy result: final value {performance.get('final_value')} USD, "
        f"net total return {performance.get('total_return_percent')}%, "
        f"maximum drawdown {performance.get('max_drawdown_percent')}%, "
        f"{len(trades)} executed trade(s) and {completed} completed round trip(s) over "
        f"{sessions} session(s)"
    )


def _benchmark_line(result: Mapping, benchmark: Mapping) -> str:
    if not benchmark:
        return "Buy and hold: not available for this run"
    return (
        f"Buy and hold over the same window, instrument and costs: final value "
        f"{benchmark.get('final_value')} USD, net total return "
        f"{benchmark.get('total_return_percent')}%, maximum drawdown "
        f"{benchmark.get('max_drawdown_percent')}%"
    )


def _position_line(result: Mapping) -> str:
    position = result.get("open_position")
    if not position:
        return (
            "Position at the end: none, so the whole result is realised and the account is all "
            f"cash ({result.get('cash_remaining')} USD)"
        )
    return (
        f"Position at the end: {position.get('quantity')} share(s) bought on "
        f"{position.get('entry_date')} at {position.get('entry_price')} and still held, marked "
        f"on {position.get('mark_date')} at {position.get('mark_price')} "
        f"({position.get('market_value')} USD), with {result.get('cash_remaining')} USD in cash"
    )


# --- the code that makes the prompt a rule -----------------------------------------------

def _invented_figures(text: str, run: Mapping[str, object]) -> set[str]:
    """Percentage and currency figures in `text` that the run does not contain.

    **Only figures are checked, and only these two kinds.** A percentage or a dollar amount is
    a measurement, and a measurement the engine did not make is an invention however plausible
    it reads. Bare integers are deliberately left alone: "three trades", "2024" and "the second
    session" are prose about a run, and a check that refused them would refuse good
    explanations.

    Rounding is allowed. A reply that writes -28.6% where the run says -28.56% has read the
    figure correctly; one that writes -31.2% has not.
    """
    allowed = _allowed_figures(run)
    found: set[str] = set()
    for pattern in (_PERCENT, _CURRENCY):
        for match in pattern.finditer(text):
            written = match.group(1)
            if not _matches_a_figure(written, allowed):
                found.add(written)
    return found


def _matches_a_figure(written: str, allowed: set[Decimal]) -> bool:
    try:
        value = Decimal(written.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - the pattern only matches valid numbers
        return False
    for figure in allowed:
        for places in (0, 1, 2, 3, 4):
            if value == figure.quantize(Decimal(1).scaleb(-places)):
                return True
    return False


def _allowed_figures(run: Mapping[str, object]) -> set[Decimal]:
    """Every figure on the row a percentage or a dollar sign could legitimately be attached to.

    Read from the same places `facts` reads, plus the per-trade ledger -- a reply may quote the
    price a trade filled at.
    """
    result = _mapping(run.get("result"))
    values: list[object] = [
        run.get("initial_capital"),
        run.get("commission_per_share"),
        run.get("slippage_percent"),
    ]
    for section in ("performance", "benchmark"):
        values.extend(_mapping(result.get(section)).values())
    values.append(result.get("cash_remaining"))
    position = _mapping(result.get("open_position"))
    values.extend(position.values())
    for trade in result.get("trades") or []:
        values.extend(_mapping(trade).values())

    figures: set[Decimal] = set()
    for value in values:
        try:
            figures.add(Decimal(str(value)))
        except (InvalidOperation, TypeError):
            continue
    return figures


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _strings(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, (list, tuple)) else []


__all__ = [
    "Explanation",
    "MAX_EXPLANATION_TOKENS",
    "PROMPT_VERSION",
    "REASON_INVENTED_FIGURE",
    "REASON_NOT_CONFIGURED",
    "REASON_PROVIDER",
    "REASON_UNREADABLE",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "explain",
    "facts",
]
