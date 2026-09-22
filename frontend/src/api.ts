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
  /** `"demo"` for the fictional demo table, otherwise a broker slug. */
  price_source: string
  /** When the snapshot behind these figures was read from the broker; null on demo data. */
  last_synced_at: string | null
  cash_balance: string
  holdings_value: string
  total_value: string
  cash_allocation_percent: string | null
  holdings: ValuationHolding[]
}

/** Mirrors `PortfolioSyncOut` in backend/app/schemas.py.
 *
 * `applied` is false when a newer snapshot was already stored and this one was refused for
 * being older; the figures are then the stored ones. */
export type SyncResult = {
  applied: boolean
  portfolio_id: number
  broker: string | null
  broker_account_id: string | null
  last_synced_at: string | null
  currency: string
  cash_balance: string
  equity: string | null
  position_count: number
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
  last_synced_at: string | null
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

/** Mirrors `NewsArticleOut` in backend/app/schemas.py.
 *
 * Two times on every article, and they are not interchangeable: `published_at` is when the
 * publisher says the story ran, `ingested_at` is when our database stored it. A page that
 * showed only one could not say whether a story was new or merely newly fetched.
 */
export type NewsArticle = {
  id: number
  provider: string
  source: string
  category: string
  title: string
  excerpt: string
  url: string
  symbols: string[]
  published_at: string
  provider_updated_at: string | null
  ingested_at: string
  /** False while an article is stored but not yet in the search index. */
  indexed: boolean
}

/** Mirrors `NewsSourceStatusOut`. */
export type NewsSourceStatus = {
  source: string
  last_success_at: string | null
}

/** Mirrors `NewsListOut`. */
export type NewsList = {
  total: number
  limit: number
  offset: number
  articles: NewsArticle[]
  sources: NewsSourceStatus[]
  /** A sentence about the feed's freshness, written by the backend so the page cannot
   *  drift from it. Never says real-time. */
  timeliness: string
}

/** Mirrors `NewsSearchPassageOut`. `similarity` is a cosine similarity, not a percentage:
 *  it says how close two vectors are and nothing about whether the passage is an answer. */
export type NewsSearchPassage = {
  text: string
  similarity: number
  chunk_index: number
  article: NewsArticle
}

/** Mirrors `NewsSearchOut`. `status` distinguishes "nothing matched" from "nothing is
 *  indexed" from "the index is down", which are three different things to be told. */
export type NewsSearch = {
  status: string
  reason: string | null
  query: string
  returned: number
  passages: NewsSearchPassage[]
  warnings: string[]
}

/** Mirrors `NewsSourceResultOut`: what one source did during one ingestion. */
export type NewsSourceResult = {
  source: string
  status: string
  fetched: number
  new: number
  updated: number
  unchanged: number
  indexed: number
  failed_index: number
  error: string | null
  last_success_at: string | null
}

/** Mirrors `NewsIngestOut`. A partial failure is a success here: `failed_sources` names
 *  what did not work while the rest is stored. */
export type NewsIngest = {
  started_at: string
  completed_at: string
  symbols: string[]
  stored: number
  indexed: number
  failed_sources: string[]
  sources: NewsSourceResult[]
}

/** The filters a listing or a search accepts, as the backend spells them. */
export type NewsFilters = {
  source?: string
  category?: string
  symbol?: string
}

function newsParams(filters: NewsFilters, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams(extra)
  // Empty values are omitted rather than sent as blanks: `source=` would be a filter
  // matching the empty string, which is not the same as no filter at all.
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value)
  }
  const query = params.toString()
  return query ? `?${query}` : ''
}

/** A page of stored articles, newest first by publication time. */
export function fetchNews(
  filters: NewsFilters = {},
  { limit = 20, offset = 0 }: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<NewsList> {
  const query = newsParams(filters, { limit: String(limit), offset: String(offset) })
  return request<NewsList>(`/api/news${query}`, { signal })
}

/** Semantically similar passages, filtered the same way a listing is. */
export function searchNews(
  q: string,
  filters: NewsFilters = {},
  signal?: AbortSignal,
): Promise<NewsSearch> {
  return request<NewsSearch>(`/api/news/search${newsParams(filters, { q })}`, { signal })
}

/** Read every source once and store what is new.
 *
 * POST because it fetches and writes rather than returning what is stored, and it is the
 * only call on this page that reaches a provider. Bounded on the server: an article count,
 * entries per feed, release pages and a window. */
export function ingestNews(signal?: AbortSignal): Promise<NewsIngest> {
  return request<NewsIngest>('/api/news/ingest', { method: 'POST', signal })
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

/** Read the Alpaca paper account and store it as the portfolio's snapshot.
 *
 * The one call that writes. POST because it changes the stored snapshot rather than
 * returning one — and because a GET that mutates is the kind of thing a proxy or a prefetch
 * is entitled to replay. Nothing is sent to the broker: it reads an account and its
 * positions, and places no orders. */
export function syncPortfolio(signal?: AbortSignal): Promise<SyncResult> {
  return request<SyncResult>('/api/portfolio/sync', { method: 'POST', signal })
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
