"""Typed, read-only analysis tools over the Module 1 data.

Four callables, one result envelope, and a lookup table. That is the whole design: this is not
a plugin framework, and there is deliberately nothing here that discovers, registers or
configures a tool at runtime. `TOOLS` is a literal mapping of four names to four functions, so
reading this file tells you exactly what exists.

Every tool:

* takes **one validated request model** -- an undeclared argument is an error, not something
  quietly ignored;
* takes a **session it did not open**, so a caller can run it inside its own transaction;
* **reads only**. No tool inserts, updates, deletes, indexes, ingests, or repairs anything, and
  none of them reaches the network;
* answers with a `ToolResult`, whose `status` says whether the tool could do its job and whose
  `data` holds the payload when there is one.

**Nothing here loads a model or connects to Qdrant at import.** The three SQL-only tools import
neither, and the search tool builds its client and embedder inside the call. So importing this
package is safe in a process that has no model files and no vector store.

Step 7B binds these four to a language model. It should need nothing from this module except
`TOOLS`, `invoke`, and the request models' JSON schemas.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.tools import filing_search, financial_facts, market_insider, portfolio
from app.tools.results import ToolResult


class UnknownToolError(LookupError):
    """A name that is not one of the four tools.

    Raised rather than returned as a failed result: an unknown name is a mistake in the code
    that chose it, not a condition of the data, and a caller that passed one has nothing to
    branch on.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"{name!r} is not a tool. Known tools: {', '.join(tool_names())}"
        )


@dataclass(frozen=True)
class ToolSpec:
    """One tool: what it is called, when to use it, what it accepts, and what it returns."""

    name: str
    # Written to be read by whoever is choosing a tool -- in Step 7B, a language model. It says
    # when the tool applies and what it will not do, in that order.
    description: str
    request_model: type[BaseModel]
    run: Callable[..., ToolResult]
    # A one-line description of the payload, since `data` is a dict: for the two tools that
    # wrap an existing calculation it is that calculation's own `as_dict()`, which is already
    # documented and tested, and re-declaring its fields here would create a second definition
    # of one contract. This is where the shape is pointed at instead.
    payload: str


_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name=market_insider.TOOL_NAME,
        description=market_insider.DESCRIPTION,
        request_model=market_insider.MarketInsiderRequest,
        run=market_insider.run,
        payload=(
            "AnalysisResult.as_dict() -- company, period, prices, transactions, exclusions, "
            "amendments, sample_comparison, overall_conclusion, coverage, limitations, "
            "methodology -- plus `included_transactions` (the row-listing cap) and "
            "`stored_price_availability`."
        ),
    ),
    ToolSpec(
        name=financial_facts.TOOL_NAME,
        description=financial_facts.DESCRIPTION,
        request_model=financial_facts.FinancialFactsRequest,
        run=financial_facts.run,
        payload=(
            "FinancialFactsData -- metric, concept, metric_kind, metric_note, selection, "
            "selection_criteria, observations (with filing availability), counts, and the "
            "excluded observations with their reasons."
        ),
    ),
    ToolSpec(
        name=filing_search.TOOL_NAME,
        description=filing_search.DESCRIPTION,
        request_model=filing_search.FilingSearchRequest,
        run=filing_search.run,
        payload=(
            "RetrievalResult.as_dict() -- status, query, as_of, information_cutoff, top_k, "
            "passages with full citations and similarity -- plus `truncation`."
        ),
    ),
    ToolSpec(
        name=portfolio.TOOL_NAME,
        description=portfolio.DESCRIPTION,
        request_model=portfolio.PortfolioContextRequest,
        run=portfolio.run,
        payload=(
            "PortfolioContextData -- portfolio, price_source and its note, read_at, held, "
            "holding, the full current valuation, and the reason one is unavailable."
        ),
    ),
)

TOOLS: Mapping[str, ToolSpec] = {spec.name: spec for spec in _SPECS}


def tool_names() -> tuple[str, ...]:
    """The four names, in the order they are declared."""
    return tuple(TOOLS)


def invoke(name: str, arguments: Mapping[str, Any], session: Session) -> ToolResult:
    """Validate `arguments` for the named tool and run it.

    Validation happens here, before the tool is called and therefore before it touches the
    database or the search index: an invalid request costs nothing and cannot half-run.

    Raises `UnknownToolError` for a name that is not a tool, and `pydantic.ValidationError`
    for arguments the request model rejects. Neither is a data condition, so neither is
    reported as a `ToolResult`; the operational failures that *are* data conditions --
    an unreachable database, index or model -- come back as `status="failed"`.
    """
    spec = TOOLS.get(name)
    if spec is None:
        raise UnknownToolError(name)
    request = spec.request_model.model_validate(dict(arguments))
    return spec.run(session, request)


__all__ = ["TOOLS", "ToolSpec", "UnknownToolError", "invoke", "tool_names"]
