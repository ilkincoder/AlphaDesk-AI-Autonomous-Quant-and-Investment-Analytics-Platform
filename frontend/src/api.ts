/** The one place the frontend talks to the backend.
 *
 * The URL is relative on purpose. The browser calls its own origin at /api/..., Vite
 * forwards that to the backend service, and the browser never learns the backend's
 * hostname or port. It also means there is no CORS to configure: from the browser's
 * point of view there is only one origin.
 */

/** Mirrors `ValuationHoldingOut` in backend/app/schemas.py.
 *
 * Every numeric field is a string because that is what the API sends — see
 * "Why decimals are strings" in the README. Widening these to `number` would undo
 * that on the first line of component code.
 */
export type ValuationHolding = {
  symbol: string
  quantity: string
  price: string
  holding_value: string
  allocation_percent: string | null
}

/** Mirrors `PortfolioValuationOut` in backend/app/schemas.py. */
export type Valuation = {
  portfolio_id: number
  currency: string
  price_source: string
  cash_balance: string
  holdings_value: string
  total_value: string
  cash_allocation_percent: string | null
  holdings: ValuationHolding[]
}

/** A request that did not produce usable data.
 *
 * `status` is null when the request never reached the API at all, which is how the
 * caller tells "the backend said no" apart from "there is no backend".
 */
export class ApiError extends Error {
  readonly status: number | null

  constructor(message: string, status: number | null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** Mirrors `ScenarioOut` in backend/app/schemas.py. */
export type Scenario = {
  portfolio_id: number
  currency: string
  price_source: string
  symbol: string
  price_change_percent: string
  price_before: string
  price_after: string
  holding_value_before: string
  holding_value_after: string
  total_value_before: string
  total_value_after: string
  change_value: string
  change_percent: string | null
}

/** What the scenario endpoint accepts.
 *
 * The percentage is a *string*, deliberately: `Decimal` on the far side parses it
 * exactly, whereas a JSON number would have been through a float first.
 */
export type ScenarioBody = {
  symbol: string
  price_change_percent: string
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(url, init)
  } catch (cause) {
    // An aborted request is not a failure to report: it means the component moved on.
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new ApiError('Could not reach the API.', null)
  }

  if (!response.ok) {
    throw new ApiError(await readErrorDetail(response), response.status)
  }

  return (await response.json()) as T
}

export function fetchValuation(signal?: AbortSignal): Promise<Valuation> {
  return request<Valuation>('/api/portfolio/valuation', { signal })
}

/** POST, because the question has a body rather than a path: "what if *this* symbol
 *  moved by *this* much". Nothing is stored on the server. */
export function fetchScenario(
  body: ScenarioBody,
  signal?: AbortSignal,
): Promise<Scenario> {
  return request<Scenario>('/api/portfolio/scenario', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}

/** Prefer the backend's own wording.
 *
 * FastAPI returns `{"detail": "..."}`, and those messages are written to be read by a
 * person — "No demo price available for held symbol(s): XYZ" is far more useful than
 * "the request failed with status 503". Falls back when the body is not JSON.
 */
async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string' && body.detail !== '') return body.detail
  } catch {
    // Not JSON. Use the generic message below.
  }
  return `The API returned ${response.status}.`
}
