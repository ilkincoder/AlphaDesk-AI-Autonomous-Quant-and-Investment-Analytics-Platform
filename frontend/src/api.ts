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

/** The filters a listing or a search accepts, as the backend spells them.
 *
 * `provider` is the feed slug -- `alpaca_news`, `official_fed_monetary` -- which is what the
 * page's Source filter holds and what the route filters on. It is not the publisher's name:
 * the two BLS feeds share one, so a name could not tell them apart. */
export type NewsFilters = {
  provider?: string
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

/** Mirrors `ProposalPositionOut` in backend/app/schemas.py.
 *
 * Every numeric field is a string, as everywhere else in this file: the API computes these in
 * `Decimal` and hands them over as strings, and parsing one into a number here would undo that
 * on the first line of component code.
 */
export type ProposalPosition = {
  symbol: string
  quantity: string
  price: string
  holding_value: string
  /** Null when the allocation's total is zero: a share of nothing is undefined, not zero. */
  allocation_percent: string | null
}

/** Mirrors `ProposalAllocationOut`. One complete allocation; `before` and `after` are two. */
export type ProposalAllocation = {
  total_value: string
  cash_balance: string
  cash_allocation_percent: string | null
  holdings: ProposalPosition[]
}

/** Mirrors `ProposalSnapshotOut`. What the proposal was computed from, frozen on the record. */
export type ProposalSnapshot = {
  read_at: string
  price_source: string
  last_synced_at: string | null
  currency: string
  cash_balance: string
  holdings_value: string
  total_value: string
  /** The broker's own equity where there is one. Need not equal positions plus cash. */
  reported_total_value: string | null
  cash_allocation_percent: string | null
  positions: ProposalPosition[]
}

/** Mirrors `ProposalPolicyOut`. The rules the targets were proposed under. */
export type ProposalPolicy = {
  name: string
  description: string
  constraints: string[]
  scenario: { description: string; shock_percent: string }
}

/** Mirrors `ProposalTradeOut`. One proposed whole-share order, sent nowhere. */
export type ProposalTrade = {
  symbol: string
  /** `"buy"` or `"sell"`. Always shown as a word: colour alone does not carry the direction. */
  action: string
  quantity: string
  reference_price: string
  estimated_value: string
  quantity_before: string
  quantity_after: string
}

/** Mirrors `ProposalScenarioOut`. The assumed shock, calculated rather than predicted. */
export type ProposalScenario = {
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

/** Mirrors `ProposalAllocationLineOut`: one line of the target schedule.
 *
 * Three weights rather than one, because they are three different facts. `target_percent` is
 * what the agent asked for, `achieved_percent` is where whole-share rounding actually leaves
 * the holding, and `current_percent` is what the portfolio holds now. Showing only the target
 * would be showing the flattering one.
 */
export type ProposalAllocationLine = {
  /** A held symbol, or `"CASH"`. */
  symbol: string
  current_percent: string | null
  target_percent: string
  achieved_percent: string | null
  /** `increase`, `decrease` or `retain`. */
  movement: string
  /** The agent's own reason for that specific number. Empty when it gave none. */
  reason: string
  /** The retrieved articles the reason rests on; empty for policy-based reasoning. */
  evidence_refs: string[]
}

/** Mirrors `ProposalCalculationOut`. Every figure the page shows comes from here. */
export type ProposalCalculation = {
  outcome: string
  trades: ProposalTrade[]
  targets: Record<string, string>
  allocations: ProposalAllocationLine[]
  cash_target: string
  before: ProposalAllocation
  after: ProposalAllocation
  cash_before: string
  cash_after: string
  sell_proceeds: string
  buy_cost: string
  /** True when the purchases depend on the proposed sales executing first. */
  buys_depend_on_sells: boolean
  largest_before: { symbol: string; allocation_percent: string | null }
  largest_after: { symbol: string; allocation_percent: string | null }
  scenario: ProposalScenario
  reconciliation: {
    reported_total_value: string | null
    summed_total_value: string
    difference: string | null
    note: string
  }
}

/** Mirrors `ProposalEvidenceOut`: one retrieved article, as a citation. */
export type ProposalEvidence = {
  reference: string
  article_id: number
  title: string
  publisher: string
  url: string
  published_at: string
  category: string
  symbols: string[]
  /** A cosine similarity, not a probability. */
  similarity: number
  /** False for an article older than the recent window; labelled, never dropped. */
  recent: boolean
}

/** Mirrors `RebalanceProposalOut`.
 *
 * `status` is `proposed`, `no_change`, `unavailable`, `interrupted`, or `generating`. There is
 * no approved, submitted or filled status: this milestone ends at generation.
 */
export type RebalanceProposal = {
  proposal_id: string
  status: string
  created_at: string
  completed_at: string | null
  /** `current`, `prices_updated`, `portfolio_changed` or `unknown`.
   *
   * Four states rather than a flag: a price that ticked is not a portfolio that changed, and
   * a proposal that cannot be checked is not thereby current. */
  freshness: string
  freshness_reason: string | null
  /** The symbols the comparison found a difference in. */
  freshness_changed: string[]
  snapshot: ProposalSnapshot | null
  policy: ProposalPolicy | null
  calculation: ProposalCalculation | null
  evidence: ProposalEvidence[]
  assumptions: string[]
  limitations: string[]
  targets: Record<string, string> | null
  /** Text a model wrote. Presented as such; nothing here corroborates it. */
  rationale: string | null
  failure: string | null
  failure_reason: string | null
  usage: Record<string, unknown> | null
}

/** The latest proposal, or an explicit null. A page restoring itself is asking "is there
 *  one?", and "no" is an answer rather than an error. */
export type ProposalView = {
  proposal: RebalanceProposal | null
}

/** One event from the proposal stream.
 *
 * `stage` events name a step the run actually has; the terminal `done` event carries the same
 * body `GET /rebalance/proposal` returns, so a client whose stream broke has lost nothing.
 */
export type ProposalEvent = {
  event: string
  stage?: string
  detail?: string
  proposal?: RebalanceProposal
}

/** The latest proposal, as the page reads it on load. */
export function fetchProposal(signal?: AbortSignal): Promise<ProposalView> {
  return request<ProposalView>('/api/rebalance/proposal', { signal })
}

/** Ask for a proposal and report which stage the run is in while it works.
 *
 * The request id is the client's, and it is the whole duplicate-suppression story: a retry with
 * the same id is answered from the run already in flight rather than paying for a second one.
 *
 * Read with `fetch` rather than `EventSource`, because `EventSource` cannot POST a body and
 * this request has one -- the same trade `analysisApi.streamMessage` makes, and the frames are
 * parsed the same way.
 */
export async function streamProposal(
  requestId: string,
  onEvent: (event: ProposalEvent) => void,
  signal?: AbortSignal,
): Promise<RebalanceProposal> {
  let response: Response
  try {
    response = await fetch('/api/rebalance/proposal/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ request_id: requestId }),
      signal,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new ApiError('Could not reach the API.', null)
  }

  // A 200 or a 202 that is not an event stream is a *replayed* request: one already running, or
  // one already finished, served as ordinary JSON because there is nothing new to report. Both
  // carry the stored proposal, and reading them as a stream would find no frames and report
  // "the stream ended without an answer" for a request that has one.
  const contentType = response.headers.get('content-type') ?? ''
  if (response.status === 202 || !contentType.startsWith('text/event-stream')) {
    const body = (await response.json().catch(() => ({}))) as { proposal?: RebalanceProposal }
    if (body.proposal) return body.proposal
    throw new ApiError('A proposal is already being generated.', response.status)
  }
  if (!response.ok) {
    throw new ApiError(await readErrorDetail(response), response.status)
  }
  if (!response.body) {
    throw new ApiError('The API returned no stream.', response.status)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let proposal: RebalanceProposal | null = null
  let failure: string | null = null

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      // Frames are separated by a blank line; anything after the last separator is partial.
      let separator = buffer.indexOf('\n\n')
      while (separator !== -1) {
        const frame = buffer.slice(0, separator)
        buffer = buffer.slice(separator + 2)
        const event = parseProposalFrame(frame)
        if (event) {
          onEvent(event)
          if (event.event === 'done' && event.proposal) proposal = event.proposal
          if (event.event === 'error') failure = event.detail ?? 'The proposal failed.'
        }
        separator = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.cancel().catch(() => {
      // The stream is already gone. Nothing to do and nothing to report.
    })
  }

  if (proposal) return proposal
  throw new ApiError(failure ?? 'The stream ended without a proposal.', null)
}

function parseProposalFrame(frame: string): ProposalEvent | null {
  const dataLines = frame
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())

  if (dataLines.length === 0) return null
  try {
    return JSON.parse(dataLines.join('\n')) as ProposalEvent
  } catch {
    return null
  }
}
