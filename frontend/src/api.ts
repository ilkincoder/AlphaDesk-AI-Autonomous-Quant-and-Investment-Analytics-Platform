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
 * `status` is `proposed`, `no_change`, `unavailable`, `interrupted`, or `generating` — the
 * *run's* life, and nothing to do with what a person decided about it. A decision lives beside
 * the proposal rather than in its status: an approved proposal is still a `proposed` one, and
 * `decision` below is what says somebody acted on it.
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
  /** What a person decided about this proposal, or null if nobody has. */
  decision: ProposalDecision | null
  /** The execution batch an approval opened, so a page restoring itself can offer to open it. */
  batch_id: string | null
}

/** One order as the decision resolved it. Every figure is the server's, not a browser's. */
export type DecisionOrder = {
  sequence: number
  symbol: string
  /** "buy" or "sell". Always shown as a word. */
  side: string
  quantity: string
  /** The estimate the plan was approved at. Not a quote. */
  estimated_price: string
  estimated_value: string
  /** What the account held when the plan was validated. */
  quantity_before: string
}

/** Mirrors `ProposalDecisionOut`: the recorded decision and the exact orders it bound to. */
export type ProposalDecision = {
  decision_id: string
  proposal_id: string
  /** "approved" or "rejected". */
  decision: string
  decided_at: string
  plan_fingerprint: string
  price_drift_percent: string | null
  reason: string | null
  order_plan: DecisionOrder[]
}

/** Mirrors `DecisionOutcomeOut`: what a decision request did.
 *
 * `renewal_required` is an answer rather than an error — a plan whose prices moved, waiting for
 * a second confirmation — and so is `already_decided`, which is a retry or a double click
 * landing on the decision that already exists.
 */
export type DecisionOutcome = {
  outcome: string
  detail: string
  reason: string | null
  decision: ProposalDecision | null
  batch_id: string | null
  plan: DecisionOrder[]
  price_drift_percent: string | null
}

/** Mirrors `OrderIntentOut`: one order in a batch, and everything the broker has said about it.
 *
 * `estimated_price` is the plan's figure and `filled_avg_price` is the only actual price here.
 * The two are deliberately separate fields, so a page cannot show one as the other.
 */
export type OrderIntent = {
  intent_id: string
  sequence: number
  symbol: string
  side: string
  /** Requested, whole shares. Never resized after approval. */
  quantity: string
  order_type: string
  time_in_force: string
  /** planned, submitting, accepted, partially_filled, filled, rejected, canceled, expired,
   *  unknown, not_found, halted. */
  status: string
  /** The broker's own status verbatim, beside the mapped one. */
  broker_status: string | null
  estimated_price: string
  quantity_before: string
  filled_quantity: string
  filled_avg_price: string | null
  client_order_id: string
  broker_order_id: string | null
  submitted_at: string | null
  resolved_at: string | null
  error: string | null
}

/** Mirrors `ExecutionBatchOut`: an approved plan and how far it has got.
 *
 * `sync_status` is the *portfolio* sync and is deliberately separate from `status`: a filled
 * order that could not be written into the portfolio is a different problem from an order that
 * did not fill.
 */
export type ExecutionBatch = {
  batch_id: string
  proposal_id: string
  /** pending, submitting, completed, partial or halted. */
  status: string
  created_at: string
  started_at: string | null
  completed_at: string | null
  currency: string
  cash_at_approval: string
  reserved_cash: string
  price_drift_percent: string | null
  notes: string[]
  halt_reason: string | null
  sync_status: string
  sync_error: string | null
  sync_at: string | null
  orders: OrderIntent[]
}

/** Mirrors `OrdersView`: every batch, newest first, and which one is running. */
export type OrdersView = {
  active_batch_id: string | null
  batches: ExecutionBatch[]
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

/** Record a decision about one proposal.
 *
 * The body carries a decision and nothing else: no quantity, no symbol and no side. What is
 * executed is resolved server-side from the proposal's own stored calculation, and the server
 * refuses a body that tries to say otherwise.
 *
 * `confirm` is the second click on a plan whose prices moved. It is not a permission, and an
 * approval that needed no renewal does not need it.
 */
export function decideProposal(
  proposalId: string,
  body: { decision: 'approve' | 'reject'; idempotency_key: string; confirm?: boolean; reason?: string },
  signal?: AbortSignal,
): Promise<DecisionOutcome> {
  return request<DecisionOutcome>(
    `/api/rebalance/proposal/${encodeURIComponent(proposalId)}/decision`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    },
  )
}

/** Every execution batch, newest first, and which one is running.
 *
 * A read. Statuses are advanced by the server's own execution loop rather than by this call, so
 * a page that polls sees a batch move without the browser driving anything. */
export function fetchOrders(signal?: AbortSignal): Promise<OrdersView> {
  return request<OrdersView>('/api/orders', { signal })
}

/** One execution batch, followed while the server's own loop works on it.
 *
 * The single-batch route rather than the whole list: a page waiting on the batch an approval
 * just opened is waiting on that one, and reading fifty others with all their orders would be
 * paying for an answer nobody asked for. 404s for a batch that is not this portfolio's, which
 * the server reports as "not found" rather than "forbidden" -- it cannot confirm the existence
 * of an id that is not its own.
 */
export function fetchBatch(batchId: string, signal?: AbortSignal): Promise<ExecutionBatch> {
  return request<ExecutionBatch>(`/api/orders/${encodeURIComponent(batchId)}`, { signal })
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

// --- backtesting -------------------------------------------------------------------------------
//
// One strategy is defined by the application, and this file mirrors what the API says about it
// rather than restating it: the rules, the execution notes and the bounds an entry is checked
// against all arrive from `GET /backtest/strategies`, so the page cannot drift from the engine.

/** Mirrors `BacktestStrategyOut`.
 *
 * The `min_*`/`max_*` bounds are convenience, not the guard: the server re-checks every one of
 * them. They exist so a typo is caught before a provider request is spent on it.
 */
/** One number a strategy is configured with, as the strategy declares it.
 *
 * The page renders a field per declaration, so a strategy with three settings of its own -- a
 * period and two thresholds -- needs no change here. `label` carries the unit in the words a
 * person reads, and the bounds are what the form refuses an entry against before the round trip.
 */
export type BacktestParam = {
  name: string
  label: string
  default: number
  minimum: number
  maximum: number
}

export type BacktestStrategy = {
  id: string
  version: string
  name: string
  summary: string
  rules: string[]
  execution: string[]
  parameters: BacktestParam[]
  min_commission_per_share: string
  max_commission_per_share: string
  min_slippage_percent: string
  max_slippage_percent: string
  max_initial_capital: string
}

/** Mirrors `BacktestStrategyListOut`. */
export type BacktestStrategyList = {
  strategies: BacktestStrategy[]
}

/** Mirrors `BacktestInstrumentOut`.
 *
 * A *listing*, not a ticker: `exchange` is part of the identity, because the same symbol on two
 * exchanges is two instruments with two price series.
 */
export type BacktestInstrument = {
  symbol: string
  name: string
  exchange: string
  instrument_type: string
  currency: string
  country: string
}

/** Mirrors `BacktestInstrumentListOut`. `query` comes back so the page can tell the results for
 *  what is in the box from the results for what was typed two keystrokes ago. */
export type BacktestInstrumentList = {
  query: string
  matches: BacktestInstrument[]
}

/** One date on a curve, marked at that session's close. The first point of every curve is the
 *  initial capital, dated the requested start date. */
export type BacktestEquityPoint = {
  on: string
  equity: string
}

/** Mirrors `BacktestTradeOut`. `price` is the price actually paid or received -- the session's
 *  open moved by the slippage model -- and `gross_value` is `quantity * price`. */
export type BacktestTrade = {
  executed_on: string
  /** `"buy"` or `"sell"`. Always shown as a word. */
  side: string
  /** A whole number of shares, so a JSON number rather than a string. */
  quantity: number
  price: string
  gross_value: string
  commission: string
  cash_after: string
  signalled_on: string
}

/** Mirrors `BacktestOpenPositionOut`. A position still held at the end, marked rather than sold. */
export type BacktestOpenPosition = {
  quantity: number
  entry_date: string
  entry_price: string
  mark_date: string
  mark_price: string
  market_value: string
}

/** Mirrors `BacktestPerformanceOut`. `total_return_percent` is signed; `max_drawdown_percent` is
 *  a positive magnitude, because a drawdown is already a fall. */
export type BacktestPerformance = {
  final_value: string
  total_return_percent: string
  max_drawdown_percent: string
}

/** Mirrors `BacktestCoverageOut`. `warmup_sessions` were read and excluded from every figure. */
export type BacktestCoverage = {
  first_session: string
  last_session: string
  sessions: number
  warmup_sessions: number
  data_first_date: string
  data_last_date: string
  data_sessions: number
}

/** Mirrors `BacktestRunOut`.
 *
 * Every run here finished: a run that failed is returned as an error and stored nowhere, which
 * is why there is no status on this type. The two curves are the strategy's and buy-and-hold's
 * over the same instrument, capital, window and costs.
 */
export type BacktestRun = {
  run_id: string
  request_id: string
  created_at: string
  strategy_id: string
  strategy_version: string
  strategy_name: string
  symbol: string
  exchange: string
  instrument_name: string | null
  instrument_type: string | null
  currency: string
  start_date: string
  end_date: string
  initial_capital: string
  /** The strategy's own parameters as it was run with, keyed by the names it declares. */
  parameters: Record<string, number>
  commission_per_share: string
  slippage_percent: string
  provider: string
  /** Our vocabulary: `"raw"` or `"adjusted"`. */
  adjustment_basis: string
  /** The provider's own mode, e.g. `"splits"`. Kept apart from the word above because it is
   *  what decides whether these figures are a price return or a total return. */
  provider_adjust_mode: string
  data_retrieved_at: string
  coverage: BacktestCoverage | null
  equity: BacktestEquityPoint[]
  benchmark_equity: BacktestEquityPoint[]
  trades: BacktestTrade[]
  performance: BacktestPerformance | null
  benchmark: BacktestPerformance | null
  cash_remaining: string | null
  open_position: BacktestOpenPosition | null
  assumptions: string[]
  limitations: string[]
  /** The explanation, when one has been written. `null` is the ordinary state: a run is
   *  complete without prose, and the model and prompt version say who wrote it and under what
   *  request. */
  explanation: string | null
  explanation_model: string | null
  explanation_prompt_version: string | null
}

/** Mirrors `BacktestExplanationOut`.
 *
 * `status` is the whole answer: `ok`, or `unavailable` with a `reason`. An explanation that
 * could not be written is not an error -- the results stand without it -- so both come back
 * 200 and the page keeps the figures on screen.
 */
export type BacktestExplanation = {
  status: string
  reason: string | null
  detail: string | null
  explanation: string | null
  model: string | null
  prompt_version: string | null
}

/** What `POST /backtest/runs` accepts.
 *
 * `exchange` is sent when a search result was chosen and omitted on the portfolio path, which
 * knows only the ticker -- the server resolves the listing either way. No quantity and no price
 * are sent: selecting a holding chooses an instrument and nothing else, and the run starts from
 * `initial_capital`, flat.
 *
 * Every money and percentage field is a string, as everywhere else in this file.
 */
export type BacktestRunBody = {
  request_id: string
  strategy_id: string
  symbol: string
  exchange?: string
  start_date: string
  end_date: string
  initial_capital: string
  /** The chosen strategy's parameters, named as that strategy names them. */
  parameters: Record<string, number>
  costs: {
    commission_per_share: string
    slippage_percent: string
  }
}

/** The strategies this application defines. */
export function fetchBacktestStrategies(signal?: AbortSignal): Promise<BacktestStrategyList> {
  return request<BacktestStrategyList>('/api/backtest/strategies', { signal })
}

/** Find a stock by ticker or company name.
 *
 * An empty `matches` list is an answer -- nothing matched -- and not a failure, which arrives as
 * an `ApiError` instead. The two are shown differently.
 */
export function searchBacktestInstruments(
  q: string,
  signal?: AbortSignal,
): Promise<BacktestInstrumentList> {
  return request<BacktestInstrumentList>(
    `/api/backtest/instruments?q=${encodeURIComponent(q)}`,
    { signal },
  )
}

/** Run a backtest and store it.
 *
 * POST because it fetches prices and writes a run, and because a GET that does either is the
 * kind of thing a proxy or a prefetch is entitled to replay. The `request_id` is what makes a
 * retry free: the same id returns the row it already produced rather than paying for the
 * provider request twice. Nothing is sent to a broker -- this simulates trades over sessions
 * that have already happened.
 */
export function runBacktest(
  body: BacktestRunBody,
  signal?: AbortSignal,
): Promise<BacktestRun> {
  return request<BacktestRun>('/api/backtest/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
}

/** The most recent stored run, or `null` when nothing has been run.
 *
 * This is what the page reads on load so it can put back what it was last showing. `run` is
 * `null` rather than a 404 when there is nothing to restore: "there is no saved run" is an
 * answer, not a failure. Nothing is recalculated and no prices are refetched -- the row holds
 * the resolved instrument, the configuration and the results.
 */
export function fetchLatestBacktestRun(
  signal?: AbortSignal,
): Promise<{ run: BacktestRun | null }> {
  return request<{ run: BacktestRun | null }>('/api/backtest/runs/latest', { signal })
}

/** Ask for an explanation of a stored run.
 *
 * POST because the first call writes one: it asks a model to read the run's own figures and
 * stores the prose beside them. A repeated call is answered from what was stored and costs
 * nothing, so a retry and a double click are the same as one. Nothing here is sent to a broker.
 */
export function explainBacktestRun(
  runId: string,
  signal?: AbortSignal,
): Promise<BacktestExplanation> {
  return request<BacktestExplanation>(
    `/api/backtest/runs/${encodeURIComponent(runId)}/explanation`,
    { method: 'POST', signal },
  )
}
