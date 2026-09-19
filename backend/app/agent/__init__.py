"""The Module 1 analysis flow: a Supervisor, an agent, and the evidence between them.

```
question ──→ Supervisor ──┬─→ Module 1 agent ──→ four read-only tools
                          │            │
                          │            └──→ evidence map (application-owned)
                          │                        │
                          └────────────────────────┴──→ final answer
```

`run_analysis` is the entry point. Everything else in this package is a part of it:

| Module | Responsibility |
|---|---|
| `budget` | The counters and the deadline that bound one run |
| `llm` | The DeepSeek client, and the only place that knows the provider's details |
| `context` | What the run is about, and the date conventions that settle it |
| `evidence` | What the tools returned, and the identifiers citations are checked against |
| `prompts` | The two system prompts, kept apart because the roles are different jobs |
| `module1` | Choosing tools, and the trust boundary that executes them |
| `supervisor` | Routing, and writing the final answer |
| `run` | Wiring it together into one typed result |

**Model output is never trusted with a fact this application can check.** The company, the
market window and the information cutoff come from `context`; the citations come from
`evidence`; the limits come from `budget`. The model chooses what to look at and writes the
prose, and those are the two things it is actually good for.

**Nothing here is imported by the API.** `app.main` does not import this package, no endpoint
reaches it, and importing it loads no model and opens no connection. A missing DeepSeek key
therefore cannot stop the API from starting.
"""

from app.agent.budget import BudgetExhausted, RunBudget
from app.agent.run import (
    FAILED_STATUSES,
    RunDependencies,
    RunResult,
    run_analysis,
)

__all__ = [
    "FAILED_STATUSES",
    "BudgetExhausted",
    "RunBudget",
    "RunDependencies",
    "RunResult",
    "run_analysis",
]
